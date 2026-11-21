"""
New-release monitor: watches artists on YouTube (channel RSS) and SoundCloud (yt-dlp) and
sends each new track to one Telegram topic as soon as ANY platform shows it first.
Spotify is intentionally not used (it is usually the slowest and has no public feed).

Artists live in artists.txt, one per line (managed from the menu: `telspot artists`):
    Kendrick Lamar | yt:UC... | yt:UC... | sc:kendrick-lamar-official  # notes
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, FSInputFile

import health
from core import (
    artists_file, ws_ids, ws_owner, ws_title, sk, AUDIO_FMT_SC, AUDIO_FMT_YT, CHAT_ID, HEADERS, RELEASE_INTERVAL,
    RELEASE_MAX_AGE_HOURS, RELEASE_MAX_ATTEMPTS, RELEASE_MAX_SECONDS, RELEASE_MIN_SECONDS,
    RELEASES_TOPIC_NAME, db, dl_lock, download_url, fetch_image, log, send_audio_topic,
    topic_id_for, ytdlp_opts,
)

YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
PLATFORM_NAME = {"yt": "YouTube", "sc": "SoundCloud"}


# ------------------------------------------------------------- artists file
@dataclass
class Artist:
    name: str
    yt: list[str] = field(default_factory=list)  # YouTube channel ids (UC...)
    sc: list[str] = field(default_factory=list)  # SoundCloud profile permalinks
    note: str = ""


def read_artists(chat_id: int = CHAT_ID) -> list[Artist]:
    p = Path(artists_file(chat_id))
    if not p.exists():
        return []
    out: list[Artist] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        note = ""
        if " #" in line:
            line, note = line.split(" #", 1)
        parts = [x.strip() for x in line.split("|")]
        art = Artist(parts[0], note=note.strip())
        for part in parts[1:]:
            kind, _, val = part.partition(":")
            kind, val = kind.strip().lower(), val.strip().strip("/")
            if kind == "yt" and val:
                art.yt.append(val)
            elif kind == "sc" and val:
                art.sc.append(val)
        if art.name and (art.yt or art.sc):
            out.append(art)
    return out


def drop_source(name: str, platform: str, sid: str, chat_id: int = CHAT_ID) -> None:
    arts = read_artists(chat_id)
    for a in arts:
        if a.name == name:
            lst = a.yt if platform == "yt" else a.sc
            if sid in lst:
                lst.remove(sid)
    write_artists([a for a in arts if a.yt or a.sc], chat_id)


def write_artists(artists: list[Artist], chat_id: int = CHAT_ID) -> None:
    lines = ["# Tel Spot artists: Name | yt:<channel id> | sc:<soundcloud name>   (edit with: telspot artists)"]
    for a in artists:
        parts = [a.name] + [f"yt:{c}" for c in a.yt] + [f"sc:{s}" for s in a.sc]
        line = " | ".join(parts)
        if a.note:
            line += f"  # {a.note}"
        lines.append(line)
    Path(artists_file(chat_id)).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------- title handling
_NOISE = (
    r"official|video|audio|music|lyrics?|lyric video|visuali[sz]er|hd|4k|explicit|clean|"
    r"full|version|m/?v|out now|new"
)
_NOISE_BRACKET = re.compile(r"[\(\[]\s*(?:%s)(?:[\s,&+/|-]+(?:%s))*\s*[\)\]]" % (_NOISE, _NOISE), re.I)
_FEAT_BRACKET = re.compile(r"[\(\[]\s*(?:feat|ft|featuring|with|prod)\b[^\)\]]*[\)\]]", re.I)
_FEAT_TAIL = re.compile(r"\s+(?:feat|ft|featuring)\.?\s+.*$", re.I)
_BLOCK = re.compile(
    r"\b(teaser|snippet|trailer|preview|interview|reaction|behind the scenes|making of|podcast|"
    r"vlog|shorts?|full album|livestream|live stream|tour|mix)\b"
    r"|\blive (?:at|from|in|on|session|performance|version)\b|[\(\[]\s*live\s*[\)\]]",
    re.I,
)


def clean_title(title: str, artist: str) -> str:
    t = _NOISE_BRACKET.sub("", title or "")
    t = re.sub(r"^\s*%s\s*[-–—:|]\s*" % re.escape(artist), "", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(" -–—|")


def release_key(artist: str, title: str) -> str:
    """Platform-independent identity of a song, used to skip the copy that shows up later."""
    t = _FEAT_BRACKET.sub("", clean_title(title, artist))
    t = _FEAT_TAIL.sub("", t)
    t = unicodedata.normalize("NFKC", t).lower()
    return re.sub(r"\W+", "", artist.lower()) + ":" + re.sub(r"\W+", "", t)


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- sources
def parse_yt_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    channel = (root.findtext("a:title", namespaces=NS) or "").strip()
    items = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        if not vid:
            continue
        items.append(
            {
                "platform": "yt",
                "id": vid,
                "title": (e.findtext("a:title", namespaces=NS) or "").strip(),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "published": parse_dt(e.findtext("a:published", namespaces=NS)),
                "channel": channel,
                "topic": channel.endswith("- Topic"),  # auto-generated audio channel
            }
        )
    return items


async def fetch_yt(session: aiohttp.ClientSession, channel_id: str) -> list[dict]:
    url = YT_FEED.format(channel_id)
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            raise RuntimeError(f"YouTube feed HTTP {r.status}")
        body = await r.read()
    return parse_yt_feed(body)


def fetch_sc_sync(permalink: str, limit: int = 6) -> list[dict]:
    """Blocking: newest tracks of a SoundCloud profile (ids and urls only)."""
    opts = ytdlp_opts(extract_flat="in_playlist", playlistend=limit)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://soundcloud.com/{permalink}/tracks", download=False)
    items = []
    for e in (info or {}).get("entries") or []:
        url = e.get("url") or e.get("webpage_url")
        if not url:
            continue
        items.append(
            {
                "platform": "sc", "id": str(e.get("id") or url), "title": e.get("title") or "",
                "url": url, "published": None, "channel": permalink, "topic": False,
            }
        )
    return items


def fetch_info_sync(url: str) -> dict:
    with yt_dlp.YoutubeDL(ytdlp_opts(noplaylist=True)) as ydl:
        return ydl.extract_info(url, download=False)


# ---------------------------------------------------------------- filters
def _info_time(info: dict) -> datetime | None:
    ts = info.get("release_timestamp") or info.get("timestamp")
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def reject_reason(item: dict, info: dict, now: datetime | None = None) -> str | None:
    """Why this upload is not a new song release (None = accept)."""
    now = now or datetime.now(timezone.utc)
    dur = info.get("duration") or 0
    if dur and dur < RELEASE_MIN_SECONDS:
        return f"too short ({dur}s)"
    if dur and dur > RELEASE_MAX_SECONDS:
        return f"too long ({dur}s)"
    title = info.get("title") or item.get("title") or ""
    if not item.get("topic") and _BLOCK.search(title):
        return "looks like a teaser/live/non-song upload"
    pub = item.get("published") or _info_time(info)
    if pub and now - pub > timedelta(hours=RELEASE_MAX_AGE_HOURS):
        return f"older than {RELEASE_MAX_AGE_HOURS}h"
    return None


# ----------------------------------------------------------------- state
def is_seen(chat_id: int, iid: str) -> bool:
    return db.execute("SELECT 1 FROM seen WHERE item=?", (sk(chat_id, iid),)).fetchone() is not None


def mark_seen(chat_id: int, iid: str) -> None:
    db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (sk(chat_id, iid),))
    db.execute("DELETE FROM attempts WHERE item=?", (sk(chat_id, iid),))
    db.commit()


def bump_attempts(chat_id: int, iid: str) -> int:
    k = sk(chat_id, iid)
    db.execute("INSERT OR IGNORE INTO attempts VALUES (?, 0)", (k,))
    db.execute("UPDATE attempts SET n = n + 1 WHERE item=?", (k,))
    db.commit()
    return db.execute("SELECT n FROM attempts WHERE item=?", (k,)).fetchone()[0]


async def ensure_releases_topic(bot: Bot, chat_id: int = CHAT_ID, force_new: bool = False) -> int:
    return await topic_id_for(bot, chat_id, "releases_topic", RELEASES_TOPIC_NAME, 0xFB6F5F, force_new)


def _thumb_url(item: dict, info: dict) -> str | None:
    if item["platform"] == "yt":
        return f"https://i.ytimg.com/vi/{item['id']}/mqdefault.jpg"  # 320x180, within Telegram's limit
    thumb = info.get("thumbnail")
    return re.sub(r"-(?:large|t500x500|original|crop)\.", "-t300x300.", thumb) if thumb else None


async def send_release(
    bot: Bot, session: aiohttp.ClientSession, chat_id: int, artist: Artist, item: dict, info: dict, title: str
) -> None:
    """Post one release in a group. The file is downloaded once; other groups reuse Telegram's file id."""
    fkey = f"rel:{item['platform']}:{item['id']}"
    song_key = release_key(artist.name, title)
    caption = f"🆕 {artist.name} — {title}\n{PLATFORM_NAME[item['platform']]} · {item['url']}"
    cached = db.execute("SELECT file_id FROM files WHERE track_id=?", (fkey,)).fetchone()
    tmp = tempfile.mkdtemp(prefix="telspot_rel_")
    try:
        if cached:
            await send_audio_topic(bot, chat_id, "releases_topic", RELEASES_TOPIC_NAME, 0xFB6F5F, cached[0],
                                   song_key=song_key, title=title, performer=artist.name, caption=caption)
        else:
            fmt = AUDIO_FMT_YT if item["platform"] == "yt" else AUDIO_FMT_SC
            async with dl_lock():
                path, dinfo = await asyncio.to_thread(download_url, item["url"], tmp, fmt)
            turl = _thumb_url(item, dinfo)
            img = await fetch_image(session, turl, max_bytes=200_000) if turl else None
            thumb = BufferedInputFile(img, "thumb.jpg") if img else None
            duration = int(dinfo.get("duration") or info.get("duration") or 0) or None
            msg = await send_audio_topic(
                bot, chat_id, "releases_topic", RELEASES_TOPIC_NAME, 0xFB6F5F, FSInputFile(path), title=title,
                performer=artist.name, duration=duration, thumbnail=thumb, caption=caption, song_key=song_key
            )
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (fkey, msg.audio.file_id))
        db.execute(
            "INSERT OR REPLACE INTO releases (key, artist, title, platform, url) VALUES (?,?,?,?,?)",
            (sk(chat_id, song_key), artist.name, title, item["platform"], item["url"]),
        )
        db.commit()
        log.info("NEW RELEASE sent to %s: %s - %s (%s)", chat_id, artist.name, title, PLATFORM_NAME[item["platform"]])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_info_cache: dict[str, dict] = {}


