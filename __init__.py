"""Admin package for the music bot.

Everything here is optional: bot.py only calls register() inside a try/except, and the
handlers run in their own groups (before the bot's own ones), so a problem in this folder
can never break song recognition.

  panel.py      admin menu, stats, ACR/AudD accounts, daily limit, ban, cookies upload
  join.py       forced channel membership (gate + settings)
  broadcast.py  message to all users
  shared.py     small helpers
"""
import logging
from telegram import Update
from telegram.ext import (ApplicationHandlerStop, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters)
from . import shared as S
from . import panel, join, broadcast

log = logging.getLogger("admin")


async def on_admin_cb(update, ctx):
    q = update.callback_query
    if not S.is_admin(q.from_user.id):
        await q.answer("⛔", show_alert=True)
        raise ApplicationHandlerStop
    try:
        await panel.admin_cb(q, ctx, q.data)
    except Exception:
        log.exception("admin callback failed")
    raise ApplicationHandlerStop  # the bot's own button handler must not see it


async def on_admin_msg(update, ctx):
    """Admin messages the panel is waiting for (broadcast text, channel, limit, ...)."""
    msg = update.message
    user = update.effective_user
    if not msg or not user or not S.is_admin(user.id):
        return
    st = ctx.user_data.get("await")
    if not st:
        if msg.document:
            await panel.on_document(update, ctx)
        return
    try:
        handled = await broadcast.on_message(msg, ctx, st)
        if not handled and msg.text:
            await panel.admin_input(update, ctx, st)
            handled = True
    except Exception:
        log.exception("admin message failed")
        handled = True
    if handled:
        raise ApplicationHandlerStop


def register(app, core):
    S.setup(core)
    items = [
        # group -3: admin first
        (CommandHandler("admin", panel.cmd_admin), -3),
        (CommandHandler("addacr", panel.cmd_addacr), -3),
        (CommandHandler("addaudd", panel.cmd_addaudd), -3),
        (CommandHandler("accounts", panel.cmd_accounts), -3),
        (CommandHandler("remove", panel.cmd_remove), -3),
        (CommandHandler("toggle", panel.cmd_toggle), -3),
        (CallbackQueryHandler(on_admin_cb, pattern=r"^(adm|acc):"), -3),
        (MessageHandler(filters.ALL & ~filters.COMMAND, on_admin_msg), -3),
        # group -2: bans + forced join
        (MessageHandler(filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
                        join.message_gate), -2),
        (CallbackQueryHandler(join.callback_gate, pattern=r"^(rec|bad|lyr|pick|more):"), -2),
        (CallbackQueryHandler(join.on_join_check, pattern=r"^chk:"), -2),
    ]
    for handler, group in items:  # added only after everything above worked
        app.add_handler(handler, group=group)
    log.info("admin module loaded")
