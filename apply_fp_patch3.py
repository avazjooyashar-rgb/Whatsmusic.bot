#!/usr/bin/env python3
"""
apply_fp_patch3.py - run AFTER apply_fp_patch.py and apply_fp_patch2.py.

bot.py
  1. "wrong song" can no longer wipe an entry of our database with ONE press. A normal user only adds a flag;
     the entry is deleted when FP_FORGET_VOTES different users flagged it (default 3). An admin deletes at once.
     (Before, any user could erase a song you seeded by hand, and the button is open to everybody.)
  2. the own-engine lookup stops at the first piece that is clearly known (faster answers)
fp_engine.py (skipped automatically when your fp_engine.py already has these fixes)
  3. recognize(): the same song stored under another spelling / artist no longer counts as a rival of itself
     (e.g. "Haydeh/Reza Parsa" from your channel + "Reza Parsa" from the API used to block each other)
  4. add_track(): adding the same manual track twice no longer doubles its hashes

Run from the bot folder:   ./venv/bin/python apply_fp_patch3.py
Every edit must match EXACTLY ONE place, both files are syntax-checked first, backups: *.before_fp3
"""
import os
import py_compile
import shutil
import sys
import tempfile

BOT_EDITS = []
ENGINE_EDITS = []


def bot_edit(old, new):
    BOT_EDITS.append((old, new))


def engine_edit(old, new):
    ENGINE_EDITS.append((old, new))


# ---------------------------------------------------------------- bot.py
bot_edit('''def fp_forget(key: str):''', '''FP_FORGET_VOTES = int(os.environ.get("FP_FORGET_VOTES", "3"))  # different users needed to delete an entry


def fp_forget_vote(key: str, uid: int):
    # "Wrong song" pressed. One user alone cannot erase an entry from our database: the entry is deleted when
    # FP_FORGET_VOTES different users flagged it (an admin deletes at once). The user who pressed the button
    # never gets that answer again anyway (the song is in his own rejected list).
    eng = _fp_engine()
    if not eng or not key:
        return
    with _FP_LOCK:
        conn = eng.db.conn
        conn.execute("CREATE TABLE IF NOT EXISTS fp_flags("
                     "track_id INTEGER, uid INTEGER, PRIMARY KEY(track_id, uid))")
        ids = [r[0] for r in conn.execute("SELECT id, title FROM tracks").fetchall()
               if title_key(r[1]) == key]
        for i in ids:
            conn.execute("INSERT OR IGNORE INTO fp_flags VALUES(?,?)", (i, uid))
            n = conn.execute("SELECT COUNT(*) FROM fp_flags WHERE track_id=?", (i,)).fetchone()[0]
            if uid in ADMIN_IDS or n >= FP_FORGET_VOTES:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_fp_track ON fingerprints(track_id)")
                conn.execute("DELETE FROM fingerprints WHERE track_id=?", (i,))
                conn.execute("DELETE FROM tracks WHERE id=?", (i,))
                conn.execute("DELETE FROM fp_flags WHERE track_id=?", (i,))
                log.info("own engine: removed track #%s (flagged by %s user%s)", i, n, "" if n == 1 else "s")
            else:
                log.info("own engine: track #%s flagged as wrong (%s/%s)", i, n, FP_FORGET_VOTES)
        conn.commit()


def fp_forget(key: str):''')

bot_edit('''await asyncio.to_thread(fp_forget, key)  # ...and it must not come back from our own database''',
         '''await asyncio.to_thread(fp_forget_vote, key, uid)  # ...enough flags remove it from our own database''')

bot_edit('''        if r and r["score"] >= FP_MIN_SCORE and (best is None or r["score"] > best["score"]):
            best = r
''', '''        if r and r["score"] >= FP_MIN_SCORE and (best is None or r["score"] > best["score"]):
            best = r
            if r["score"] >= FP_MIN_SCORE * 2:
                break  # clearly known: no need to look at the other pieces
''')

# ---------------------------------------------------------------- fp_engine.py
engine_edit('''import argparse
import os
''', '''import argparse
import os
import re
''')

engine_edit('''        ranked = sorted(best.items(), key=lambda x: x[1], reverse=True)
        top_tid, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
''', '''        ranked = sorted(best.items(), key=lambda x: x[1], reverse=True)
        top_tid, top_score = ranked[0]
        norm = lambda s: re.sub(r"[\\W_]+", "", (s or "").lower())
        top_title = norm(self.db.track_info(top_tid)[0])
        second = 0
        for tid, sc in ranked[1:]:  # the same title stored under another name is the same song, not a rival
            if norm(self.db.track_info(tid)[0]) != top_title:
                second = sc
                break
''')

engine_edit('''        tid = self.db.get_or_create_track(title, artist, source)
        self.db.add_hashes(tid, hashes)
        return tid
''', '''        tid = self.db.get_or_create_track(title, artist, source)
        have = self.db.conn.execute("SELECT n_hashes FROM tracks WHERE id=?", (tid,)).fetchone()[0]
        if have and source == "manual":
            return tid  # already stored: adding it again would double its hashes
        self.db.add_hashes(tid, hashes)
        return tid
''')


def patch(path, edits, name):
    """Apply all edits (each must match exactly once) and syntax-check. Returns the new source."""
    src = open(path, encoding="utf-8").read()
    for n, (old, new) in enumerate(edits, 1):
        c = src.count(old)
        if c != 1:
            sys.exit(f"{name} edit #{n} matches {c} places (must be exactly 1) - NOTHING was changed. "
                     f"Send me this line: {old.strip().splitlines()[0][:100]}")
        src = src.replace(old, new)
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        sys.exit(f"patched {path} has a syntax error, NOTHING was changed:\n{e}")
    finally:
        os.remove(tmp)
    return src


def main():
    for path in ("bot.py", "fp_engine.py"):
        if not os.path.exists(path):
            sys.exit(f"{path} not found: run this from the bot folder")
    bot_src = open("bot.py", encoding="utf-8").read()
    eng_src = open("fp_engine.py", encoding="utf-8").read()
    if "fp_verify_async(" not in bot_src:
        sys.exit("run apply_fp_patch.py and apply_fp_patch2.py first")
    bot_done = "fp_forget_vote(" in bot_src
    # a newer fp_engine.py already has the same-song / no-double-hash fixes (its own _tkey logic)
    eng_done = "top_title = norm(" in eng_src or "_tkey(" in eng_src
    if bot_done and eng_done:
        sys.exit("nothing to do: bot.py and fp_engine.py already contain these changes")
    out = []
    if not bot_done:
        out.append(("bot.py", patch("bot.py", BOT_EDITS, "bot.py")))
    if not eng_done:
        out.append(("fp_engine.py", patch("fp_engine.py", ENGINE_EDITS, "fp_engine.py")))
    for path, new in out:
        shutil.copy(path, path + ".before_fp3")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    print("OK. changed:", ", ".join(p for p, _ in out) or "-",
          "| already up to date:", ", ".join(p for p, d in (("bot.py", bot_done), ("fp_engine.py", eng_done)) if d) or "-")
    print("Backups: *.before_fp3. Restart the bot. Optional env: FP_FORGET_VOTES=3")


if __name__ == "__main__":
    main()
