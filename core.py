"""Shared config, database, Telegram and yt-dlp helpers for Tel Spot."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

import aiohttp
import yt_dlp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


BOT_TOKEN = _env("BOT_TOKEN")
# --- access control: members must answer a secret question the first time (empty = no question)
ACCESS_ANSWERS = [a.strip().lower().replace(" ", "") for a in _env("ACCESS_ANSWER").split(",") if a.strip()]
ACCESS_QUESTION = _env("ACCESS_QUESTION", "🔐 Secret question: what is the codename of the creator of this project?")
USER_MAX_GROUPS = int(_env("USER_MAX_GROUPS", "2") or 2)        # groups one (non-owner) user may connect
USER_MAX_PLAYLISTS = int(_env("USER_MAX_PLAYLISTS", "5") or 5)  # per personal group
USER_MAX_ARTISTS = int(_env("USER_MAX_ARTISTS", "15") or 15)    # per personal group
USER_SONGS_PER_HOUR = int(_env("USER_SONGS_PER_HOUR", "20") or 20)  # search downloads per person
MAIN_GROUP_LINK = _env("MAIN_GROUP_LINK")  # invite link of the recommended group (else the bot makes one)
OWNER_ENV = int(_env("OWNER_ID", "0") or 0)  # your numeric Telegram id (or claim it with /claim)
CHAT_ID = int(_env("CHAT_ID", "0") or 0)  # supergroup id, like -100123456789
SYNC_INTERVAL = int(_env("SYNC_INTERVAL", "600"))  # playlist check interval, seconds
YTDLP_COOKIES = _env("YTDLP_COOKIES")  # optional cookies.txt path
SEND_DELAY = float(_env("SEND_DELAY", "3.5"))  # seconds between playlist sends
PLAYLISTS_FILE = _env("PLAYLISTS_FILE", "playlists.txt")
ARTISTS_FILE = _env("ARTISTS_FILE", "artists.txt")
DB_PATH = _env("DB_PATH", "state.db")

# new-release monitor
RELEASE_INTERVAL = int(_env("RELEASE_INTERVAL", "180"))  # seconds between checks
RELEASES_TOPIC_NAME = _env("RELEASES_TOPIC_NAME", "🆕 New Releases")
RELEASE_MAX_AGE_HOURS = int(_env("RELEASE_MAX_AGE_HOURS", "72"))  # ignore older uploads
RELEASE_MIN_SECONDS = int(_env("RELEASE_MIN_SECONDS", "60"))
RELEASE_MAX_SECONDS = int(_env("RELEASE_MAX_SECONDS", "720"))
FAV_TOPIC_NAME = _env("FAV_TOPIC_NAME", "❤️ Favorites")  # songs you react to with a heart
FAV_EMOJIS = {e.strip().replace("\ufe0f", "") for e in _env("FAV_EMOJIS", "❤").split(",") if e.strip()}
PICKS_TOPIC_NAME = _env("PICKS_TOPIC_NAME", "🎧 Picks")  # songs added from the search menu
RELEASE_MAX_ATTEMPTS = int(_env("RELEASE_MAX_ATTEMPTS", "20"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# no ffmpeg needed: progressive m4a (YouTube) / mp3 (SoundCloud) formats
AUDIO_FMT_YT = "bestaudio[ext=m4a]/bestaudio"
AUDIO_FMT_SC = "http_mp3_128/bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telspot")

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS playlists (
        playlist_id TEXT PRIMARY KEY, topic_id INTEGER, name TEXT, cover_sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tracks (
        playlist_id TEXT, track_id TEXT, message_id INTEGER,
        PRIMARY KEY (playlist_id, track_id));
    CREATE TABLE IF NOT EXISTS files (track_id TEXT PRIMARY KEY, file_id TEXT);
    CREATE TABLE IF NOT EXISTS failed (
        playlist_id TEXT, track_id TEXT, attempts INTEGER DEFAULT 0,
        PRIMARY KEY (playlist_id, track_id));
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS releases (
        key TEXT PRIMARY KEY, artist TEXT, title TEXT, platform TEXT, url TEXT,
        sent_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS seen (item TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS source_state (src TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS attempts (item TEXT PRIMARY KEY, n INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS lyrics_cache (key TEXT PRIMARY KEY, text TEXT);
    CREATE TABLE IF NOT EXISTS ws (chat_id INTEGER PRIMARY KEY, owner_uid INTEGER, title TEXT, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS audio_meta (key TEXT PRIMARY KEY, artist TEXT, title TEXT, duration INTEGER);
    CREATE TABLE IF NOT EXISTS users (
        uid INTEGER PRIMARY KEY, username TEXT, name TEXT, first_seen INTEGER, last_seen INTEGER,
        verified_at INTEGER, blocked INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, locked_until INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS favs (key TEXT PRIMARY KEY, src_msg INTEGER, fav_msg INTEGER, uid INTEGER);
    CREATE TABLE IF NOT EXISTS picks (
        key TEXT PRIMARY KEY, dz_id INTEGER, artist TEXT, title TEXT, added_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """
)
_cols = [r[1] for r in db.execute("PRAGMA table_info(audios)")]
if _cols and "chat_id" not in _cols:  # older versions knew a single group only
    db.executescript(
        f"ALTER TABLE audios RENAME TO audios_old;"
        f"CREATE TABLE audios (chat_id INTEGER, msg_id INTEGER, key TEXT, PRIMARY KEY (chat_id, msg_id));"
        f"INSERT INTO audios SELECT {CHAT_ID}, msg_id, key FROM audios_old; DROP TABLE audios_old;")
