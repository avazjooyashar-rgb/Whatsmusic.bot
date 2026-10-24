#!/usr/bin/env python3
"""apply_name_patch.py - show only the song name when the artist is unknown / junk (idempotent, makes a backup).
    ./venv/bin/python apply_name_patch.py
Optional env: SHOW_ARTIST=0 hides the artist for EVERY song (title only).
"""
import shutil, sys

P = "bot.py"
src = open(P, encoding="utf-8").read()
if "def has_artist(" in src:
    sys.exit("already patched: nothing to do")

CAP_OLD = '''def result_caption(song: dict, src=None) -> str:
    return f"<code>{esc(song['title'])} — {esc(song['artist'])}</code>\\n\\n{footer(src)}"
'''
CAP_NEW = '''_NO_ARTIST = {"", "unknown", "unknown artist", "نامشخص"}


def has_artist(song: dict) -> bool:
    """False when the artist is unknown (or SHOW_ARTIST=0): then only the song name is shown."""
    if os.environ.get("SHOW_ARTIST", "1") != "1":
        return False
    return (song.get("artist") or "").strip().lower() not in _NO_ARTIST


def result_caption(song: dict, src=None) -> str:
    name = esc(song['title'])
    if has_artist(song):
        name += f" — {esc(song['artist'])}"
    return f"<code>{name}</code>\\n\\n{footer(src)}"
'''
PERF_OLD = '''    performer = (song or {}).get("artist") or None\n'''
PERF_NEW = '''    performer = (song or {}).get("artist") if has_artist(song or {}) else None\n'''

missing = [n for n, old in (("result_caption", CAP_OLD), ("deliver_audio", PERF_OLD)) if src.count(old) != 1]
if missing:
    sys.exit("anchor not found exactly once for: " + ", ".join(missing) + "\nSend me those two functions from bot.py.")
shutil.copy(P, P + ".before_name")
src = src.replace(CAP_OLD, CAP_NEW).replace(PERF_OLD, PERF_NEW)
open(P, "w", encoding="utf-8").write(src)
print("OK. changed: bot.py (backup: bot.py.before_name). Restart the bot.")