async def item_info(url: str) -> dict:
    if url not in _info_cache:
        if len(_info_cache) > 200:
            _info_cache.clear()
        _info_cache[url] = await asyncio.to_thread(fetch_info_sync, url)
    return _info_cache[url]


async def process_item(bot: Bot, session: aiohttp.ClientSession, chat_id: int, artist: Artist, item: dict) -> None:
    iid = f"{item['platform']}:{item['id']}"
    try:
        info = await item_info(item["url"])
        if info.get("live_status") in ("is_live", "is_upcoming"):
            _info_cache.pop(item["url"], None)
            raise RuntimeError("live or premiere not started yet")
        reason = reject_reason(item, info)
        if reason:
            log.info("skip '%s' (%s): %s", item["title"], artist.name, reason)
            mark_seen(chat_id, iid)
            return
        title = clean_title(info.get("title") or item["title"], artist.name) or item["title"]
        key = release_key(artist.name, title)
        if db.execute("SELECT 1 FROM releases WHERE key=?", (sk(chat_id, key),)).fetchone():
            log.info("duplicate, already sent from another platform: %s - %s", artist.name, title)
            mark_seen(chat_id, iid)
            return
        await send_release(bot, session, chat_id, artist, item, info, title)
        await health.ok(bot, "download", "downloads work again")
        mark_seen(chat_id, iid)
        await asyncio.sleep(1)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        n = bump_attempts(chat_id, iid)
        log.warning("release '%s' could not be posted in %s (%d/%d): %s", item["title"], chat_id, n, RELEASE_MAX_ATTEMPTS, e)
        if chat_id != CHAT_ID and any(s in str(e).lower() for s in ("kicked", "chat not found", "forbidden", "not a member")):
            from playlists import _group_gone
            await _group_gone(bot, chat_id, e)
        if n >= RELEASE_MAX_ATTEMPTS:
            mark_seen(chat_id, iid)
    except Exception as e:
        n = bump_attempts(chat_id, iid)
        log.warning("release '%s' failed (%d/%d): %s", item["title"], n, RELEASE_MAX_ATTEMPTS, e)
        await health.fail(bot, "download", "Downloads keep failing (new releases)", str(e), threshold=5)
        if n >= RELEASE_MAX_ATTEMPTS:
            mark_seen(chat_id, iid)