elif not _cols:
    db.execute("CREATE TABLE audios (chat_id INTEGER, msg_id INTEGER, key TEXT, PRIMARY KEY (chat_id, msg_id))")
try:  # databases created by older versions have no cover_sent column
    db.execute("ALTER TABLE playlists ADD COLUMN cover_sent INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
db.commit()

# --------------------------------------------------------------- telegram
_dl_lock: asyncio.Lock | None = None


def dl_lock() -> asyncio.Lock:
    """One download at a time (keeps CPU/RAM low). Created lazily inside the running loop."""
    global _dl_lock
    if _dl_lock is None:
        _dl_lock = asyncio.Lock()
    return _dl_lock


async def tg_call(coro_factory):
    """Run a Telegram call, sleeping through flood-wait errors."""
    while True:
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            log.warning("flood wait %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)


# --------------------------------------------------------------- groups
# Every connected group is a "workspace" with its own playlists, artists, topics and history.
# The main group (CHAT_ID) belongs to the owner. Data of other groups is stored under keys
# prefixed with "<chat id>|" so one database serves all of them.
def is_main(chat_id: int) -> bool:
    return chat_id == CHAT_ID


def sk(chat_id: int, key: str) -> str:
    """Scoped key: unchanged for the main group (keeps old data valid), prefixed otherwise."""
    return key if chat_id == CHAT_ID else f"{chat_id}|{key}"


def scope_where(chat_id: int, col: str) -> tuple[str, tuple]:
    """SQL condition that selects the rows of one group in a table whose `col` holds scoped keys."""
    if chat_id == CHAT_ID:
        return f"{col} NOT LIKE '%|%'", ()
    return f"{col} LIKE ?", (f"{chat_id}|%",)


def ws_dir(chat_id: int) -> Path:
    d = Path(DB_PATH).resolve().parent / "ws" / str(abs(chat_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def playlists_file(chat_id: int) -> str:
    return PLAYLISTS_FILE if chat_id == CHAT_ID else str(ws_dir(chat_id) / "playlists.txt")


def artists_file(chat_id: int) -> str:
    return ARTISTS_FILE if chat_id == CHAT_ID else str(ws_dir(chat_id) / "artists.txt")


def ws_register(chat_id: int, owner_uid: int, title: str) -> None:
    db.execute("INSERT INTO ws VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, "
               "owner_uid=excluded.owner_uid", (chat_id, owner_uid, title or "", int(time.time())))
    db.commit()


def ws_unregister(chat_id: int) -> None:
    db.execute("DELETE FROM ws WHERE chat_id=?", (chat_id,))
    db.commit()


def ws_ids() -> list[int]:
    """Main group first, then every connected group."""
    ids = [CHAT_ID] if CHAT_ID else []
    return ids + [r[0] for r in db.execute("SELECT chat_id FROM ws ORDER BY created_at")]


def ws_owner(chat_id: int) -> int:
    if chat_id == CHAT_ID:
        return owner_id()
    r = db.execute("SELECT owner_uid FROM ws WHERE chat_id=?", (chat_id,)).fetchone()
    return r[0] if r else 0


def ws_title(chat_id: int) -> str:
    if chat_id == CHAT_ID:
        return "Main group"
    r = db.execute("SELECT title FROM ws WHERE chat_id=?", (chat_id,)).fetchone()
    return (r[0] if r else "") or str(chat_id)


def user_ws(uid: int) -> list[int]:
    """Groups this person owns (the owner also manages the main group)."""
    ids = [r[0] for r in db.execute("SELECT chat_id FROM ws WHERE owner_uid=? ORDER BY created_at", (uid,))]
    return ([CHAT_ID] if CHAT_ID and is_owner(uid) else []) + ids


def active_ws(uid: int) -> int | None:
    """The group a private-chat menu works on."""
    mine = user_ws(uid)
    if not mine:
        return None
    r = db.execute("SELECT value FROM meta WHERE key=?", (f"active:{uid}",)).fetchone()
    return int(r[0]) if r and int(r[0]) in mine else mine[0]


def set_active_ws(uid: int, chat_id: int) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"active:{uid}", str(chat_id)))
    db.commit()


def limits(chat_id: int) -> tuple[int, int]:
    """(max playlists, max artists) for a group; the main group has no limit."""
    return (10**6, 10**6) if chat_id == CHAT_ID else (USER_MAX_PLAYLISTS, USER_MAX_ARTISTS)


_song_log: dict[int, list[float]] = {}


def song_quota(uid: int) -> bool:
    """True if this person may fetch another song now (per-hour limit, owner exempt)."""
    if is_owner(uid):
        return True
    now = time.time()
    lst = [t for t in _song_log.get(uid, []) if now - t < 3600]
    if len(lst) >= USER_SONGS_PER_HOUR:
        _song_log[uid] = lst
        return False
    lst.append(now)
    _song_log[uid] = lst
    return True


def setting(key: str, default: str = "") -> str:
    r = db.execute("SELECT value FROM meta WHERE key=?", (f"cfg:{key}",)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"cfg:{key}", value))
    db.commit()


def require_join() -> bool:
    """Owner's switch: must people join the main group before they may use the bot?"""
    return setting("require_join", "0") == "1"


MAIN_LINK = {"url": MAIN_GROUP_LINK}  # filled at start when empty


async def topic_id_for(bot: Bot, chat_id: int, key: str, name: str, color: int | None = None,
                       force_new: bool = False) -> int:
    """Topic id stored under meta[key] for that group; creates the topic when missing (or when force_new)."""
    mk = sk(chat_id, key)
    row = db.execute("SELECT value FROM meta WHERE key=?", (mk,)).fetchone()
    if row and not force_new:
        return int(row[0])
    kw = {"icon_color": color} if color else {}
    topic = await tg_call(lambda: bot.create_forum_topic(chat_id, name[:128], **kw))
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (mk, str(topic.message_thread_id)))
    db.commit()
    return topic.message_thread_id


