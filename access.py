"""Who may use the bot.
- First time: a secret question (ACCESS_ANSWER in .env). Right answer = verified, wrong 5 times = locked 15 min.
- Everybody who touches the bot is recorded (numeric id, username, name) so the owner can see them.
- Owner: numeric id from OWNER_ID in .env, or claimed once with /claim <code> (the code is printed in the
  server log at start, so only someone with access to the server can become the owner).
- Owner-only: /users (list, block, unblock, revoke)."""
from __future__ import annotations

import hmac
import html
import secrets
import time

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message, User,
)

from core import (
    ACCESS_ANSWERS, ACCESS_QUESTION, ANON_ADMIN_ID, CHAT_ID, db, is_owner, log, owner_id, ws_ids,
)

router = Router()
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
_claim_code: str | None = None
_group_hint: dict[int, float] = {}  # uid -> last time we answered them in the group


# ------------------------------------------------------------------ state
def gate_on() -> bool:
    return bool(ACCESS_ANSWERS)


def touch(u: User) -> None:
    now = int(time.time())
    name = " ".join(x for x in (u.first_name, u.last_name) if x)
    db.execute(
        "INSERT INTO users (uid, username, name, first_seen, last_seen) VALUES (?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET username=excluded.username, name=excluded.name, last_seen=excluded.last_seen",
        (u.id, u.username or "", name, now, now),
    )
    db.commit()


def _row(uid: int):
    return db.execute("SELECT verified_at, blocked, fails, locked_until FROM users WHERE uid=?", (uid,)).fetchone()


def blocked(uid: int) -> bool:
    if is_owner(uid):
        return False
    r = _row(uid)
    return bool(r and r[1])


def verified(uid: int) -> bool:
    if uid == ANON_ADMIN_ID or is_owner(uid) or not gate_on():
        return not blocked(uid)
    r = _row(uid)
    return bool(r and r[0] and not r[1])


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def check_answer(text: str) -> bool:
    t = _norm(text).encode()
    return any(hmac.compare_digest(t, a.encode()) for a in ACCESS_ANSWERS)


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def ago(ts: int | None) -> str:
    if not ts:
        return "never"
    s = int(time.time()) - int(ts)
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def list_users() -> list[tuple]:
    return db.execute(
        "SELECT uid, username, name, verified_at, blocked, last_seen FROM users ORDER BY last_seen DESC").fetchall()


def users_text() -> str:
    """Plain table for the server console (telspot users)."""
    rows = list_users()
    own = owner_id()
    out = [f"owner: {own or '(not set; send /claim CODE to the bot in private)'}",
           f"{'id':>12}  {'username':<20} {'status':<9} {'last seen':<9} name"]
    for uid, un, name, ver, blk, seen in rows:
        st = "OWNER" if uid == own else "blocked" if blk else "verified" if (ver or not gate_on()) else "pending"
        out.append(f"{uid:>12}  {('@' + un) if un else '-':<20} {st:<9} {ago(seen):<9} {name}")
    out.append(f"total {len(rows)}")
    return "\n".join(out)


def announce_claim() -> None:
    """Called at start: if there is no owner yet, print a one-time claim code in the log."""
    global _claim_code
    if owner_id():
        return
    _claim_code = secrets.token_hex(4)
    log.warning("NO OWNER YET. To become the owner, send this to the bot in a PRIVATE chat:  /claim %s", _claim_code)


