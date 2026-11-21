"""❤️ Favorites: react with a heart to any song in the group and the bot copies it into the
Favorites topic (created on first use). Remove the heart and the copy is removed again."""
from __future__ import annotations

import asyncio
import shutil
import tempfile

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, FSInputFile, MessageReactionUpdated, ReplyParameters

import access
import health
import playlists
from core import (
    CHAT_ID, FAV_EMOJIS, FAV_TOPIC_NAME, db, dl_lock, is_owner, log, record_audio, scope_where, send_audio_topic, sk,
    tg_call, topic_id_for, topic_known, ws_ids,
)

router = Router()


def _hearts(reactions) -> bool:
    return any(getattr(r, "emoji", "").replace("️", "") in FAV_EMOJIS for r in reactions or [])


def song_key(chat_id: int, msg_id: int) -> str | None:
    """Which song a group message holds, or None if it is not a song the bot sent."""
    row = db.execute("SELECT key FROM audios WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone()
    if row:
        return row[0]
    cond, args = scope_where(chat_id, "playlist_id")
    row = db.execute(f"SELECT track_id FROM tracks WHERE message_id=? AND {cond}", (msg_id, *args)).fetchone()
    return f"sp:{row[0]}" if row else None


def count(chat_id: int = CHAT_ID) -> int:
    cond, args = scope_where(chat_id, "key")
    return db.execute(f"SELECT COUNT(*) FROM favs WHERE {cond}", args).fetchone()[0]


def topic_id(chat_id: int = CHAT_ID) -> int | None:
    return topic_known(chat_id, "fav_topic")


_busy: set[str] = set()


async def _meta(key: str) -> dict | None:
    """Title/artist/duration of a song, from the database or (playlist songs of older versions) from Spotify."""
    row = db.execute("SELECT artist, title, duration FROM audio_meta WHERE key=?", (key,)).fetchone()
    if row and row[1]:
        return {"artist": row[0], "title": row[1], "duration": row[2]}
    if not key.startswith("sp:"):
        return None
    tid = key[3:]
    for (pid,) in db.execute("SELECT playlist_id FROM tracks WHERE track_id=?", (tid,)).fetchall():
        try:
            async with aiohttp.ClientSession() as session:
                data = await playlists.fetch_playlist(session, pid)
        except Exception as e:
            log.warning("could not read playlist %s: %s", pid, e)
            continue
        for t in data["tracks"]:
            if t["id"] == tid:
                m = {"artist": t["artist"], "title": t["title"], "duration": t["duration"]}
                db.execute("INSERT OR REPLACE INTO audio_meta VALUES (?,?,?,?)", (key, m["artist"], m["title"], m["duration"]))
                db.commit()
                return m
    return None


async def _rebuild(bot: Bot, chat_id: int, key: str) -> int | None:
    """The bot cannot copy the original (e.g. it was sent by a previous bot): send the song again."""
    m = await _meta(key)
    if not m:
        return None
    cached = db.execute("SELECT file_id FROM files WHERE track_id=?", (key[3:] if key.startswith("sp:") else key,)).fetchone()
    if cached:
        try:
            msg = await send_audio_topic(bot, chat_id, "fav_topic", FAV_TOPIC_NAME, 0xFF93B2, cached[0], song_key=key,
                                         title=m["title"], performer=m["artist"], duration=m["duration"] or None)
            return msg.message_id
        except TelegramBadRequest as e:
            log.warning("cached file for %s unusable: %s", key, e)
    tmp = tempfile.mkdtemp(prefix="telspot_fav_")
    try:
        async with dl_lock():
            path = await asyncio.to_thread(playlists.download_audio, m, tmp)
        thumb = None
        if key.startswith("sp:"):
            async with aiohttp.ClientSession() as session:
                img = await playlists.fetch_track_cover(session, key[3:])
            thumb = BufferedInputFile(img, "cover.jpg") if img else None
        msg = await send_audio_topic(bot, chat_id, "fav_topic", FAV_TOPIC_NAME, 0xFF93B2, FSInputFile(path), song_key=key,
                                     title=m["title"], performer=m["artist"], duration=m["duration"] or None,
                                     thumbnail=thumb)
        db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (key[3:] if key.startswith("sp:") else key, msg.audio.file_id))
        return msg.message_id
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def add(bot: Bot, chat_id: int, msg_id: int, uid: int) -> bool:
    key = song_key(chat_id, msg_id)
    fk = sk(chat_id, key) if key else None
    if not key or fk in _busy or db.execute("SELECT 1 FROM favs WHERE key=?", (fk,)).fetchone():
        return False
    _busy.add(fk)
    try:
        new_id = None
        for attempt in (1, 2):
            tid = await topic_id_for(bot, chat_id, "fav_topic", FAV_TOPIC_NAME, 0xFF93B2, force_new=(attempt == 2))
            if attempt == 2:
                cond, args = scope_where(chat_id, "key")
                db.execute(f"DELETE FROM favs WHERE {cond}", args)  # old copies lived in the deleted topic
            try:
                copy = await tg_call(lambda: bot.copy_message(
                    chat_id, chat_id, msg_id, message_thread_id=tid, disable_notification=True))
                new_id = copy.message_id
                break
            except TelegramBadRequest as e:
                if attempt == 1 and "thread" in str(e).lower():
                    continue
                log.warning("favorite: cannot copy message %s (%s), sending the song again", msg_id, e)
                break
        if new_id is None:
            try:
                new_id = await _rebuild(bot, chat_id, key)
            except Exception as e:
                log.warning("favorite rebuild failed for %s: %s", key, e)
        if new_id is None:
            await health.fail(bot, "favorites", "Could not add a liked song to Favorites", "", threshold=3)
            try:
                await bot.send_message(chat_id, "❌ Could not add that song to Favorites.",
                                       reply_parameters=ReplyParameters(message_id=msg_id, allow_sending_without_reply=True))
            except Exception:
                pass
            return False
        db.execute("INSERT OR REPLACE INTO favs VALUES (?,?,?,?)", (fk, msg_id, new_id, uid))
        record_audio(chat_id, new_id, key)
        db.commit()
        await health.ok(bot, "favorites")
        return True
    finally:
        _busy.discard(fk)


async def remove(bot: Bot, chat_id: int, msg_id: int, uid: int) -> bool:
    cond, args = scope_where(chat_id, "key")
    row = db.execute(f"SELECT key, fav_msg FROM favs WHERE src_msg=? AND uid=? AND {cond}", (msg_id, uid, *args)).fetchone()
    if not row:
        return False
    db.execute("DELETE FROM favs WHERE key=?", (row[0],))
    db.commit()
    try:
        await tg_call(lambda: bot.delete_message(chat_id, row[1]))
    except Exception as e:
        log.warning("could not delete favorite copy: %s", e)
    return True


async def _is_ws(ev: MessageReactionUpdated) -> bool:
    return ev.chat.id in ws_ids()


@router.message_reaction(_is_ws)
async def on_reaction(ev: MessageReactionUpdated, bot: Bot) -> None:
    if ev.user is None or ev.user.is_bot or not access.verified(ev.user.id):
        return
    cid = ev.chat.id
    # in the owner's main group only the owner's hearts count (members cannot change that group)
    if cid == CHAT_ID and not is_owner(ev.user.id):
        return
    now, before = _hearts(ev.new_reaction), _hearts(ev.old_reaction)
    if now and not before:
        await add(bot, cid, ev.message_id, ev.user.id)
    elif before and not now:
        await remove(bot, cid, ev.message_id, ev.user.id)
