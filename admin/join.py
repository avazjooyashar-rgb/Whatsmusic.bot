"""Forced channel membership: gate + admin screens."""
import json
import re
import time
import logging
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.ext import ApplicationHandlerStop
from . import shared as S

log = logging.getLogger("admin.join")

JT = {
    "fa": {"need": "🔒 برای استفاده از ربات، اول توی کانال(های) زیر عضو شو و بعد دکمهٔ «عضو شدم» رو بزن 👇",
           "btn": "✅ عضو شدم", "ok": "✅ عضویتت تأیید شد! حالا می‌تونی از ربات استفاده کنی.",
           "no": "❌ هنوز توی همهٔ کانال‌ها عضو نشدی."},
    "en": {"need": "🔒 To use the bot, please join the channel(s) below first, then tap “I've joined” 👇",
           "btn": "✅ I've joined", "ok": "✅ Membership confirmed! You can use the bot now.",
           "no": "❌ You haven't joined all the channels yet."},
    "tr": {"need": "🔒 Botu kullanmak için önce aşağıdaki kanal(lar)a katıl, sonra «Katıldım» düğmesine dokun 👇",
           "btn": "✅ Katıldım", "ok": "✅ Üyeliğin onaylandı! Artık botu kullanabilirsin.",
           "no": "❌ Henüz tüm kanallara katılmadın."},
    "ar": {"need": "🔒 لاستخدام البوت، انضم أولاً إلى القناة (القنوات) التالية ثم اضغط «انضممت» 👇",
           "btn": "✅ انضممت", "ok": "✅ تم تأكيد اشتراكك! يمكنك استخدام البوت الآن.",
           "no": "❌ لم تنضم إلى جميع القنوات بعد."},
    "ru": {"need": "🔒 Чтобы пользоваться ботом, сначала подпишись на канал(ы) ниже, затем нажми «Я подписался» 👇",
           "btn": "✅ Я подписался", "ok": "✅ Подписка подтверждена! Теперь можно пользоваться ботом.",
           "no": "❌ Ты ещё не подписался на все каналы."},
}


def jt(lang: str, key: str) -> str:
    return JT.get(lang, JT["en"]).get(key) or JT["en"][key]


JOIN_OK = {}  # uid -> last time membership was verified


def channels() -> list:
    try:
        return json.loads(S.core.get_setting("force_channels", "[]"))
    except Exception:
        return []


def enabled() -> bool:
    return S.core.get_setting("force_on", "1") == "1"


def is_banned(uid: int) -> bool:
    with S.core.db() as c:
        r = c.execute("SELECT banned FROM users WHERE uid=?", (uid,)).fetchone()
    return bool(r and r["banned"])


async def missing_channels(bot, uid: int) -> list:
    miss = []
    for ch in channels():
        try:
            m = await bot.get_chat_member(ch["id"], uid)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", True)):
                miss.append(ch)
        except Exception as e:
            # bot is not admin there / channel gone: don't lock every user out
            log.warning("membership check failed for %s: %s", ch.get("id"), e)
    return miss


def keyboard(chs: list, lang: str):
    rows = [[Btn(f"📢 {c['title']}"[:60], url=c["url"])] for c in chs]
    rows.append([Btn(jt(lang, "btn"), callback_data="chk:1")])
    return Markup(rows)


async def join_ok(bot, uid: int, lang: str, msg=None) -> bool:
    if S.is_admin(uid) or not enabled() or not channels():
        return True
    if time.time() - JOIN_OK.get(uid, 0) < 120:
        return True
    miss = await missing_channels(bot, uid)
    if not miss:
        JOIN_OK[uid] = time.time()
        return True
    if msg:
        await msg.reply_text(jt(lang, "need"), reply_markup=keyboard(miss, lang))
    return False


# ----- gates (run before the bot's own handlers; any failure lets the user through) -----
async def message_gate(update, ctx):
    msg = update.message
    if not msg or not msg.from_user:
        return
    uid = msg.from_user.id
    try:
        S.touch(msg.from_user)
        if S.is_admin(uid):
            return
        if is_banned(uid):
            raise ApplicationHandlerStop
        if not await join_ok(ctx.bot, uid, S.core.get_lang(uid), msg):
            raise ApplicationHandlerStop
    except ApplicationHandlerStop:
        raise
    except Exception:
        log.exception("message gate failed (user let through)")