# ------------------------------------------------------------- middleware
class Track(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            try:
                touch(u)
            except Exception as e:
                log.warning("could not record user: %s", e)
        return await handler(event, data)


router.message.outer_middleware(Track())
router.callback_query.outer_middleware(Track())

PRIVATE = F.chat.type == "private"


# async filters run in the event loop (sync ones run in a thread, where SQLite refuses to work)
async def BLOCKED(event) -> bool:
    return bool(event.from_user) and blocked(event.from_user.id)


async def UNVERIFIED(event) -> bool:
    return bool(event.from_user) and not verified(event.from_user.id)


def _cmd(m: Message) -> bool:
    return bool(m.text and m.text.startswith("/"))


# ----------------------------------------------------------------- claim
@router.message(PRIVATE, Command("claim"))
async def on_claim(m: Message, command: CommandObject, bot: Bot) -> None:
    uid = m.from_user.id
    if blocked(uid):
        return
    if owner_id() or not _claim_code:
        return await m.answer("This bot already has an owner.")
    if not hmac.compare_digest((command.args or "").strip().encode(), _claim_code.encode()):
        log.warning("wrong claim code from %s", uid)
        return await m.answer("Wrong code. Look at the server log: telspot logs")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('owner_id', ?)", (str(uid),))
    db.execute("UPDATE users SET verified_at=? WHERE uid=?", (int(time.time()), uid))
    db.commit()
    log.warning("owner is now %s (@%s)", uid, m.from_user.username)
    await m.answer("👑 Done. You are the owner of this bot.\nOwner commands: /users\n"
                   "(To make it permanent in .env: OWNER_ID=%d)" % uid)


# ---------------------------------------------------------------- blocked
@router.message(BLOCKED)
async def swallow_message(m: Message) -> None:
    return None


@router.callback_query(BLOCKED)
async def swallow_callback(cb: CallbackQuery) -> None:
    await cb.answer()


# --------------------------------------------------------- verification
def _locked(uid: int) -> int:
    r = _row(uid)
    return max(0, int(r[3] or 0) - int(time.time())) if r else 0


async def ask(m: Message) -> None:
    await m.answer(f"{ACCESS_QUESTION}\n\n<i>Reply with the answer.</i>", parse_mode="HTML",
                   reply_markup=ForceReply())


@router.message(PRIVATE, UNVERIFIED)
async def on_unverified_private(m: Message, bot: Bot) -> None:
    uid = m.from_user.id
    wait = _locked(uid)
    if wait:
        return await m.answer(f"Too many wrong answers. Try again in {wait // 60 + 1} min.")
    if not m.text or _cmd(m):
        return await ask(m)
    if check_answer(m.text):
        db.execute("UPDATE users SET verified_at=?, fails=0, locked_until=0 WHERE uid=?", (int(time.time()), uid))
        db.commit()
        try:
            await m.delete()  # do not leave the answer visible in the chat
        except Exception:
            pass
        await m.answer("✅ Welcome! Send /menu to start.")
        u = m.from_user
        log.info("verified %s @%s", uid, u.username)
        if owner_id():
            try:
                await bot.send_message(owner_id(), f"✅ New user: {esc(u.full_name)} "
                                       f"{'@' + u.username if u.username else ''} <code>{uid}</code>", parse_mode="HTML")
            except Exception:
                pass
        return
    r = _row(uid)
    fails = (r[2] if r else 0) + 1
    if fails >= MAX_FAILS:
        db.execute("UPDATE users SET fails=0, locked_until=? WHERE uid=?", (int(time.time()) + LOCK_SECONDS, uid))
        db.commit()
        if owner_id():
            try:
                u = m.from_user
                await bot.send_message(owner_id(), f"⚠️ {MAX_FAILS} wrong answers: {esc(u.full_name)} "
                                       f"{'@' + u.username if u.username else ''} <code>{uid}</code>", parse_mode="HTML")
            except Exception:
                pass
        return await m.answer("Wrong. Locked for 15 minutes.")
    db.execute("UPDATE users SET fails=? WHERE uid=?", (fails, uid))
    db.commit()
    await m.answer(f"Wrong answer ({MAX_FAILS - fails} tries left).", reply_markup=ForceReply())


async def IN_WS(m) -> bool:
    return m.chat.id in ws_ids()


@router.message(IN_WS, UNVERIFIED, F.text.func(lambda t: t.startswith("/")))
async def on_unverified_group_command(m: Message, bot: Bot) -> None:
    first = m.text.split()[0]
    me = await bot.me()
    if "@" in first and not first.lower().endswith("@" + (me.username or "").lower()):
        return  # a command for another bot
    now = time.time()
    if now - _group_hint.get(m.from_user.id, 0) < 600:
        return
    _group_hint[m.from_user.id] = now
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔐 Verify in private", url=f"https://t.me/{me.username}?start=verify")]])
    await m.reply("This bot is private. Verify yourself once in my private chat first.", reply_markup=kb)


@router.callback_query(UNVERIFIED)
async def on_unverified_callback(cb: CallbackQuery) -> None:
    await cb.answer("🔐 Open the bot in private and send /start to verify first.", show_alert=True)


# ------------------------------------------------------------------ owner
PAGE = 6


def users_screen(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    rows = list_users()
    own = owner_id()
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    ver = sum(1 for r in rows if r[3] and not r[4])
    lines = [f"👑 <b>Users</b>\n<blockquote>{len(rows)} seen · {ver} verified · "
             f"{sum(1 for r in rows if r[4])} blocked</blockquote>"]
    kb = []
    for uid, un, name, vat, blk, seen in rows[page * PAGE:(page + 1) * PAGE]:
        st = "👑" if uid == own else "🚫" if blk else "✅" if (vat or not gate_on()) else "⏳"
        lines.append(f"{st} <b>{esc(name) or '?'}</b> {'@' + esc(un) if un else ''}\n    <code>{uid}</code> · {ago(seen)} ago")
        if uid != own:
            btn = [InlineKeyboardButton(text=("♻️ Unblock " if blk else "🚫 Block ") + (name or str(uid))[:16],
                                        callback_data=f"u:b:{uid}:{page}")]
            if vat and not blk and gate_on():
                btn.append(InlineKeyboardButton(text="↩ Revoke", callback_data=f"u:r:{uid}:{page}"))
            kb.append(btn)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"u:p:0:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"u:p:0:{page + 1}"))
    kb.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(Command("users"))
async def cmd_users(m: Message) -> None:
    if not is_owner(m.from_user.id):
        return  # silently ignored for everybody else
    text, kb = users_screen()
    await m.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^u:[brp]:\d+:\d+$"))
async def on_users_cb(cb: CallbackQuery) -> None:
    if not is_owner(cb.from_user.id):
        return await cb.answer("Owner only.", show_alert=True)
    _, act, uid, page = cb.data.split(":")
    uid, page = int(uid), int(page)
    if uid and is_owner(uid):
        return await cb.answer("That is you.")
    if act == "b":
        r = _row(uid)
        db.execute("UPDATE users SET blocked=? WHERE uid=?", (0 if (r and r[1]) else 1, uid))
        await cb.answer("Done")
    elif act == "r":
        db.execute("UPDATE users SET verified_at=NULL, fails=0 WHERE uid=?", (uid,))
        await cb.answer("Verification removed")
    db.commit()
    text, kb = users_screen(page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