# ------------------------------------------------------------------ loop
async def release_cycle(bot: Bot, session: aiohttp.ClientSession) -> None:
    per_ws = {cid: read_artists(cid) for cid in ws_ids()}
    per_ws = {cid: arts for cid, arts in per_ws.items() if arts}
    if not per_ws:
        return
    sem = asyncio.Semaphore(4)
    # every distinct source is fetched once, even if many groups follow the same artist
    sources: dict[tuple[str, str], list] = {}
    for cid, arts in per_ws.items():
        for a in arts:
            for sid in a.yt:
                sources.setdefault(("yt", sid), [])
            for sid in a.sc:
                if sid not in ("tracks", "users"):
                    sources.setdefault(("sc", sid), [])
        for a in arts:  # earlier versions saved a wrong profile name "tracks"
            if "tracks" in a.sc or "users" in a.sc:
                drop_source(a.name, "sc", "tracks", cid); drop_source(a.name, "sc", "users", cid)

    async def fetch(platform: str, sid: str):
        async with sem:
            try:
                items = await (fetch_yt(session, sid) if platform == "yt" else asyncio.to_thread(fetch_sc_sync, sid))
                await health.ok(bot, f"src:{platform}:{sid}", f"{PLATFORM_NAME[platform]} source {sid} works again")
                return platform, sid, items
            except Exception as e:
                log.warning("%s %s: %s", PLATFORM_NAME[platform], sid, e)
                err = str(e)
                if platform == "sc" and ("404" in err or "not found" in err.lower()):
                    if await health.fail(bot, f"src:sc:{sid}", f"SoundCloud profile “{sid}” does not exist", err, threshold=5):
                        for cid, arts in per_ws.items():
                            for a in arts:
                                if sid in a.sc:
                                    drop_source(a.name, "sc", sid, cid)
                                    await health.alert(bot, f"🧹 Removed the broken SoundCloud source “{sid}” of {a.name}. "
                                                       "Follow the artist again from /search to add the right profile.",
                                                       to=ws_owner(cid) or None)
                else:
                    await health.fail(bot, f"src:{platform}:{sid}", f"Cannot check {PLATFORM_NAME[platform]} source {sid}", err, threshold=5)
                return platform, sid, None

    fetched = {(p, s): items for p, s, items in await asyncio.gather(*(fetch(p, s) for p, s in sources))}

    candidates: list[tuple[int, Artist, dict]] = []
    for cid, arts in per_ws.items():
        for artist in arts:
            for platform, sid in [("yt", s) for s in artist.yt] + [("sc", s) for s in artist.sc]:
                items = fetched.get((platform, sid))
                if items is None:
                    continue
                src = sk(cid, f"{platform}:{sid}")
                if db.execute("SELECT 1 FROM source_state WHERE src=?", (src,)).fetchone() is None:
                    # first time this group sees this source: remember what exists, send nothing
                    for it in items:
                        db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (sk(cid, f"{it['platform']}:{it['id']}"),))
                    db.execute("INSERT INTO source_state VALUES (?)", (src,))
                    db.commit()
                    log.info("watching %s on %s for %s (%d existing uploads ignored)", artist.name,
                             PLATFORM_NAME[platform], cid, len(items))
                    continue
                candidates += [(cid, artist, it) for it in items if not is_seen(cid, f"{it['platform']}:{it['id']}")]

    health.mark_cycle("releases")
    far = datetime.max.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda c: c[2]["published"] or far)  # oldest first
    for cid, artist, item in candidates:
        if cid in ws_ids():  # a group may have been disconnected meanwhile
            await process_item(bot, session, cid, artist, item)


