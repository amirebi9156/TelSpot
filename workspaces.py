"""Personal groups: anyone can add Tel Spot to their own supergroup (with Topics) and use it with
their own playlists and artists. This module reacts to the bot being added/removed and checks
that the group is ready (Topics on + the bot is an admin who may manage topics)."""
from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup

import access
from core import (
    CHAT_ID, USER_MAX_GROUPS, db, is_owner, log, require_join, joined_main, set_active_ws, user_ws, ws_ids,
    ws_register, ws_unregister, ws_title, ws_owner,
)

router = Router()
IN, OUT = ("member", "administrator"), ("left", "kicked")


def _pend_get(chat_id: int) -> int:
    r = db.execute("SELECT value FROM meta WHERE key=?", (f"pend:{chat_id}",)).fetchone()
    return int(r[0]) if r else 0


def _pend_set(chat_id: int, uid: int | None) -> None:
    if uid:
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"pend:{chat_id}", str(uid)))
    else:
        db.execute("DELETE FROM meta WHERE key=?", (f"pend:{chat_id}",))
    db.commit()


async def readiness(bot: Bot, chat_id: int) -> list[str]:
    """What is still missing in that group (empty list = ready)."""
    problems = []
    chat = await bot.get_chat(chat_id)
    if chat.type != "supergroup":
        problems.append("Make it a supergroup (Group settings → Chat history → Visible, or any admin change converts it).")
    if not getattr(chat, "is_forum", False):
        problems.append("Turn on Topics (Group settings → Topics).")
    me = await bot.me()
    m = await bot.get_chat_member(chat_id, me.id)
    if m.status != "administrator":
        problems.append("Make me an admin.")
    elif not getattr(m, "can_manage_topics", False) and m.status != "creator":
        problems.append("Give me the “Manage topics” admin right.")
    return problems


def _kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Check again", callback_data=f"wsc:{chat_id}")]])


async def _dm(bot: Bot, uid: int, text: str, kb=None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
        return True
    except Exception as e:
        log.info("cannot DM %s: %s", uid, e)
        return False


async def evaluate(bot: Bot, chat_id: int, title: str, uid: int) -> str:
    """Register the group if it is ready, else tell the owner what is missing. Returns 'ok'|'pending'|'denied'."""
    if chat_id in ws_ids():
        return "ok"
    if not is_owner(uid):
        if len(user_ws(uid)) >= USER_MAX_GROUPS:
            await _dm(bot, uid, f"❌ You already connected {USER_MAX_GROUPS} group(s), which is the limit.")
            return "denied"
        if require_join() and not await joined_main(bot, uid):
            await _dm(bot, uid, "❌ Join the creator's group first, then add me again.")
            return "denied"
    problems = await readiness(bot, chat_id)
    if problems:
        _pend_set(chat_id, uid)
        await _dm(bot, uid, f"📁 <b>{title}</b> is almost ready. Still needed:\n" + "\n".join(f"• {p}" for p in problems),
                  _kb(chat_id))
        return "pending"
    _pend_set(chat_id, None)
    ws_register(chat_id, uid, title)
    set_active_ws(uid, chat_id)
    await _dm(bot, uid, f"✅ <b>{title}</b> is connected!\nSend /menu here to add playlists and follow artists "
                        "(or use the menu inside the group).")
    log.info("group connected: %s (%s) by %s", title, chat_id, uid)
    return "ok"


@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_my_chat_member(ev: ChatMemberUpdated, bot: Bot) -> None:
    cid, new = ev.chat.id, ev.new_chat_member.status
    if cid == CHAT_ID:
        return
    if new in OUT:
        if cid in ws_ids():
            owner = ws_owner(cid)
            ws_unregister(cid)
            _pend_set(cid, None)
            log.info("removed from group %s", cid)
            if owner:
                await _dm(bot, owner, f"ℹ️ I was removed from <b>{ev.chat.title}</b>. Its data is kept; add me again to reconnect.")
        return
    if new not in IN:
        return
    uid = _pend_get(cid) or (ws_owner(cid) if cid in ws_ids() else 0) or ev.from_user.id
    if cid in ws_ids():
        return
    if access.gate_on() and not (is_owner(uid) or access.verified(uid)):
        await _dm(bot, uid, "🔐 Open me in private and answer the secret question first, then add me to your group again.")
        try:
            await bot.leave_chat(cid)
        except Exception:
            pass
        return
    await evaluate(bot, cid, ev.chat.title or str(cid), uid)


@router.callback_query(F.data.startswith("wsc:"))
async def on_recheck(cb: CallbackQuery, bot: Bot) -> None:
    cid = int(cb.data.split(":")[1])
    uid = cb.from_user.id
    owner = _pend_get(cid) or ws_owner(cid)
    if owner != uid and not is_owner(uid):
        return await cb.answer("Not your group.", show_alert=True)
    if cid in ws_ids():
        return await cb.answer("Already connected ✅", show_alert=True)
    try:
        title = (await bot.get_chat(cid)).title or str(cid)
    except Exception:
        return await cb.answer("I cannot see that group. Add me again.", show_alert=True)
    res = await evaluate(bot, cid, title, uid)
    await cb.answer("Connected ✅" if res == "ok" else "Not ready yet — see my message." if res == "pending" else "Not allowed.",
                    show_alert=res != "ok")