def topic_known(chat_id: int, key: str) -> int | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (sk(chat_id, key),)).fetchone()
    return int(row[0]) if row else None


def record_audio(chat_id: int, msg_id: int, song_key: str, artist: str = "", title: str = "", duration: int = 0) -> None:
    """Remember which song a group message holds (needed to react ❤️ -> Favorites)."""
    db.execute("INSERT OR REPLACE INTO audios VALUES (?, ?, ?)", (chat_id, msg_id, song_key))
    if title:
        db.execute("INSERT OR REPLACE INTO audio_meta VALUES (?,?,?,?)", (song_key, artist, title, int(duration or 0)))
    db.commit()


async def send_audio_topic(bot: Bot, chat_id: int, key: str, name: str, color: int | None, audio,
                           song_key: str | None = None, **kw):
    """Send audio into a named topic of a group; recreate the topic once if someone deleted it."""
    for attempt in (1, 2):
        topic_id = await topic_id_for(bot, chat_id, key, name, color, force_new=(attempt == 2))
        try:
            msg = await tg_call(lambda: bot.send_audio(chat_id, audio, message_thread_id=topic_id, **kw))
            if song_key:
                record_audio(chat_id, msg.message_id, song_key, kw.get("performer") or "", kw.get("title") or "",
                             kw.get("duration") or 0)
            return msg
        except TelegramBadRequest as e:
            if attempt == 1 and "thread" in str(e).lower():
                log.error("topic %s was deleted, creating a new one", key)
                continue
            raise