async def release_loop(bot: Bot) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await release_cycle(bot, session)
            except Exception:
                log.exception("release cycle failed")
            await asyncio.sleep(RELEASE_INTERVAL)


# ============================================================ artist CLI
def _norm(s: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", s or "").lower())


def _flat_search(query: str) -> list[dict]:
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts(extract_flat=True)) as ydl:
            return (ydl.extract_info(query, download=False) or {}).get("entries") or []
    except Exception as e:
        print(f"  (search failed: {e})")
        return []


def search_youtube_channels(name: str) -> list[dict]:
    found: dict[str, dict] = {}
    for q in (name, f"{name} topic", f"{name} official"):
        for e in _flat_search(f"ytsearch10:{q}"):
            cid, title = e.get("channel_id"), e.get("channel") or e.get("uploader")
            if not cid or not title:
                continue
            c = found.setdefault(cid, {"id": cid, "label": title, "hits": 0})
            c["hits"] += 1
    n = _norm(name)
    for c in found.values():
        c["score"] = c["hits"] + (5 if n and n in _norm(c["label"]) else 0)
    return sorted(found.values(), key=lambda c: -c["score"])[:8]


def _sc_permalink(e: dict) -> str | None:
    """Profile permalink of a SoundCloud search hit. The hit's own `url` is an API address
    (api.soundcloud.com/tracks/123), so the uploader's profile url is used instead."""
    for key in ("uploader_url", "webpage_url"):
        u = e.get(key) or ""
        if "api.soundcloud.com" in u or "api-v2.soundcloud.com" in u:
            continue
        parts = [p for p in urlparse(u).path.split("/") if p]
        if parts and parts[0] not in ("tracks", "users", "playlists", "you", "discover", "search"):
            return parts[0]
    up = e.get("uploader_id")
    return up if isinstance(up, str) and not up.isdigit() else None


