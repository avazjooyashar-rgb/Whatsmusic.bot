#!/usr/bin/env python3
"""
apply_fp_patch2.py - run AFTER apply_fp_patch.py and after updating fp_engine.py.

What it changes: the bot no longer learns a song just because two engines agreed.
The answer is only a CLAIM. The bot already downloads the claimed song to send you the mp3;
now it also checks that the user's clip really lines up with that audio. Only then the WHOLE
song (hashes only) goes into our database. Wrong answers can no longer poison it.

Run from the bot folder:   ./venv/bin/python apply_fp_patch2.py
"""
import os
import py_compile
import shutil
import sys
import tempfile

BOT = "bot.py"
EDITS = []


def edit(old, new):
    EDITS.append((old, new))


edit('''FP_LEARN_PIECES = int(os.environ.get("FP_LEARN_PIECES", "3"))  # how many pieces of a clip are learned
''', '''FP_LEARN_PIECES = int(os.environ.get("FP_LEARN_PIECES", "3"))  # how many pieces of a clip are used to verify
FP_VERIFY_SCORE = int(os.environ.get("FP_VERIFY_SCORE", "30"))  # clip must line up this well with the real song audio
''')

edit('''def fp_forget(key: str):''', '''def fp_verify_async(src_path, song):
    # The answer of the engines is only a CLAIM. Check that the user's clip really lines up with the audio of
    # the claimed song; only then the WHOLE song is stored (hashes only, the audio copy is deleted).
    eng = _fp_engine()
    paths = [p for p in (song.pop("fp_paths", None) or []) if os.path.exists(p)]
    title, artist = song.get("title"), song.get("artist")
    if not (FP_LEARN and eng and paths and title and artist and src_path and os.path.exists(src_path)):
        return
    fd, copy = tempfile.mkstemp(suffix=os.path.splitext(src_path)[1] or ".m4a", dir=SAMPLES_DIR)
    os.close(fd)
    try:
        shutil.copyfile(src_path, copy)  # the original is deleted right after it is sent to the user
    except Exception as e:
        log.warning("own engine: could not copy the audio: %s", e)
        try:
            os.remove(copy)
        except OSError:
            pass
        return

    def job():
        try:
            from fp_engine import fingerprint_file, best_alignment
            full = fingerprint_file(copy)
            if not full:
                return
            best = 0
            for p in paths:
                best = max(best, best_alignment(full, fingerprint_file(p)))
            if best < FP_VERIFY_SCORE:
                log.info("own engine: NOT learned (%s - %s): the clip does not match that audio (score=%s)",
                         artist, title, best)
                return
            with _FP_LOCK:
                n = eng.ingest_verified(full, title, artist)
            log.info("own engine: learned %s - %s (%d hashes, verify score=%s)", artist, title, n, best)
        except Exception as e:
            log.warning("own engine verify failed: %s", e)
        finally:
            try:
                os.remove(copy)
            except OSError:
                pass

    FP_POOL.submit(job)


def fp_forget(key: str):''')

edit('''        fp_learn_async(paths, top)  # background: the next person asking for this song is answered locally
''', '''        top["fp_paths"] = list(paths[:FP_LEARN_PIECES])  # verified against the real song audio in prepare_song_audio
''')

edit('''async def prepare_song_audio(song: dict) -> dict:
    target = await asyncio.to_thread(pick_target, song)
    return await fetch_audio(target, song)
''', '''async def prepare_song_audio(song: dict) -> dict:
    target = await asyncio.to_thread(pick_target, song)
    a = await fetch_audio(target, song)
    if song.get("fp_paths") and a.get("path"):  # fresh download: verify the answer, then learn the whole song
        try:
            await asyncio.to_thread(fp_verify_async, a["path"], song)
        except Exception:
            log.exception("own engine verify setup failed")
    return a
''')


def main():
    if not os.path.exists(BOT):
        sys.exit("bot.py not found: run this from the bot folder")
    src = open(BOT, encoding="utf-8").read()
    if "fp_verify_async(" in src:
        sys.exit("bot.py already contains this patch: nothing to do")
    if "fp_identify(" not in src:
        sys.exit("run apply_fp_patch.py first")
    if not os.path.exists("fp_engine.py") or "def best_alignment" not in open("fp_engine.py", encoding="utf-8").read():
        sys.exit("fp_engine.py is the old version: git pull the new fp_engine.py first")
    for n, (old, new) in enumerate(EDITS, 1):
        c = src.count(old)
        if c != 1:
            sys.exit(f"edit #{n} matches {c} places (must be exactly 1) - bot.py was NOT changed. "
                     f"Send me this line: {old.strip().splitlines()[0][:100]}")
        src = src.replace(old, new)
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        sys.exit(f"patched file has a syntax error, bot.py was NOT changed:\n{e}")
    finally:
        os.remove(tmp)
    shutil.copy(BOT, BOT + ".before_fp2")
    with open(BOT, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"OK: {len(EDITS)} edits applied. Backup: {BOT}.before_fp2")


if __name__ == "__main__":
    main()
