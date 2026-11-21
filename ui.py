"""
Telegram menu for Tel Spot: playlists, artists, search, add songs, follow artists.
Works both inside the group (commands/buttons) and in a private chat with the bot
(for members of the group). Management actions need a group admin.
"""
from __future__ import annotations

import asyncio
import html
import math
import re
import secrets
import shutil
import tempfile
import time

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile, CallbackQuery, ForceReply, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

import favorites
import health
import music
import playlists
import releases
from core import (
    ALLOWED_CB, ALLOWED_MSG, CHAT_ID, MAIN_LINK, PICKS_TOPIC_NAME, USER_MAX_GROUPS, active_ws, can_manage, can_use, db,
    dl_lock, fetch_image, is_owner, limits, log, require_join, scope_where, send_audio_topic, set_active_ws,
    set_setting, sk, song_quota, tg_call, user_ws, ws_ids, ws_title,
)

router = Router()

PER_PAGE = 8
MAX_ADD_ALL = 30
PROMPT_PLAYLIST = "➕ Send a public Spotify playlist link (reply to this message)"
PROMPT_SEARCH = "🔎 Send a song name, an artist, or a line of lyrics you remember (reply to this message)"
PROMPT_LYRICS = "📝 Send a song name, like: Artist - Title (reply to this message). You can also just send me the music file."
PROMPTS = {PROMPT_PLAYLIST, PROMPT_SEARCH, PROMPT_LYRICS}
LYR: dict[str, list[dict]] = {}  # token -> lyrics candidates

BG: set[asyncio.Task] = set()  # keeps background tasks alive until they finish
_adding: set[str] = set()  # song keys currently being added (avoid double sends)
PENDING: dict[str, dict] = {}  # follow dialogs: token -> state
_bot_id: int | None = None


