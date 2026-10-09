"""Admin panel: menu, stats, accounts, limits, ban, cookies."""
import os
import datetime
import logging
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
import shared as S
from . import join, broadcast

log = logging.getLogger("admin.panel")


def stats_text() -> str:
    core = S.core
    today = datetime.date.today()
    day, week = today.isoformat(), (today - datetime.timedelta(days=7)).isoformat()
    with core.db() as c:
        one = lambda q, *a: c.execute(q, a).fetchone()[0]
        users = one("SELECT COUNT(*) FROM users")
        new = one("SELECT COUNT(*) FROM users WHERE first_seen=?", day)
        new7 = one("SELECT COUNT(*) FROM users WHERE first_seen>=?", week)
        active = one("SELECT COUNT(*) FROM users WHERE last_seen=?", day)
        blocked = one("SELECT COUNT(*) FROM users WHERE COALESCE(blocked,0)=1")
        banned = one("SELECT COUNT(*) FROM users WHERE COALESCE(banned,0)=1")
        rec = one("SELECT COALESCE(SUM(n),0) FROM usage WHERE day=?", day)
        songs = one("SELECT COUNT(*) FROM songs")
        clips = one("SELECT COUNT(*) FROM clips")
        accs = one("SELECT COUNT(*) FROM accounts WHERE enabled=1")
        langs = c.execute("SELECT COALESCE(lang,'?'), COUNT(*) FROM users GROUP BY lang ORDER BY 2 DESC LIMIT 6").fetchall()
    lim = core.daily_limit()
    lang_line = " | ".join(f"{core.LANGS.get(k, k).split(' ')[0]} {n}" for k, n in langs)
    fj = f"{'🟢' if join.enabled() else '⚪️'} {len(join.channels())} کانال"
    return (f"📊 آمار\n\n👥 کل کاربران: {users}\n🆕 جدید امروز: {new} | ۷ روز: {new7}\n"
            f"🔥 فعال امروز: {active}\n🚫 بلاک‌کرده: {blocked} | مسدود: {banned}\n\n"
            f"🎧 شناسایی امروز: {rec}\n💾 آهنگ کش‌شده: {songs} | 🎬 کلیپ: {clips}\n"
            f"🟢 اکانت فعال: {accs}\n⚙️ سقف روزانه: {'نامحدود' if lim <= 0 else lim}\n"
            f"🔒 عضویت اجباری: {fj}\n\n🌐 زبان‌ها: {lang_line}")


def accounts_lines(rows) -> str:
    return "\n".join(
        f'{"🟢" if r["enabled"] else "⚪️"} #{r["id"]} {r["type"]} …{(r["key"] or "")[-4:]} | استفاده {r["uses"]} | خطا {r["errors"]}'
        for r in rows)


async def show_accounts(q):
    with S.core.db() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    text = accounts_lines(rows) if rows else "هیچ اکانتی نیست. از منو اضافه کن."
    kb = [[Btn(f'{"⏸" if r["enabled"] else "▶️"} #{r["id"]}', callback_data=f'acc:t:{r["id"]}'),
           Btn(f'🗑 #{r["id"]}', callback_data=f'acc:d:{r["id"]}')] for r in rows]
    kb.append([Btn("⬅️ بازگشت", callback_data="adm:menu")])
    try:
        await q.edit_message_text(text, reply_markup=Markup(kb))
    except Exception:
        pass


async def admin_cb(q, ctx, data: str):
    await q.answer()
    core = S.core
    parts = data.split(":")
    if parts[0] == "acc":
        _, op, aid = parts
        with core.db() as c:
            if op == "t":
                c.execute("UPDATE accounts SET enabled=1-enabled WHERE id=?", (int(aid),))
            elif op == "d":
                c.execute("DELETE FROM accounts WHERE id=?", (int(aid),))
        return await show_accounts(q)
    act = parts[1]
    if act == "menu":
        ctx.user_data.pop("await", None)
        await q.edit_message_text("🛠 پنل مدیریت", reply_markup=S.admin_menu())
    elif act == "addacr":
        ctx.user_data["await"] = "acr"
        await q.edit_message_text("اطلاعات ACRCloud رو توی یه پیام بفرست:\nHOST ACCESS_KEY ACCESS_SECRET\n(بعد از ذخیره، پیامت پاک میشه)", reply_markup=S.BACK)
    elif act == "addaudd":
        ctx.user_data["await"] = "audd"
        await q.edit_message_text("توکن AudD رو بفرست:\n(بعد از ذخیره، پیامت پاک میشه)", reply_markup=S.BACK)
    elif act == "limit":
        ctx.user_data["await"] = "limit"
        await q.edit_message_text(f"سقف روزانه‌ی هر کاربر الان {core.daily_limit()} هست. عدد جدید رو بفرست (۰ = نامحدود):", reply_markup=S.BACK)
    elif act == "list":
        await show_accounts(q)
    elif act == "stats":
        await q.edit_message_text(stats_text(), reply_markup=S.BACK)
    elif act.startswith("fj"):
        await join.cb(q, ctx, act, parts)
    elif act.startswith("bc"):
        await broadcast.cb(q, ctx, act)
    elif act == "ban":
        await q.edit_message_text("🚫 مسدود / آزاد کردن کاربر", reply_markup=Markup([
            [Btn("🚫 مسدود کن", callback_data="adm:banadd"), Btn("✅ آزاد کن", callback_data="adm:unban")],
            [Btn("⬅️ بازگشت", callback_data="adm:menu")]]))
    elif act in ("banadd", "unban"):
        ctx.user_data["await"] = "ban" if act == "banadd" else "unban"
        await q.edit_message_text("آیدی عددی کاربر رو بفرست:", reply_markup=S.BACK)