# ------------------------------------------------------------------- auth
ANON_ADMIN_ID = 1087968824  # Telegram's "GroupAnonymousBot" (admin posting anonymously)


MEMBER_STATUSES = ("creator", "administrator", "member", "restricted")


async def _status(bot: Bot, uid: int, chat_id: int | None = None) -> str:
    try:
        return (await bot.get_chat_member(chat_id or CHAT_ID, uid)).status
    except Exception:
        return "left"


def owner_id() -> int:
    """The owner: OWNER_ID from .env, or the id claimed with /claim (stored in the database)."""
    if OWNER_ENV:
        return OWNER_ENV
    row = db.execute("SELECT value FROM meta WHERE key='owner_id'").fetchone()
    return int(row[0]) if row else 0


def is_owner(uid: int) -> bool:
    return bool(uid) and uid == owner_id()


async def is_admin(bot: Bot, uid: int, chat_id: int | None = None) -> bool:
    """Admin of that group (default: the main group). The owner is admin everywhere."""
    return is_owner(uid) or uid == ANON_ADMIN_ID or await _status(bot, uid, chat_id) in ("creator", "administrator")


async def can_manage(bot: Bot, chat_id: int | None, uid: int) -> bool:
    """May this person change things (playlists, artists, add songs) in that group?
    Main group: the owner only. Connected groups: its owner and its admins."""
    if not chat_id or chat_id not in ws_ids():
        return False
    if is_owner(uid):
        return True
    if chat_id == CHAT_ID:
        return False
    return uid == ws_owner(chat_id) or await is_admin(bot, uid, chat_id)


async def joined_main(bot: Bot, uid: int) -> bool:
    return is_owner(uid) or (bool(CHAT_ID) and await _status(bot, uid, CHAT_ID) in MEMBER_STATUSES)


async def can_use(bot: Bot, chat, uid: int) -> bool:
    """Private chat: anyone (personal mode). Groups: only connected ones. If the owner made joining the
    main group mandatory, people who have not joined it cannot use the bot anywhere."""
    if is_owner(uid):
        return True
    if require_join() and not await joined_main(bot, uid):
        return False
    return chat.type == "private" or chat.id in ws_ids()


async def ALLOWED_MSG(event) -> bool:
    c = event.chat
    return c.type == "private" or c.id in ws_ids()


async def ALLOWED_CB(event) -> bool:
    m = event.message
    return bool(m) and (m.chat.type == "private" or m.chat.id in ws_ids())


async def fetch_image(session: aiohttp.ClientSession, url: str, max_bytes: int = 10_000_000) -> bytes | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            data = await r.read()
        return data if len(data) <= max_bytes else None
    except Exception:
        return None


# ----------------------------------------------------------------- yt-dlp
def ytdlp_opts(**extra) -> dict:
    opts: dict = {"quiet": True, "no_warnings": True}
    if YTDLP_COOKIES:
        opts["cookiefile"] = YTDLP_COOKIES
    opts.update(extra)
    return opts


def download_url(url: str, outdir: str, fmt: str) -> tuple[str, dict]:
    """Blocking: download one video/track as audio. Returns (file path, info dict)."""
    opts = ytdlp_opts(noplaylist=True, format=fmt, outtmpl=os.path.join(outdir, "%(id)s.%(ext)s"))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info
