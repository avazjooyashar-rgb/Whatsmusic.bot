"""Shared bits for the admin package. `core` is the bot.py module, set by register()."""
import logging
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

log = logging.getLogger("admin")
core = None


def setup(c):
    """Remember the core module and add the columns the admin features need."""
    global core
    core = c
    with c.db() as con:
        for col, typ in (("last_seen", "TEXT"), ("blocked", "INTEGER DEFAULT 0"), ("banned", "INTEGER DEFAULT 0")):
            cols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
            if col not in cols:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")


def is_admin(uid: int) -> bool:
    return uid in core.ADMIN_IDS


def touch(user):
    """Make sure the user exists and remember when we last saw them."""
    import datetime
    day = datetime.date.today().isoformat()
    with core.db() as c:
        c.execute("INSERT OR IGNORE INTO users(uid, first_seen, lang) VALUES(?,?,?)",
                  (user.id, day, core.detect_lang(user.language_code)))
        c.execute("UPDATE users SET last_seen=?, blocked=0 WHERE uid=?", (day, user.id))


BACK = Markup([[Btn("⬅️ بازگشت", callback_data="adm:menu")]])


def admin_menu():
    return Markup([
        [Btn("📣 پیام همگانی", callback_data="adm:bc"), Btn("📊 آمار", callback_data="adm:stats")],
        [Btn("🔒 عضویت اجباری", callback_data="adm:fj"), Btn("🚫 مسدود / آزاد", callback_data="adm:ban")],
        [Btn("➕ ACRCloud", callback_data="adm:addacr"), Btn("➕ AudD", callback_data="adm:addaudd")],
        [Btn("📋 اکانت‌ها", callback_data="adm:list"), Btn("⚙️ سقف روزانه", callback_data="adm:limit")],
    ])