# ----------------------------------------------------------------- helpers
def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def B(text: str, data: str | None = None, url: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    """style: 'primary' (blue), 'success' (green), 'danger' (red); older clients just ignore it."""
    return InlineKeyboardButton(text=text, callback_data=data, url=url, style=style)


def KB(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topic_link(ws: int | None, topic_id: int | None) -> str | None:
    s = str(ws or CHAT_ID)
    return f"https://t.me/c/{s[4:]}/{topic_id}" if topic_id and s.startswith("-100") else None


def _thread(m: Message) -> int | None:
    return m.message_thread_id if m.is_topic_message else None


async def say(bot: Bot, m: Message, text: str, markup=None) -> Message:
    return await tg_call(
        lambda: bot.send_message(
            m.chat.id, text, message_thread_id=_thread(m), reply_markup=markup,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    )


async def edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    BG.add(t)
    t.add_done_callback(BG.discard)


async def my_id(bot: Bot) -> int:
    global _bot_id
    if _bot_id is None:
        _bot_id = (await bot.me()).id
    return _bot_id


def followed(name: str, ws: int = CHAT_ID) -> bool:
    n = releases._norm(name)
    return any(releases._norm(a.name) == n for a in releases.read_artists(ws))


def ws_of(chat, uid: int) -> int | None:
    """Which group a menu works on: the group itself, or (in private) the person's own active group."""
    if chat.type != "private":
        return chat.id if chat.id in ws_ids() else None
    return active_ws(uid)


async def connect_url(bot: Bot) -> str:
    me = (await bot.me()).username
    return f"https://t.me/{me}?startgroup=connect&admin=manage_topics+delete_messages+pin_messages+invite_users"


def nav(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(B("◀", f"{prefix}:{page - 1}"))
    row.append(B(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(B("▶", f"{prefix}:{page + 1}"))
    return row


# keeps the last tracks we showed so a button press needs no extra API call
TRK: dict[int, dict] = {}


def remember(tracks: list[dict]) -> None:
    if len(TRK) > 3000:
        TRK.clear()
    for t in tracks:
        TRK[t["id"]] = t


# ----------------------------------------------------------------- screens
async def main_menu(bot: Bot, ws: int | None, uid: int, private: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[B("🔎  Search songs & artists", "m:se", style="primary")]]
    if ws:
        rows += [[B("🎵  Playlists", "m:pl"), B("🎤  Artists", "m:ar")],
                 [B("❤️  Favorites", "m:fv"), B("📝  Lyrics", "m:ly")],
                 [B("📊  Status", "m:st")]]
        text = "🎧 <b>Tel Spot</b>\n<blockquote>Your music, in Telegram.</blockquote>"
        if private:
            text += f"\n📁 Group: <b>{esc(ws_title(ws))}</b>"
    else:
        rows.append([B("📝  Lyrics", "m:ly")])
        rows.append([B("➕  Connect my own group", url=await connect_url(bot), style="success")])
        text = ("🎧 <b>Tel Spot</b>\n<blockquote>Find songs and lyrics right here. Want your own playlists, "
                "new-release alerts and favorites? Connect a group of yours (a supergroup with Topics).</blockquote>")
    if private and not is_owner(uid) and MAIN_LINK["url"]:
        rows.append([B("⭐  Recommended group", url=MAIN_LINK["url"])])
    if private and ws is not None and len(user_ws(uid)) > 1:
        rows.append([B("🔁  My groups", "m:sw")])
    elif private and ws is None:
        pass
    if private and ws is not None and not is_owner(uid) and len(user_ws(uid)) < USER_MAX_GROUPS:
        rows.append([B("➕  Connect another group", url=await connect_url(bot))])
    if private and is_owner(uid):
        rows.append([B("⚙️  Settings", "m:cf")])
    return text, KB(rows)


def switch_screen(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    cur = active_ws(uid)
    rows = [[B(f"{'✅' if w == cur else '📁'}  {cut(ws_title(w), 36)}", f"sw:{w}")] for w in user_ws(uid)]
    rows.append([B("‹ Menu", "m:main")])
    return "🔁 <b>My groups</b>\n<blockquote>Pick the group the menu works on.</blockquote>", KB(rows)


def settings_screen() -> tuple[str, InlineKeyboardMarkup]:
    rj = require_join()
    n_ws = len(ws_ids()) - 1
    n_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    text = ("⚙️ <b>Settings</b>\n"
            f"<blockquote>Joining your main group is <b>{'mandatory' if rj else 'optional'}</b>.\n"
            "Mandatory: people who have not joined it cannot use the bot at all.\n"
            "Optional: anyone can use it; the group is only recommended.</blockquote>\n"
            f"👥 {n_users} users · 📁 {n_ws} connected group(s)")
    if rj and not MAIN_LINK["url"]:
        text += "\n⚠️ No invite link for the main group yet (set MAIN_GROUP_LINK or give the bot the invite right)."
    return text, KB([[B("🔓  Make it optional" if rj else "🔒  Make it mandatory", "cfg:rj", style="primary")],
                     [B("‹ Menu", "m:main")]])


def playlists_screen(admin: bool, ws: int = CHAT_ID) -> tuple[str, InlineKeyboardMarkup]:
    rows = playlists.playlist_overview(ws)
    lines = ["🎵 <b>Playlists</b>", "<blockquote>Each playlist is a topic in the group.</blockquote>"]
    if not rows:
        lines.append("Nothing here yet.")
    for r in rows:
        link = topic_link(ws, r["topic_id"])
        name = esc(r["name"])
        lines.append(f"▫️ {f'<a href=\"{link}\">{name}</a>' if link else name} · {r['tracks']}")
    if admin and ws != CHAT_ID:
        lines.append(f"<i>{len(rows)}/{limits(ws)[0]} playlists</i>")
    kb_rows = [[B("➕  Add playlist", "pl:add", style="success")]] if admin else []
    if admin:
        for r in rows:
            kb_rows.append([B(f"✖  {cut(r['name'], 32)}", f"pl:rm:{r['id']}", style="danger")])
    kb_rows.append([B("‹ Menu", "m:main")])
    return "\n".join(lines), KB(kb_rows)


def artists_screen(admin: bool, ws: int = CHAT_ID) -> tuple[str, InlineKeyboardMarkup]:
    arts = releases.read_artists(ws)
    lines = ["🎤 <b>Followed artists</b>", "<blockquote>New songs land in the 🆕 New Releases topic.</blockquote>"]
    if not arts:
        lines.append("Nobody yet. Search an artist and tap Follow.")
    for a in arts:
        lines.append(f"▫️ {esc(a.name)} · YT {len(a.yt)} · SC {len(a.sc)}")
    kb_rows = [[B("⭐  Find & follow an artist", "m:se", style="success")]] if admin else []
    if admin:
        for a in arts:
            kb_rows.append([B(f"✖  {cut(a.name, 32)}", f"ar:rm:{releases.artist_hash(a.name)}", style="danger")])
    kb_rows.append([B("‹ Menu", "m:main")])
    return "\n".join(lines), KB(kb_rows)


def favorites_screen(ws: int = CHAT_ID) -> tuple[str, InlineKeyboardMarkup]:
    link = topic_link(ws, favorites.topic_id(ws))
    n = favorites.count(ws)
    text = ("❤️ <b>Favorites</b>\n<blockquote>React with ❤️ to any song in the group and it is copied into the "
            "Favorites topic. Remove the heart to remove it again.</blockquote>\n"
            f"{n} song(s) so far" + (f" · <a href=\"{link}\">open topic</a>" if link else ""))
    return text, KB([[B("‹ Menu", "m:main")]])


def ago(ts: float | None) -> str:
    if not ts:
        return "not yet"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago"


def status_screen(admin: bool, ws: int = CHAT_ID) -> tuple[str, InlineKeyboardMarkup]:
    def q(table: str, col: str, extra: str = "") -> int:
        cond, args = scope_where(ws, col)
        return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {cond} {extra}", args).fetchone()[0]
    failed = q("failed", "playlist_id", f"AND attempts >= {playlists.MAX_ATTEMPTS}")
    bad = health.failing()
    lines = [
        "📊 <b>Status</b>",
        f"<blockquote>Playlists checked {ago(health.LAST_OK.get('playlists'))}\n"
        f"Releases checked {ago(health.LAST_OK.get('releases'))}\n"
        f"Running for {ago(health.START).replace(' ago', '')}</blockquote>",
        f"🎵 {len(playlists.read_playlist_ids(ws))} playlists · {q('tracks', 'playlist_id')} songs sent",
        f"🎤 {len(releases.read_artists(ws))} artists · {q('releases', 'key')} new releases sent",
        f"🎧 {q('picks', 'key')} songs added from search",
    ]
    lines.append(f"{'⚠️' if failed else '✅'} {failed} songs gave up after repeated failures")
    if bad and is_owner_view(admin, ws):
        lines.append("⚠️ Having trouble: " + ", ".join(f"{esc(k.split(':', 1)[-1])} ×{v}" for k, v in list(bad.items())[:5]))
    for ts, what in list(health.RECENT)[:3]:
        lines.append(f"🕘 {ago(ts)}: {esc(what)}")
    rows = [[B("🩺  Check connections", "st:check")]]
    if admin:
        rows.append([B(f"🔁  Retry failed ({failed})", "st:retry", style="primary")])
    rows.append([B("‹ Menu", "m:main")])
    return "\n".join(lines), KB(rows)


def is_owner_view(admin: bool, ws: int) -> bool:
    return admin and ws == CHAT_ID  # server-wide problems are only shown for the owner's group


def help_text() -> str:
    return (
        "📝 <b>Lyrics</b>\n<blockquote>Lyrics are sent to you in private, so the group stays clean.</blockquote>\n"
        "• Open me in private and send a song name or the music file\n"
        "• Or reply to a song in the group with /lyrics (or the word متن)\n\n"
        "<b>Commands</b>\n/menu · /favorites · /search name · /playlists · /addplaylist link · /artists · /status · /lyrics\n"
        "In private you can also just type an artist or song name, or paste a Spotify playlist link."
    )


async def ask_lyrics(bot: Bot, m: Message) -> None:
    await tg_call(lambda: bot.send_message(m.chat.id, PROMPT_LYRICS, reply_markup=ForceReply()))


async def do_lyrics_search(bot: Bot, m: Message, query: str) -> None:
    import lyrics
    parts = re.split(r"\s+[-–—]\s+", query.strip(), maxsplit=1)
    if len(parts) == 2:  # "Artist - Title": go straight to the lyrics
        await lyrics.deliver(bot, None, parts[0], parts[1], dest=m.chat.id)
        return
    found = await lyrics.search_candidates(query.strip())
    if not found:
        await say(bot, m, f"No lyrics found for <b>{esc(query)}</b>.\nTry <code>Artist - Title</code>.",
                  KB([[B("‹ Menu", "m:main")]]))
        return
    token = secrets.token_hex(3)
    if len(LYR) > 100:
        LYR.clear()
    LYR[token] = found
    rows = [[B(f"📝  {cut(c['artist'] + ' — ' + c['title'], 44)}", f"lg:{token}:{i}")] for i, c in enumerate(found)]
    rows.append([B("‹ Menu", "m:main")])
    await say(bot, m, f"📝 <b>{esc(query)}</b>\n<blockquote>Pick the song to get its lyrics.</blockquote>", KB(rows))


async def recognize_clip(bot: Bot, m: Message) -> None:
    """Identify the song in a voice message / video / audio clip, then offer it like a search result."""
    import recognize
    if not recognize.available():
        return await say(bot, m, "Song recognition is not installed on this server yet (owner: run <code>sudo telspot update</code>).")
    f = m.voice or m.video_note or m.video or m.audio
    suffix = {"voice": ".ogg", "video_note": ".mp4", "video": ".mp4"}.get(
        "voice" if m.voice else "video_note" if m.video_note else "video" if m.video else "", ".mp3")
    wait = await say(bot, m, "🎧 Listening…")
    try:
        hit = await recognize.identify(bot, f.file_id, getattr(f, "file_size", 0) or 0, suffix)
    except ValueError:
        hit, err = None, "That file is too big (max 20 MB). Send a shorter clip."
    except Exception as e:
        log.warning("recognition failed: %s", e)
        hit, err = None, "Recognition failed right now. Try again in a minute."
    else:
        err = "I could not recognise that. Try a clearer, longer clip (10-20 s) with less noise, or type a lyric line."
    try:
        await bot.delete_message(m.chat.id, wait.message_id)
    except Exception:
        pass
    if not hit:
        return await say(bot, m, "❓ " + err)
    token = secrets.token_hex(3)
    if len(LYR) > 100:
        LYR.clear()
    LYR[token] = [dict(artist=hit["artist"], title=hit["title"], duration=0)]
    await say(bot, m, f"🎶 <b>{esc(hit['artist'])} — {esc(hit['title'])}</b>",
              KB([[B("📝  Lyrics", f"lg:{token}:0")]]))
    await do_search(bot, m, f"{hit['artist']} {hit['title']}".strip())


async def check_connections() -> list[str]:
    targets = {
        "Telegram": "https://api.telegram.org", "Spotify": "https://open.spotify.com",
        "YouTube": "https://www.youtube.com", "SoundCloud": "https://soundcloud.com",
        "Lyrics (LRCLIB)": "https://lrclib.net", "Deezer": "https://api.deezer.com/infos",
    }

    async def one(name: str, url: str) -> str:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    return f"{'✅' if r.status < 500 else '❌'} {name}"
        except Exception:
            return f"❌ {name}"

    out = list(await asyncio.gather(*(one(n, u) for n, u in targets.items())))
    try:  # can yt-dlp really search YouTube from this server?
        await asyncio.to_thread(playlists._yt_search, "music", 1)
        out.append("✅ YouTube search (yt-dlp)")
    except Exception as e:
        out.append(f"❌ YouTube search (yt-dlp): {esc(health.hint(str(e)) or cut(str(e), 80))}")
    return out


# ------------------------------------------------------------------ search
WEB: dict[str, dict] = {}  # YouTube video id -> song info, for the "add from YouTube" buttons
SEARCHQ: dict[str, str] = {}  # token -> artist name typed by the user (follow without Deezer)


def web_song_search(query: str) -> list[dict]:
    """Blocking: YouTube results that look like songs (used when Deezer has nothing, e.g. Persian names)."""
    out = []
    for e in playlists._yt_search(query, 10):
        dur = e.get("duration") or 0
        if not e.get("id") or not (60 <= dur <= 720):
            continue
        chan = re.sub(r"\s*-\s*topic$", "", (e.get("channel") or e.get("uploader") or ""), flags=re.I).strip()
        out.append({"yt": e["id"], "title": releases.clean_title(e.get("title") or "", chan) or e.get("title") or "?",
                    "artist": chan or "?", "duration": int(dur), "cover": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"})
        if len(out) >= 6:
            break
    return out


def is_non_latin(s: str) -> bool:
    return any(c.isalpha() and ord(c) > 0x24F for c in s)


async def do_search(bot: Bot, m: Message, query: str) -> None:
    uid = m.from_user.id
    ws = ws_of(m.chat, uid)
    manage = bool(ws) and await can_manage(bot, ws, uid)
    arts, trks = await asyncio.gather(music.search_artists(query, 4), music.search_tracks(query, 8))
    web: list[dict] = []
    # Deezer is weak for many Persian / non-Latin names: also look on YouTube directly
    if is_non_latin(query) or not arts or not trks:
        try:
            web = await asyncio.to_thread(web_song_search, query)
        except Exception as e:
            log.warning("web search failed: %s", e)
    if not arts and not trks and not web and not (len(query.split()) >= 3 and await music.search_by_lyrics(query)):
        await say(bot, m, f"Nothing found for <b>{esc(query)}</b>.", KB([[B("‹ Menu", "m:main")]]))
        return
    by_lyrics: list[dict] = []
    if len(query.split()) >= 3:  # probably a remembered line of lyrics
        by_lyrics = [t for t in await music.search_by_lyrics(query) if t["id"] not in {x["id"] for x in trks}]
        if not by_lyrics and not trks:
            try:
                web = web or await asyncio.to_thread(web_song_search, query + " lyrics")
            except Exception as e:
                log.warning("web lyrics search failed: %s", e)
    remember(trks + by_lyrics)
    rows = [[B(f"🎤  {cut(a['name'], 32)} · {a['fans']:,}", f"a:{a['id']}", style="primary")] for a in arts]
    rows += [
        song_row(f"▶️  {cut(t['artist'] + ' — ' + t['title'], 36)} · {mmss(t['duration'])}", f"t:{t['id']}", f"g:{t['id']}", manage)
        for t in trks
    ]
    if by_lyrics:
        rows += [song_row(f"🔤  {cut(t['artist'] + ' — ' + t['title'], 36)} · {mmss(t['duration'])}", f"t:{t['id']}", f"g:{t['id']}", manage)
                 for t in by_lyrics]
    if web:
        for w in web:
            WEB[w["yt"]] = w
        if len(WEB) > 500:
            for k in list(WEB)[:250]:
                WEB.pop(k, None)
        rows += [song_row(f"🌐  {cut(w['artist'] + ' — ' + w['title'], 34)} · {mmss(w['duration'])}", f"w:{w['yt']}", f"wg:{w['yt']}", manage) for w in web]
    if not arts and manage:
        token = secrets.token_hex(3)
        SEARCHQ[token] = query.strip()[:60]
        rows.append([B(f"⭐  Follow “{cut(query.strip(), 26)}” for new songs", f"fq:{token}", style="success")])
    rows.append([B("‹ Menu", "m:main")])
    notes = []
    if by_lyrics:
        notes.append("🔤 = matched by lyrics")
    if web:
        notes.append("🌐 = found on YouTube directly")
    notes.append("Tap a title to get the song here" + (" · 👥 adds it to the group" if manage else ""))
    note = "\n<blockquote>" + "\n".join(notes) + "</blockquote>"
    await say(bot, m, f"🔎 <b>{esc(query)}</b>{note}", KB(rows))


def artist_screen(a: dict, ws: int | None = None, manage: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    text = f"🎤 <b>{esc(a['name'])}</b>\n<blockquote>{a['fans']:,} fans · {a['albums']} albums</blockquote>"
    rows = []
    if manage and ws:
        fol = followed(a["name"], ws)
        rows.append([B("✅  Following" if fol else "⭐  Follow · auto-send new songs",
                       "noop" if fol else f"f:{a['id']}", style=None if fol else "success")])
    rows += [[B("🎵  Top songs", f"at:{a['id']}:0"), B("💿  Albums", f"ab:{a['id']}:0")],
             [B("‹ Menu", "m:main")]]
    return text, KB(rows)


def song_row(label: str, send: str, group: str, manage: bool = True) -> list[InlineKeyboardButton]:
    """Tap the title: get the song here. Tap 👥 (group managers only): add it to the group instead."""
    return [B(label, send), B("👥", group)] if manage else [B(label, send)]


def songs_page(title: str, tracks: list[dict], page: int, prefix: str, kind: str, ident: int,
               back: list[InlineKeyboardButton], manage: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, math.ceil(len(tracks) / PER_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = tracks[page * PER_PAGE:(page + 1) * PER_PAGE]
    remember(chunk)
    rows = [song_row(f"▶️  {cut(t['title'], 32)} · {mmss(t['duration'])}", f"t:{t['id']}", f"g:{t['id']}", manage) for t in chunk]
    if pages > 1:
        rows.append(nav(prefix, page, pages))
    if manage:
        rows.append([B("➕  All on this page", f"ap:{kind}:{ident}:{page}", style="success")])
        if kind == "al":
            rows.append([B(f"➕  Whole album ({len(tracks)})", f"ap:{kind}:{ident}:-1", style="success")])
    rows.append(back + [B("‹ Menu", "m:main")])
    hint = "Tap a song to get it here · 👥 adds it to the group." if manage else "Tap a song to get it here."
    return f"{title}\n<blockquote>{hint}</blockquote>", KB(rows)


# ------------------------------------------------------------ adding songs
async def add_song(bot: Bot, tr: dict, ws: int = CHAT_ID) -> tuple[str, str]:
    """Returns ('ok'|'dup'|'fail', detail)."""
    key = releases.release_key(tr["artist"], tr["title"])
    fkey = f"yt:{tr['yt']}" if tr.get("yt") else f"dz:{tr['id']}"
    skey = sk(ws, key)
    if db.execute("SELECT 1 FROM picks WHERE key=?", (skey,)).fetchone():
        return "dup", PICKS_TOPIC_NAME
    if db.execute("SELECT 1 FROM releases WHERE key=?", (skey,)).fetchone():
        return "dup", releases.RELEASES_TOPIC_NAME
    if skey in _adding:
        return "dup", "already being added"
    _adding.add(skey)
    try:
        cached = db.execute("SELECT file_id FROM files WHERE track_id=?", (fkey,)).fetchone()
        if cached:
            msg = await send_audio_topic(bot, ws, "picks_topic", PICKS_TOPIC_NAME, 0xFFD67E, cached[0], song_key=key)
        else:
            tmp = tempfile.mkdtemp(prefix="telspot_pick_")
            try:
                track = {"artist": tr["artist"], "title": tr["title"], "duration": tr["duration"]}
                async with dl_lock():
                    if tr.get("yt"):
                        path = await asyncio.to_thread(playlists.download_video, tr["yt"], tmp)
                    else:
                        path = await asyncio.to_thread(playlists.download_audio, track, tmp)
                img = None
                if tr.get("cover"):
                    async with aiohttp.ClientSession() as session:
                        img = await fetch_image(session, tr["cover"], max_bytes=200_000)
                msg = await send_audio_topic(
                    bot, ws, "picks_topic", PICKS_TOPIC_NAME, 0xFFD67E, FSInputFile(path), song_key=key,
                    title=tr["title"], performer=tr["artist"], duration=tr["duration"] or None,
                    thumbnail=BufferedInputFile(img, "cover.jpg") if img else None
                )
                db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (fkey, msg.audio.file_id))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        db.execute(
            "INSERT OR REPLACE INTO picks (key, dz_id, artist, title) VALUES (?,?,?,?)",
            (skey, tr.get("id") or 0, tr["artist"], tr["title"]),
        )
        db.commit()
        await health.ok(bot, "download", "downloads work again")
        return "ok", ""
    except Exception as e:
        log.warning("add song failed: %s - %s: %s", tr["artist"], tr["title"], e)
        await health.fail(bot, "download", "Downloads keep failing (songs added from the menu)", str(e), threshold=5)
        return "fail", cut(str(e), 120)
    finally:
        _adding.discard(skey)


async def send_here(bot: Bot, tr: dict, chat_id: int, thread: int | None = None) -> None:
    """Send the song to one chat only (the person's private chat). Nothing goes to the group."""
    fkey = f"yt:{tr['yt']}" if tr.get("yt") else f"dz:{tr['id']}"
    cached = db.execute("SELECT file_id FROM files WHERE track_id=?", (fkey,)).fetchone()
    if cached:
        await tg_call(lambda: bot.send_audio(chat_id, cached[0], message_thread_id=thread))
        return
    tmp = tempfile.mkdtemp(prefix="telspot_here_")
    try:
        try:
            async with dl_lock():
                if tr.get("yt"):
                    path = await asyncio.to_thread(playlists.download_video, tr["yt"], tmp)
                else:
                    path = await asyncio.to_thread(playlists.download_audio,
                                                   {"artist": tr["artist"], "title": tr["title"], "duration": tr["duration"]}, tmp)
        except Exception as e:
            await health.fail(bot, "download", "Downloads keep failing (songs from the menu)", str(e), threshold=5)
            raise
        img = None
        if tr.get("cover"):
            async with aiohttp.ClientSession() as session:
                img = await fetch_image(session, tr["cover"], max_bytes=200_000)
        msg = await tg_call(lambda: bot.send_audio(
            chat_id, FSInputFile(path), message_thread_id=thread, title=tr["title"], performer=tr["artist"],
            duration=tr["duration"] or None, thumbnail=BufferedInputFile(img, "cover.jpg") if img else None))
        db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (fkey, msg.audio.file_id))
        db.commit()
        await health.ok(bot, "download", "downloads work again")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def send_here_task(bot: Bot, cb: CallbackQuery, tr: dict) -> None:
    msg = cb.message
    private = msg.chat.type == "private"
    dest, thread = (msg.chat.id, None) if private else (cb.from_user.id, None)
    if not song_quota(cb.from_user.id):
        return await say(bot, msg, "⏳ You reached the hourly song limit. Try again a bit later.")
    try:
        await send_here(bot, tr, dest, thread)
    except TelegramForbiddenError:
        await say(bot, msg, "Open me in private and press Start once, then tap the song again. "
                  "Songs you only want for yourself are sent there.",
                  KB([[B("📩  Open the bot", url=f"https://t.me/{(await bot.me()).username}?start=menu", style="primary")]]))
    except Exception as e:
        log.warning("send here failed: %s", e)
        await say(bot, msg, f"❌ Could not get <b>{esc(tr['artist'])} — {esc(tr['title'])}</b>: {esc(cut(str(e), 120))}")


async def add_songs_task(bot: Bot, ctx: Message, tracks: list[dict], ws: int = CHAT_ID) -> None:
    ok = dup = fail = 0
    last = ("", "")
    for tr in tracks:
        status, detail = await add_song(bot, tr, ws)
        ok, dup, fail = ok + (status == "ok"), dup + (status == "dup"), fail + (status == "fail")
        last = (status, detail)
        if len(tracks) > 1:
            await asyncio.sleep(1.5)
    where = "this group" if ctx.chat.id == ws else "your group"
    if len(tracks) == 1:
        t = tracks[0]
        name = f"<b>{esc(t['artist'])} — {esc(t['title'])}</b>"
        if last[0] == "ok":
            text = f"✅ {name} → {esc(PICKS_TOPIC_NAME)} in {where}"
        elif last[0] == "dup":
            text = f"ℹ️ {name} is already in {esc(last[1])}"
        else:
            text = f"❌ Could not add {name}: {esc(last[1])}"
    else:
        text = f"✅ Added {ok} · already there {dup} · failed {fail} → {esc(PICKS_TOPIC_NAME)} in {where}"
    await say(bot, ctx, text)


# ---------------------------------------------------------------- playlists
async def add_playlist_from_text(bot: Bot, m: Message, text: str, ws: int = CHAT_ID) -> None:
    pid = playlists.parse_playlist_id(text)
    if not pid:
        await say(bot, m, "That does not look like a Spotify playlist link.")
        return
    have = playlists.read_playlist_ids(ws)
    if pid in have:
        await say(bot, m, "That playlist is already added.")
        return
    if len(have) >= limits(ws)[0]:
        await say(bot, m, f"❌ This group already has {len(have)} playlists (the limit). Remove one first.")
        return
    try:
        async with aiohttp.ClientSession() as session:
            data = await playlists.fetch_playlist(session, pid)
    except Exception as e:
        await say(bot, m, f"❌ Could not read it: {esc(str(e))}\nMake sure the playlist is <b>Public</b>.")
        return
    playlists.add_playlist_id(ws, pid, data["name"])
    note = "\n⚠️ Only the first ~100 songs of a playlist can be read." if len(data["tracks"]) >= 100 else ""
    await say(bot, m, f"✅ Added <b>{esc(data['name'])}</b> ({len(data['tracks'])} songs). "
                      f"Sending it to its own topic now…{note}")

    async def run() -> None:
        async with aiohttp.ClientSession() as session:
            try:
                await playlists.sync_playlist(bot, ws, pid, session)
            except Exception as e:
                log.error("sync after add failed for %s: %s", pid, e)
    spawn(run())


# ----------------------------------------------------------------- handlers
async def deny(m: Message, bot: Bot) -> None:
    rows = [[B("⭐  Join the group first", url=MAIN_LINK["url"], style="primary")]] if MAIN_LINK["url"] else []
    await say(bot, m, "To use me, first join the creator's music group.", KB(rows) if rows else None)


async def ctx(bot: Bot, chat, uid: int) -> tuple[int | None, bool]:
    """(group the menu works on, may this person change it?)"""
    ws = ws_of(chat, uid)
    return ws, bool(ws) and await can_manage(bot, ws, uid)


async def show_menu(bot: Bot, m: Message, uid: int, note: str = "") -> None:
    ws = ws_of(m.chat, uid)
    text, kb = await main_menu(bot, ws, uid, m.chat.type == "private")
    await say(bot, m, text + note, kb)


async def no_group(bot: Bot, m: Message) -> None:
    await say(bot, m, "📁 You have no group connected yet. Add me to your own supergroup (with Topics) to use this.",
              KB([[B("➕  Connect my own group", url=await connect_url(bot), style="success")]]))


async def no_rights(bot: Bot, m: Message, ws: int | None) -> None:
    if ws == CHAT_ID:
        return await say(bot, m, "This is the creator's group: only the creator manages it. "
                                 "Connect your own group to use your own playlists.")
    await say(bot, m, "Only the group owner or its admins can change this.")


@router.message(ALLOWED_MSG, Command("menu", "start", "help"))
async def cmd_menu(m: Message, command: CommandObject, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    arg = (command.args or "").strip()
    if m.chat.type == "private" and arg == "lyrics":
        return await ask_lyrics(bot, m)
    await show_menu(bot, m, m.from_user.id, "\n\n" + help_text())


@router.message(ALLOWED_MSG, Command("search", "s", "artist", "follow"))
async def cmd_search(m: Message, command: CommandObject, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if command.args:
        await do_search(bot, m, command.args.strip())
    else:
        await tg_call(lambda: bot.send_message(m.chat.id, PROMPT_SEARCH, message_thread_id=_thread(m),
                                               reply_markup=ForceReply(selective=True),
                                               reply_parameters=None))


@router.message(ALLOWED_MSG, Command("playlists"))
async def cmd_playlists(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    ws, manage = await ctx(bot, m.chat, m.from_user.id)
    if not ws:
        return await no_group(bot, m)
    text, kb = playlists_screen(manage, ws)
    await say(bot, m, text, kb)


@router.message(ALLOWED_MSG, Command("favorites", "fav"))
async def cmd_favorites(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    ws, _ = await ctx(bot, m.chat, m.from_user.id)
    if not ws:
        return await no_group(bot, m)
    text, kb = favorites_screen(ws)
    await say(bot, m, text, kb)


@router.message(ALLOWED_MSG, Command("status"))
async def cmd_status(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    ws, manage = await ctx(bot, m.chat, m.from_user.id)
    if not ws:
        return await no_group(bot, m)
    text, kb = status_screen(manage, ws)
    await say(bot, m, text, kb)


@router.message(ALLOWED_MSG, Command("artists"))
async def cmd_artists(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    ws, manage = await ctx(bot, m.chat, m.from_user.id)
    if not ws:
        return await no_group(bot, m)
    text, kb = artists_screen(manage, ws)
    await say(bot, m, text, kb)


@router.message(F.chat.type == "private", Command("settings"))
async def cmd_settings(m: Message, bot: Bot) -> None:
    if not is_owner(m.from_user.id):
        return
    text, kb = settings_screen()
    await say(bot, m, text, kb)


@router.message(F.chat.type == "private", Command("groups", "mygroups"))
async def cmd_groups(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if not user_ws(m.from_user.id):
        return await no_group(bot, m)
    text, kb = switch_screen(m.from_user.id)
    await say(bot, m, text, kb)


@router.message(ALLOWED_MSG, Command("addplaylist", "add"))
async def cmd_addplaylist(m: Message, command: CommandObject, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    ws, manage = await ctx(bot, m.chat, m.from_user.id)
    if not ws:
        return await no_group(bot, m)
    if not manage:
        return await no_rights(bot, m, ws)
    if command.args:
        await add_playlist_from_text(bot, m, command.args, ws)
    else:
        await tg_call(lambda: bot.send_message(m.chat.id, PROMPT_PLAYLIST, message_thread_id=_thread(m),
                                               reply_markup=ForceReply(selective=True)))


async def is_prompt_reply(m: Message, bot: Bot) -> bool:
    r = m.reply_to_message
    return bool(r and r.text in PROMPTS and r.from_user and r.from_user.id == await my_id(bot))


@router.message(ALLOWED_MSG, F.text, is_prompt_reply)
async def on_prompt_reply(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if m.reply_to_message.text == PROMPT_LYRICS:
        return await do_lyrics_search(bot, m, m.text)
    if m.reply_to_message.text == PROMPT_PLAYLIST:
        ws, manage = await ctx(bot, m.chat, m.from_user.id)
        if not manage:
            return await no_rights(bot, m, ws)
        await add_playlist_from_text(bot, m, m.text, ws)
    else:
        await do_search(bot, m, m.text.strip())


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def on_private_text(m: Message, bot: Bot) -> None:
    """In a private chat: a playlist link adds the playlist to your group, anything else is a search."""
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if playlists.parse_playlist_id(m.text):
        ws, manage = await ctx(bot, m.chat, m.from_user.id)
        if not ws:
            return await no_group(bot, m)
        if not manage:
            return await no_rights(bot, m, ws)
        await add_playlist_from_text(bot, m, m.text, ws)
    else:
        await do_search(bot, m, m.text.strip())


CB_PATTERN = r"^(m|pl|ar|a|at|ab|al|ap|t|g|w|wg|lg|f|fq|fs|fo|fc|st|sw|cfg|noop)(:|$)"


@router.callback_query(ALLOWED_CB, F.data.regexp(CB_PATTERN))
async def on_callback(cb: CallbackQuery, bot: Bot) -> None:
    msg = cb.message
    if msg is None:
        return await cb.answer()
    uid = cb.from_user.id
    if not await can_use(bot, msg.chat, uid):
        return await cb.answer("Join the creator's group first.", show_alert=True)
    p = (cb.data or "").split(":")
    cmd = p[0] if len(p) == 1 or p[0] not in ("pl", "ar", "st", "cfg") else f"{p[0]}:{p[1]}"
    ws, manage = await ctx(bot, msg.chat, uid)
    ADMIN_ONLY = {"pl:add", "pl:rm", "ar:rm", "st:retry", "f", "fq", "fs", "fo", "ap", "g", "wg"}
    if cmd in ADMIN_ONLY and not manage:
        return await cb.answer("Only the group owner or admins can do that." if ws else "Connect your own group first.",
                               show_alert=True)
    if cmd.startswith("cfg") or (cmd == "m" and len(p) > 1 and p[1] == "cf"):
        if not is_owner(uid):
            return await cb.answer("Only the owner can do that.", show_alert=True)
        if cmd == "cfg:rj":
            set_setting("require_join", "0" if require_join() else "1")
            await cb.answer("Saved")
        else:
            await cb.answer()
        text, kb = settings_screen()
        return await edit(cb, text, kb)
    if cmd == "sw":
        if int(p[1]) in user_ws(uid):
            set_active_ws(uid, int(p[1]))
        await cb.answer("Switched")
        text, kb = await main_menu(bot, ws_of(msg.chat, uid), uid, msg.chat.type == "private")
        return await edit(cb, text, kb)

    if cmd == "noop":
        return await cb.answer()
    if cmd == "m":
        await cb.answer()
        what = p[1]
        if what == "main":
            text, kb = await main_menu(bot, ws, uid, msg.chat.type == "private")
            return await edit(cb, text, kb)
        if what == "sw":
            text, kb = switch_screen(uid)
            return await edit(cb, text, kb)
        if what in ("fv", "pl", "ar", "st") and not ws:
            return await no_group(bot, msg)
        if what == "fv":
            text, kb = favorites_screen(ws)
            return await edit(cb, text, kb)
        if what == "pl":
            text, kb = playlists_screen(manage, ws)
            return await edit(cb, text, kb)
        if what == "ar":
            text, kb = artists_screen(manage, ws)
            return await edit(cb, text, kb)
        if what == "ly":
            if msg.chat.type == "private":
                return await ask_lyrics(bot, msg)
            link = f"https://t.me/{(await bot.me()).username}?start=lyrics"
            return await edit(cb, help_text(), KB([[B("📩  Open the bot for lyrics", url=link, style="primary")],
                                                   [B("‹ Menu", "m:main")]]))
        if what == "st":
            text, kb = status_screen(manage, ws)
            return await edit(cb, text, kb)
        if what == "se":
            return await tg_call(lambda: bot.send_message(
                msg.chat.id, PROMPT_SEARCH, message_thread_id=_thread(msg), reply_markup=ForceReply()))
    if cmd == "lg":
        import lyrics
        lst = LYR.get(p[1])
        c = lst[int(p[2])] if lst and int(p[2]) < len(lst) else None
        if not c:
            return await cb.answer("Search again, this result expired.", show_alert=True)
        try:
            await cb.answer("📝 Finding lyrics…")
            return await lyrics.deliver(bot, None, c["artist"], c["title"], c["duration"], dest=uid)
        except Exception as e:
            log.warning("lyrics delivery failed: %s", e)
            return
    if cmd == "st:retry":
        cond, args = scope_where(ws, "playlist_id")
        n = db.execute(f"SELECT COUNT(*) FROM failed WHERE {cond}", args).fetchone()[0]
        db.execute(f"DELETE FROM failed WHERE {cond}", args)
        db.commit()
        await cb.answer(f"🔁 Retrying {n} song(s)…")

        async def again() -> None:
            await playlists.sync_all(bot)
        spawn(again())
        text, kb = status_screen(True, ws)
        return await edit(cb, text, kb)
    if cmd == "st:check":
        await cb.answer("🩺 Checking…")
        await edit(cb, "🩺 Checking connections…")
        results = await check_connections()
        text, kb = status_screen(manage, ws)
        return await edit(cb, "🩺 <b>Connections</b>\n" + "\n".join(results) + "\n\n" + text, kb)
    if cmd == "pl:add":
        await cb.answer()
        return await tg_call(lambda: bot.send_message(
            msg.chat.id, PROMPT_PLAYLIST, message_thread_id=_thread(msg), reply_markup=ForceReply()))
    if cmd == "pl:rm":
        playlists.remove_playlist_id(ws, p[2])
        await cb.answer("Removed. Songs already in Telegram stay.")
        text, kb = playlists_screen(True, ws)
        return await edit(cb, text, kb)
    if cmd == "ar:rm":
        arts = [a for a in releases.read_artists(ws) if releases.artist_hash(a.name) != p[2]]
        releases.write_artists(arts, ws)
        await cb.answer("Unfollowed.")
        text, kb = artists_screen(True, ws)
        return await edit(cb, text, kb)

    if cmd == "a":
        a = await music.artist(int(p[1]))
        if not a:
            return await cb.answer("Artist not found.", show_alert=True)
        await cb.answer()
        text, kb = artist_screen(a, ws, manage)
        return await edit(cb, text, kb)
    if cmd in ("at", "ab", "al"):
        await cb.answer()
        ident, page = int(p[1]), int(p[2])
        if cmd == "at":
            a, tracks = await music.artist(ident), await music.artist_top(ident)
            title = f"🎵 <b>{esc(a['name'] if a else '')}</b> — top songs"
            text, kb = songs_page(title, tracks, page, f"at:{ident}", "at", ident, [B("‹ Artist", f"a:{ident}")], manage)
            return await edit(cb, text, kb)
        if cmd == "ab":
            a, albums = await music.artist(ident), await music.artist_albums(ident)
            pages = max(1, math.ceil(len(albums) / PER_PAGE))
            page = min(max(page, 0), pages - 1)
            rows = [[B(f"💿 {al['year']} · {cut(al['title'], 38)} ({al['type']})", f"al:{al['id']}:0")]
                    for al in albums[page * PER_PAGE:(page + 1) * PER_PAGE]]
            if pages > 1:
                rows.append(nav(f"ab:{ident}", page, pages))
            rows.append([B("‹ Artist", f"a:{ident}"), B("‹ Menu", "m:main")])
            return await edit(cb, f"💿 <b>{esc(a['name'] if a else '')}</b> — albums & singles", KB(rows))
        al = await music.album(ident)
        if not al:
            return await cb.answer("Album not found.", show_alert=True)
        title = f"💿 <b>{esc(al['title'])}</b> ({al['year']}) — {esc(al['artist'])}"
        back = [B("‹ Albums", f"ab:{al['artist_id']}:0")] if al.get("artist_id") else []
        text, kb = songs_page(title, al["tracks"], page, f"al:{ident}", "al", ident, back, manage)
        return await edit(cb, text, kb)
    if cmd == "t":
        tr = TRK.get(int(p[1])) or await music.track(int(p[1]))
        if not tr:
            return await cb.answer("Song not found.", show_alert=True)
        await cb.answer("⏳ Getting it for you…")
        return spawn(send_here_task(bot, cb, tr))
    if cmd == "g":
        tr = TRK.get(int(p[1])) or await music.track(int(p[1]))
        if not tr:
            return await cb.answer("Song not found.", show_alert=True)
        await cb.answer("⏳ Adding… I'll tell you when it's in the group.")
        return spawn(add_songs_task(bot, msg, [tr], ws))
    if cmd == "ap":  # ap:<kind>:<id>:<page>   (page -1 = whole album)
        kind, ident, page = p[1], int(p[2]), int(p[3])
        if kind == "at":
            tracks = await music.artist_top(ident)
        else:
            al = await music.album(ident)
            tracks = al["tracks"] if al else []
        if page >= 0:
            tracks = tracks[page * PER_PAGE:(page + 1) * PER_PAGE]
        tracks = tracks[:MAX_ADD_ALL]
        if not tracks:
            return await cb.answer("Nothing to add.", show_alert=True)
        await cb.answer(f"⏳ Adding {len(tracks)} songs…")
        return spawn(add_songs_task(bot, msg, tracks, ws))

    # ---- follow an artist: pick where to watch, then save
    if cmd in ("f", "fq"):
        if cmd == "f":
            a = await music.artist(int(p[1]))
            name = a["name"] if a else None
        else:
            name = SEARCHQ.get(p[1])
        if not name:
            return await cb.answer("Artist not found. Search again.", show_alert=True)
        await cb.answer("🔎 Looking for channels…")
        await edit(cb, f"⏳ Looking for <b>{esc(name)}</b> on YouTube and SoundCloud…")
        cands = await asyncio.to_thread(releases.follow_candidates, name)
        for k in [k for k, v in PENDING.items() if time.time() - v["ts"] > 1800]:
            PENDING.pop(k, None)
        token = secrets.token_hex(3)
        PENDING[token] = {"name": name, "cands": cands, "ts": time.time()}
        text, kb = follow_screen(token)
        return await edit(cb, text, kb)
    if cmd in ("w", "wg"):
        w = WEB.get(p[1])
        if not w:
            return await cb.answer("Search again, this result expired.", show_alert=True)
        await cb.answer("⏳ Getting it for you…" if cmd == "w" else "⏳ Adding… I'll tell you when it's in the group.")
        return spawn(send_here_task(bot, cb, w) if cmd == "w" else add_songs_task(bot, msg, [w], ws))
    if cmd in ("fs", "fo", "fc"):
        st = PENDING.get(p[1])
        if not st:
            return await cb.answer("This dialog expired. Open the artist again.", show_alert=True)
        if cmd == "fc":
            PENDING.pop(p[1], None)
            await cb.answer("Cancelled.")
            text, kb = await main_menu(bot, ws, uid, msg.chat.type == "private")
            return await edit(cb, text, kb)
        if cmd == "fs":
            c = st["cands"][int(p[2])]
            c["on"] = not c["on"]
            await cb.answer()
            text, kb = follow_screen(p[1])
            return await edit(cb, text, kb)
        picks = [(c["platform"], c["id"], c["label"]) for c in st["cands"] if c["on"]]
        if not picks:
            return await cb.answer("Select at least one source.", show_alert=True)
        arts = releases.read_artists(ws)
        if len(arts) >= limits(ws)[1] and not followed(st["name"], ws):
            return await cb.answer(f"Limit reached: {limits(ws)[1]} artists per group.", show_alert=True)
        releases.add_artist(arts, st["name"], picks, save=True, chat_id=ws)
        PENDING.pop(p[1], None)
        await cb.answer("Saved!")
        return await edit(
            cb,
            f"⭐ Following <b>{esc(st['name'])}</b> on {len(picks)} source(s).\n"
            f"New songs will appear in <b>{esc(releases.RELEASES_TOPIC_NAME)}</b> "
            "(songs that already exist are ignored; checks run every few minutes).",
            KB([[B("‹ Menu", "m:main")]]),
        )
    await cb.answer()


def follow_screen(token: str) -> tuple[str, InlineKeyboardMarkup]:
    st = PENDING[token]
    head = (f"⭐ <b>{esc(st['name'])}</b>\n<blockquote>Where should I watch for new songs? "
            "Tap to toggle, then Save. An “- Topic” YouTube channel is usually the fastest.</blockquote>")
    if not st["cands"]:
        return head + "\n\nI could not find channels automatically.", KB([[B("✖  Cancel", f"fc:{token}", style="danger")]])
    rows = []
    for i, c in enumerate(st["cands"]):
        site = "YouTube" if c["platform"] == "yt" else "SoundCloud"
        rows.append([B(f"{'✅' if c['on'] else '⬜'}  {site} · {cut(c['label'], 38)}", f"fs:{token}:{i}")])
    rows.append([B("💾  Save", f"fo:{token}", style="success"), B("✖  Cancel", f"fc:{token}", style="danger")])
    return head, KB(rows)