def search_soundcloud_profiles(name: str) -> list[dict]:
    found: dict[str, dict] = {}
    for e in _flat_search(f"scsearch15:{name}"):
        perm = _sc_permalink(e)
        if not perm:
            continue
        label = e.get("uploader") or perm
        c = found.setdefault(perm, {"id": perm, "label": f"{label} (e.g. “{e.get('title') or '?'}”)", "hits": 0, "uploader": label})
        c["hits"] += 1
    n = _norm(name)
    for perm, c in found.items():
        c["score"] = c["hits"] + (5 if n and n in _norm(perm + c["uploader"]) else 0)
    return sorted(found.values(), key=lambda c: -c["score"])[:8]


def resolve_link(text: str) -> tuple[str, str, str] | None:
    """(platform, id, label) from a pasted YouTube channel / SoundCloud profile link."""
    t = text.strip()
    m = re.fullmatch(r"UC[\w-]{22}", t) or re.search(r"youtube\.com/channel/(UC[\w-]{22})", t)
    if m:
        return "yt", (m.group(1) if m.groups() else m.group(0)), t
    if "youtube.com" in t or t.startswith("@"):
        url = t if t.startswith("http") else f"https://www.youtube.com/{t}"
        try:
            with yt_dlp.YoutubeDL(ytdlp_opts(extract_flat=True, playlistend=1)) as ydl:
                info = ydl.extract_info(url, download=False) or {}
            cid = info.get("channel_id") or info.get("uploader_id")
            if cid and cid.startswith("UC"):
                return "yt", cid, info.get("channel") or info.get("title") or t
        except Exception as e:
            print(f"  could not resolve {t}: {e}")
        return None
    m = re.search(r"soundcloud\.com/([^/?#\s]+)", t)
    if m and m.group(1) not in ("tracks", "users", "you", "discover", "search"):
        return "sc", m.group(1), m.group(1)
    return None


def _show(title: str, cands: list[dict]) -> None:
    print(f"\n{title}")
    if not cands:
        print("   (nothing found; you can paste a link instead)")
    for i, c in enumerate(cands, 1):
        print(f"  {i}) {c['label']}   [{c['id']}]")


