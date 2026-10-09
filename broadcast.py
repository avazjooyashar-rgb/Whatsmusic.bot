"""Broadcast a message to every user."""
import asyncio
import time
import logging
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.error import RetryAfter, Forbidden, BadRequest
from . import shared as S

log = logging.getLogger("admin.broadcast")

BC = {"running": False, "cancel": False}
BG_TASKS = set()


def targets() -> list:
    with S.core.db() as c:
        return [r[0] for r in c.execute(
            "SELECT uid FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(banned,0)=0")]


def markup_of(bc: dict):
    if bc.get("btn"):
        return Markup([[Btn(bc["btn"][0], url=bc["btn"][1])]])
    return None


def bar(done: int, total: int, w: int = 12) -> str:
    f = w if not total else int(w * done / total)
    return "▓" * f + "░" * (w - f)


def cancel_kb():
    return Markup([[Btn("⛔️ توقف ارسال", callback_data="adm:bccancel")]])


async def run(bot, status_msg, src_chat: int, src_mid: int, markup):
    BC.update(running=True, cancel=False)
    uids = targets()
    total = len(uids)
    ok = blocked = fail = 0
    last = time.time()

    async def update_status(final=None):
        done = ok + blocked + fail
        pct = int(100 * done / total) if total else 100
        text = (f"{final or '📣 در حال ارسال...'}\n\n{bar(done, total)} {pct}%\n"
                f"✅ موفق: {ok}\n🚫 بلاک‌کرده: {blocked}\n❌ خطا: {fail}\n📦 {done}/{total}")
        try:
            await status_msg.edit_text(text, reply_markup=None if final else cancel_kb())
        except Exception:
            pass

    def mark_blocked(uid):
        with S.core.db() as c:
            c.execute("UPDATE users SET blocked=1 WHERE uid=?", (uid,))

    try:
        for uid in uids:
            if BC["cancel"]:
                break
            for attempt in (1, 2):
                try:
                    await bot.copy_message(uid, src_chat, src_mid, reply_markup=markup)
                    ok += 1
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    if attempt == 1:
                        continue
                    fail += 1
                except Forbidden:
                    blocked += 1
                    mark_blocked(uid)
                except BadRequest as e:
                    if "chat not found" in str(e).lower():
                        blocked += 1
                        mark_blocked(uid)
                    else:
                        fail += 1
                except Exception:
                    fail += 1
                break
            await asyncio.sleep(0.05)  # ~20 messages/second, under Telegram's limit
            if time.time() - last > 3:
                last = time.time()
                await update_status()
        await update_status("⛔️ ارسال متوقف شد" if BC["cancel"] else "✅ ارسال تمام شد")
    finally:
        BC["running"] = False
        try:
            await bot.send_message(status_msg.chat_id, "🛠 پنل مدیریت", reply_markup=S.admin_menu())
        except Exception:
            pass


async def send_confirm(msg, ctx):
    bc = ctx.user_data["bc"]
    n = len(targets())
    btn = f"\n🔘 دکمه: {bc['btn'][0]} ← {bc['btn'][1]}" if bc.get("btn") else ""
    kb = Markup([
        [Btn(f"🚀 ارسال به {n} نفر", callback_data="adm:bcgo")],
        [Btn("🧪 تست برای خودم", callback_data="adm:bctest"), Btn("🔘 دکمهٔ لینک", callback_data="adm:bcbtn")],
        [Btn("❌ لغو", callback_data="adm:bcx")],
    ])
    await msg.reply_text(f"📣 آمادهٔ ارسال به {n} کاربر.{btn}", reply_markup=kb)


async def on_message(msg, ctx, st: str) -> bool:
    """Admin sent the broadcast message (or its button). Returns True if handled."""
    if st == "bcast":
        ctx.user_data.pop("await", None)
        ctx.user_data["bc"] = {"chat": msg.chat_id, "mid": msg.message_id, "btn": None}
        await send_confirm(msg, ctx)
        return True
    if st == "bcbtn" and msg.text:
        bc = ctx.user_data.get("bc")
        label, _, url = msg.text.partition("|")
        label, url = label.strip(), url.strip()
        if not bc or not label or not url.startswith(("http://", "https://", "tg://")):
            await msg.reply_text("❌ فرمت اشتباهه. این‌جوری بفرست:\nمتن دکمه | https://t.me/channel")
            return True
        ctx.user_data.pop("await", None)
        bc["btn"] = (label[:60], url)
        await send_confirm(msg, ctx)
        return True
    return False


async def cb(q, ctx, act: str):
    if act == "bc":
        if BC["running"]:
            return await q.edit_message_text("⏳ یه ارسال همگانی در حال انجامه.", reply_markup=S.BACK)
        ctx.user_data["await"] = "bcast"
        await q.edit_message_text(
            "📣 پیام همگانی\n\nهر چی بفرستی (متن، عکس، ویدیو، فایل، ...) عیناً برای همهٔ کاربران کپی می‌شه، "
            "با همون فرمت و کپشن.\nحالا پیامت رو بفرست:", reply_markup=S.BACK)
    elif act == "bcbtn":
        if not ctx.user_data.get("bc"):
            return await q.message.reply_text("❌ اول پیام همگانی رو بفرست.")
        ctx.user_data["await"] = "bcbtn"
        await q.message.reply_text("🔘 دکمهٔ لینک رو این‌جوری بفرست:\nمتن دکمه | https://t.me/channel")
    elif act == "bctest":
        bc = ctx.user_data.get("bc")
        if not bc:
            return await q.message.reply_text("❌ پیامی آماده نیست.")
        try:
            await ctx.bot.copy_message(q.from_user.id, bc["chat"], bc["mid"], reply_markup=markup_of(bc))
        except Exception as e:
            await q.message.reply_text(f"❌ {str(e)[:150]}")
    elif act == "bcx":
        ctx.user_data.pop("bc", None)
        ctx.user_data.pop("await", None)
        await q.edit_message_text("❌ لغو شد.\n\n🛠 پنل مدیریت", reply_markup=S.admin_menu())
    elif act == "bcgo":
        bc = ctx.user_data.get("bc")
        if not bc:
            return await q.message.reply_text("❌ پیامی آماده نیست.")
        if BC["running"]:
            return await q.message.reply_text("⏳ یه ارسال همگانی در حال انجامه.")
        ctx.user_data.pop("bc", None)
        await q.edit_message_text("🚀 شروع ارسال...", reply_markup=cancel_kb())
        t = asyncio.create_task(run(ctx.bot, q.message, bc["chat"], bc["mid"], markup_of(bc)))
        BG_TASKS.add(t)
        t.add_done_callback(BG_TASKS.discard)
    elif act == "bccancel":
        BC["cancel"] = True