async def hide_secret(update):
    try:
        await update.message.delete()
    except Exception:
        pass


async def admin_input(update, ctx, st: str):
    """A text message from the admin while the panel is waiting for something."""
    core = S.core
    msg = update.message
    parts = msg.text.split()
    ctx.user_data.pop("await", None)
    chat = update.effective_chat.id
    if st == "acr" and len(parts) == 3:
        with core.db() as c:
            cur = c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", tuple(parts))
        reply = f"✅ ACRCloud اضافه شد (id={cur.lastrowid})"
    elif st == "audd" and len(parts) == 1:
        with core.db() as c:
            cur = c.execute("INSERT INTO accounts(type,key) VALUES('audd',?)", (parts[0],))
        reply = f"✅ AudD اضافه شد (id={cur.lastrowid})"
    elif st == "limit" and len(parts) == 1 and parts[0].isdigit():
        core.set_setting("daily_limit", int(parts[0]))
        reply = f"✅ سقف روزانه شد {parts[0]}"
    elif st in ("ban", "unban") and len(parts) == 1 and parts[0].isdigit():
        with core.db() as c:
            n = c.execute("UPDATE users SET banned=? WHERE uid=?",
                          (1 if st == "ban" else 0, int(parts[0]))).rowcount
        reply = ("🚫 مسدود شد" if st == "ban" else "✅ آزاد شد") if n else "❌ این کاربر توی دیتابیس نیست."
    elif st == "fjadd" and len(parts) == 1:
        _, reply = await join.add_channel(ctx.bot, parts[0])
    else:
        return await msg.reply_text("❌ فرمت اشتباه بود، دوباره از پنل شروع کن.", reply_markup=S.admin_menu())
    if st in ("acr", "audd"):
        await hide_secret(update)
    await ctx.bot.send_message(chat, reply, reply_markup=S.admin_menu())


# ----- commands -----
async def cmd_admin(update, ctx):
    if not S.is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🛠 پنل مدیریت", reply_markup=S.admin_menu())


async def cmd_addacr(update, ctx):
    if not S.is_admin(update.effective_user.id):
        return
    if len(ctx.args) != 3:
        return await update.message.reply_text("/addacr HOST ACCESS_KEY ACCESS_SECRET")
    with S.core.db() as c:
        cur = c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", tuple(ctx.args))
    await hide_secret(update)
    await ctx.bot.send_message(update.effective_chat.id, f"✅ ACRCloud اضافه شد (id={cur.lastrowid})")


async def cmd_addaudd(update, ctx):
    if not S.is_admin(update.effective_user.id):
        return
    if len(ctx.args) != 1:
        return await update.message.reply_text("/addaudd API_TOKEN")
    with S.core.db() as c:
        cur = c.execute("INSERT INTO accounts(type,key) VALUES('audd',?)", (ctx.args[0],))
    await hide_secret(update)
    await ctx.bot.send_message(update.effective_chat.id, f"✅ AudD اضافه شد (id={cur.lastrowid})")


async def cmd_accounts(update, ctx):
    if not S.is_admin(update.effective_user.id):
        return
    with S.core.db() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    if not rows:
        return await update.message.reply_text("هیچ اکانتی نیست. با /admin اضافه کن.")
    await update.message.reply_text(accounts_lines(rows))


async def cmd_remove(update, ctx):
    if not S.is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("/remove ID")
    with S.core.db() as c:
        n = c.execute("DELETE FROM accounts WHERE id=?", (int(ctx.args[0]),)).rowcount
    await update.message.reply_text("🗑 حذف شد" if n else "پیدا نشد")


async def cmd_toggle(update, ctx):
    if not S.is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("/toggle ID")
    with S.core.db() as c:
        n = c.execute("UPDATE accounts SET enabled=1-enabled WHERE id=?", (int(ctx.args[0]),)).rowcount
    await update.message.reply_text("✅ تغییر کرد" if n else "پیدا نشد")


# ----- cookies.txt upload (YouTube / Instagram) -----
async def on_document(update, ctx):
    msg = update.message
    if not msg or not msg.from_user or not S.is_admin(msg.from_user.id):
        return
    doc = msg.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        return
    if (doc.file_size or 0) > 3 * 1024 * 1024:
        return await msg.reply_text("❌ فایل خیلی بزرگه.")
    cookies = S.core.COOKIES
    tmp = cookies + ".tmp"
    await (await doc.get_file()).download_to_drive(tmp)
    with open(tmp, encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if "youtube.com" not in content and "instagram.com" not in content:
        os.remove(tmp)
        return await msg.reply_text("❌ این فایل کوکی یوتیوب یا اینستاگرام نیست.")
    os.replace(tmp, cookies)
    await msg.reply_text("✅ کوکی ذخیره شد، لینک رو دوباره امتحان کن.")
    try:
        await msg.delete()
    except Exception:
        pass