def _pick(prompt: str, cands: list[dict], platform: str) -> list[tuple[str, str, str]]:
    """Numbers ('1,3'), pasted links, or Enter to skip. Returns (platform, id, label)."""
    raw = input(prompt).strip()
    if not raw:
        return []
    out: list[tuple[str, str, str]] = []
    if re.fullmatch(r"[\d,\s]+", raw):
        for tok in re.split(r"[,\s]+", raw):
            if tok.isdigit() and 1 <= int(tok) <= len(cands):
                c = cands[int(tok) - 1]
                out.append((platform, c["id"], c["label"]))
    else:
        for tok in re.split(r"[\s,]+", raw):
            r = resolve_link(tok)
            if r:
                out.append(r)
            else:
                print(f"  ignored: {tok}")
    return out


def artists_cli() -> None:
    try:
        while True:
            artists = read_artists()
            print("\nArtists being watched (YouTube + SoundCloud):")
            if not artists:
                print("   (none yet)")
            for i, a in enumerate(artists, 1):
                print(f"  {i:2d}) {a.name}   yt:{len(a.yt)} sc:{len(a.sc)}")
            print("\n a) Add   r) Remove   b) Back")
            c = input("Choose: ").strip().lower()
            if c in ("b", ""):
                return
            if c == "r" and artists:
                n = input("Number to remove: ").strip()
                if n.isdigit() and 1 <= int(n) <= len(artists):
                    gone = artists.pop(int(n) - 1)
                    write_artists(artists)
                    print(f"Removed {gone.name}")
            elif c == "a":
                _add_artist_flow(artists)
    except (EOFError, KeyboardInterrupt):
        print()


def add_artist(artists: list[Artist], name: str, picks: list[tuple[str, str, str]], save: bool = True,
               chat_id: int = CHAT_ID) -> Artist:
    """Merge (platform, id, label) picks into the artist (created if new) and save the file."""
    art = next((a for a in artists if _norm(a.name) == _norm(name)), None)
    if art is None:
        art = Artist(name)
        artists.append(art)
    notes = [art.note] if art.note else []
    for p, i, label in picks:
        lst = art.yt if p == "yt" else art.sc
        if i not in lst:
            lst.append(i)
            notes.append(label[:40])
    art.note = ", ".join(dict.fromkeys(n for n in notes if n))
    if save:
        write_artists(artists, chat_id)
    return art


def artist_hash(name: str) -> str:
    return hashlib.md5(_norm(name).encode()).hexdigest()[:8]


def follow_candidates(name: str) -> list[dict]:
    """Blocking (run in a thread): YouTube/SoundCloud sources for an artist, best ones preselected."""
    yt, sc = search_youtube_channels(name), search_soundcloud_profiles(name)
    out: list[dict] = []
    for c in yt[:4]:
        topic = c["label"].lower().endswith("- topic")
        out.append({"platform": "yt", "id": c["id"], "label": c["label"], "on": topic or c is yt[0]})
    for c in sc[:2]:
        out.append({"platform": "sc", "id": c["id"], "label": c["label"], "on": c["score"] >= 6})
    return out


def _add_artist_flow(artists: list[Artist]) -> None:
    name = input("Artist name: ").strip()
    if not name:
        return
    print("Searching YouTube and SoundCloud...")
    yt, sc = search_youtube_channels(name), search_soundcloud_profiles(name)
    _show("YouTube channels (an '- Topic' channel is the auto-generated one, usually fastest for audio):", yt)
    picks = _pick("Pick YouTube channels (e.g. 1,3), paste links, or Enter to skip: ", yt, "yt")
    _show("SoundCloud profiles:", sc)
    picks += _pick("Pick SoundCloud profiles (e.g. 1), paste links, or Enter to skip: ", sc, "sc")
    if not picks:
        print("Nothing selected.")
        return
    print(f"\n{name} will be watched on:")
    for p, i, label in picks:
        print(f"   {PLATFORM_NAME[p]}: {label}")
    if input("Save? [Y/n] ").strip().lower().startswith("n"):
        return
    add_artist(artists, name, picks, save=True)
    print("Saved. The bot starts watching within a few minutes (current uploads are ignored, only new ones are sent).")