async def callback_gate(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    try:
        S.touch(q.from_user)
        if S.is_admin(uid):
            return
        if is_banned(uid) or not await join_ok(ctx.bot, uid, S.core.get_lang(uid), q.message):
            await q.answer()
            raise ApplicationHandlerStop
    except ApplicationHandlerStop:
        raise
    except Exception:
        log.exception("callback gate failed (user let through)")


async def on_join_check(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    lang = S.core.get_lang(uid)
    JOIN_OK.pop(uid, None)
    if await missing_channels(ctx.bot, uid):
        return await q.answer(jt(lang, "no"), show_alert=True)
    JOIN_OK[uid] = time.time()
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
    await ctx.bot.send_message(q.message.chat_id, jt(lang, "ok"))


# ----- admin screens -----
async def show_force(q):
    chans = channels()
    on = enabled()
    lines = "\n".join(f"{i + 1}. {c['title']}" for i, c in enumerate(chans)) or "هنوز کانالی اضافه نشده."
    text = (f"🔒 عضویت اجباری: {'🟢 روشن' if on else '⚪️ خاموش'}\n\n{lines}\n\n"
            "⚠️ ربات باید توی هر کانال ادمین باشه.")
    kb = [[Btn(f"🗑 {c['title'][:28]}", callback_data=f"adm:fjdel:{i}")] for i, c in enumerate(chans)]
    kb.append([Btn("➕ افزودن کانال", callback_data="adm:fjadd"),
               Btn("⏸ خاموش کن" if on else "▶️ روشن کن", callback_data="adm:fjtog")])
    kb.append([Btn("⬅️ بازگشت", callback_data="adm:menu")])
    try:
        await q.edit_message_text(text, reply_markup=Markup(kb))
    except Exception:
        pass


async def add_channel(bot, ref: str):
    """Returns (ok, text)."""
    if "t.me/" in ref:
        ref = "@" + ref.split("t.me/")[-1].strip("/").split("/")[0]
    if re.fullmatch(r"-?\d+", ref):
        ref = int(ref)
    elif not ref.startswith("@"):
        ref = "@" + ref
    try:
        ch = await bot.get_chat(ref)
        me = await bot.get_chat_member(ch.id, bot.id)
        if me.status not in ("administrator", "creator"):
            return False, "❌ ربات توی این کانال ادمین نیست. اول ادمینش کن."
        url = f"https://t.me/{ch.username}" if ch.username else (ch.invite_link or await bot.export_chat_invite_link(ch.id))
    except Exception as e:
        return False, f"❌ کانال پیدا نشد یا ربات دسترسی نداره.\n{str(e)[:120]}"
    chans = channels()
    if any(c["id"] == ch.id for c in chans):
        return False, "این کانال از قبل اضافه شده."
    chans.append({"id": ch.id, "title": ch.title or str(ch.id), "url": url})
    S.core.set_setting("force_channels", json.dumps(chans, ensure_ascii=False))
    S.core.set_setting("force_on", "1")
    return True, f"✅ «{ch.title}» به عضویت اجباری اضافه شد."


async def cb(q, ctx, act: str, parts: list):
    if act == "fj":
        ctx.user_data.pop("await", None)
        await show_force(q)
    elif act == "fjadd":
        ctx.user_data["await"] = "fjadd"
        await q.edit_message_text("یوزرنیم کانال رو بفرست (مثلاً @mychannel) یا آیدی عددی‌اش (-100...).\n"
                                  "⚠️ اول ربات رو توی کانال ادمین کن.", reply_markup=S.BACK)
    elif act == "fjtog":
        S.core.set_setting("force_on", "0" if enabled() else "1")
        await show_force(q)
    elif act == "fjdel":
        chans = channels()
        i = int(parts[2])
        if 0 <= i < len(chans):
            chans.pop(i)
            S.core.set_setting("force_channels", json.dumps(chans, ensure_ascii=False))
        await show_force(q)
