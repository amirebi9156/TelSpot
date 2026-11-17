"""❤️ Favorites: react with a heart to any song in the group and the bot copies it into the
Favorites topic (created on first use). Remove the heart and the copy is removed again."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import MessageReactionUpdated

from core import CHAT_ID, FAV_EMOJIS, FAV_TOPIC_NAME, db, log, record_audio, tg_call, topic_id_for

router = Router()


def _hearts(reactions) -> bool:
    return any(getattr(r, "emoji", "").replace("️", "") in FAV_EMOJIS for r in reactions or [])


def song_key(msg_id: int) -> str | None:
    """Which song a group message holds, or None if it is not a song the bot sent."""
    row = db.execute("SELECT key FROM audios WHERE msg_id=?", (msg_id,)).fetchone()
    if row:
        return row[0]
    row = db.execute("SELECT track_id FROM tracks WHERE message_id=?", (msg_id,)).fetchone()
    return f"sp:{row[0]}" if row else None


def count() -> int:
    return db.execute("SELECT COUNT(*) FROM favs").fetchone()[0]


def topic_id() -> int | None:
    row = db.execute("SELECT value FROM meta WHERE key='fav_topic'").fetchone()
    return int(row[0]) if row else None


async def add(bot: Bot, msg_id: int, uid: int) -> bool:
    key = song_key(msg_id)
    if not key or db.execute("SELECT 1 FROM favs WHERE key=?", (key,)).fetchone():
        return False
    for attempt in (1, 2):
        tid = await topic_id_for(bot, "fav_topic", FAV_TOPIC_NAME, 0xFF93B2, force_new=(attempt == 2))
        if attempt == 2:
            db.execute("DELETE FROM favs")  # old copies lived in the deleted topic
        try:
            copy = await tg_call(lambda: bot.copy_message(
                CHAT_ID, CHAT_ID, msg_id, message_thread_id=tid, disable_notification=True))
            break
        except TelegramBadRequest as e:
            if attempt == 1 and "thread" in str(e).lower():
                continue
            log.warning("favorite copy failed: %s", e)
            return False
    db.execute("INSERT OR REPLACE INTO favs VALUES (?,?,?,?)", (key, msg_id, copy.message_id, uid))
    record_audio(copy.message_id, key)
    db.commit()
    return True


async def remove(bot: Bot, msg_id: int, uid: int) -> bool:
    row = db.execute("SELECT key, fav_msg FROM favs WHERE src_msg=? AND uid=?", (msg_id, uid)).fetchone()
    if not row:
        return False
    db.execute("DELETE FROM favs WHERE key=?", (row[0],))
    db.commit()
    try:
        await tg_call(lambda: bot.delete_message(CHAT_ID, row[1]))
    except Exception as e:
        log.warning("could not delete favorite copy: %s", e)
    return True


@router.message_reaction(F.chat.id == CHAT_ID)
async def on_reaction(ev: MessageReactionUpdated, bot: Bot) -> None:
    if ev.user is None or ev.user.is_bot:
        return
    now, before = _hearts(ev.new_reaction), _hearts(ev.old_reaction)
    if now and not before:
        await add(bot, ev.message_id, ev.user.id)
    elif before and not now:
        await remove(bot, ev.message_id, ev.user.id)
