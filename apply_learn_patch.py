#!/usr/bin/env python3
"""apply_learn_patch.py - hook learn_forward.py into the REAL bot.py of the server (idempotent, makes a backup).
    ./venv/bin/python apply_learn_patch.py
"""
import shutil, sys

P = "bot.py"
ANCHOR = "    app.add_error_handler(on_error)\n"
BLOCK = '''    # teach the bot songs by forwarding them to its private chat (owner only, /learn on|off)
    try:
        import learn_forward
        learn_forward.register(app)
    except Exception:
        log.exception("learn_forward not loaded - the bot continues without it")
'''

src = open(P, encoding="utf-8").read()
if "learn_forward" in src:
    sys.exit("already patched: nothing to do")
if src.count(ANCHOR) != 1:
    sys.exit("anchor not found exactly once. Add these lines by hand in main(), before app.run_polling:\n\n" + BLOCK)
shutil.copy(P, P + ".before_learn")
open(P, "w", encoding="utf-8").write(src.replace(ANCHOR, BLOCK + ANCHOR))
print("OK. changed: bot.py (backup: bot.py.before_learn). Restart the bot.")
