"""
apply_speed_patch.py  -  run inside /root/whatsmusic-bot

Only touches the speed / timer parts of YOUR bot.py. fp_engine, learn_forward, name patches etc. are NOT touched.
 * makes a backup: bot.py.before_speed
 * every edit is checked; one that does not match is SKIPPED and reported (nothing is forced)
 * the new file is compile-checked before it replaces bot.py
"""
import re, shutil, subprocess, sys, tempfile, os

PATH = "bot.py"
src = open(PATH, encoding="utf-8").read()
orig = src
report = []


def sub_exact(name, old, new):
    global src
    if src.count(old) == 1:
        src = src.replace(old, new)
        report.append(("OK  ", name))
    else:
        report.append(("SKIP", f"{name} (found {src.count(old)}x)"))


def sub_re(name, pattern, new, flags=re.S):
    global src
    ms = list(re.finditer(pattern, src, flags))
    if len(ms) == 1:
        m = ms[0]
        src = src[:m.start()] + new + src[m.end():]
        report.append(("OK  ", name))
    else:
        report.append(("SKIP", f"{name} (found {len(ms)}x)"))


# 1) config: shorter timeout + grace time
if "GRACE_SEC" not in src:
    sub_re("config ENGINE_TIMEOUT/GRACE_SEC",
           r'ENGINE_TIMEOUT = int\(os\.environ\.get\("ENGINE_TIMEOUT", "\d+"\)\)[^\n]*\n',
           'ENGINE_TIMEOUT = int(os.environ.get("ENGINE_TIMEOUT", "20"))  # max wait for one batch of engine answers\n'
           'GRACE_SEC = float(os.environ.get("GRACE_SEC", "4"))  # after the first answer wait at most this long for the other engine\n')
else:
    report.append(("SKIP", "config (GRACE_SEC already there)"))

# 2) progress helper (placed before the chat_action helper)
if "def progress_text" not in src:
    sub_exact("progress_text helper",
              "@asynccontextmanager\nasync def chat_action(",
              'def progress_text(label: str, s: int) -> str:\n'
              '    k = min(5, 1 + s // 4)\n'
              '    return f"{label}\\n{\'▰\' * k}{\'▱\' * (5 - k)}  {s}s"\n\n\n'
              "@asynccontextmanager\nasync def chat_action(")

# 3) _confident: one clean answer is enough
sub_re("_confident",
       r"def _confident\(ranked: list\) -> bool:.*?\n\n\n",
       '''def _confident(ranked: list) -> bool:
    """One clean answer from ACR / Shazam is enough (score >= 0.95); humming matches are weaker and
    still need a second vote. Mashup / remix-like titles never stop the search early."""
    if not ranked or ranked[0]["bad"]:
        return False
    return ranked[0]["solid"] or ranked[0]["score"] >= 0.95


''')

# 4) run_batch: after the first answer wait only GRACE_SEC for the other engine
sub_re("run_batch grace wait",
       r"        pending = set\(futs\)\n        deadline = time\.time\(\) \+ ENGINE_TIMEOUT\n        while pending:.*?"
       r"            if _confident\(_group\(hits\)\):\n                break[^\n]*\n",
       '''        pending = set(futs)
        deadline = time.time() + ENGINE_TIMEOUT
        first = None  # time of the first real answer
        while pending:
            now = time.time()
            if now >= deadline or (first and now - first > GRACE_SEC):
                break
            done, pending = wait(pending, timeout=min(deadline - now, 1.0), return_when=FIRST_COMPLETED)
            n0 = len(hits)
            for f in done:
                name, idx = futs[f]
                try:
                    absorb(name, idx, f.result())
                except Exception as e:
                    log.warning("engine %s crashed: %s", name, e)
            if len(hits) > n0 and first is None:
                first = time.time()
            if _confident(_group(hits)):
                break
''')

# 5) two pieces at once from the start
sub_exact("piece batches (2,2,3)", "for size in (1, 2, 2):", "for size in (2, 2, 3):")

# 6) prep_clip publishes the song name as soon as it is known
sub_exact("prep_clip publishes song",
          '        res["song"], res["blocked"] = song, blocked\n',
          '        res["song"], res["blocked"] = song, blocked\n'
          '        if song and sid in PREP:\n            PREP[sid]["song"] = song\n')

# 7) PREP entries get a "song" key
sub_exact("PREP in do_clip",
          'PREP[sid] = {"task": prep_task, "src": src, "created": time.time(), "used": False}',
          'PREP[sid] = {"task": prep_task, "src": src, "created": time.time(), "used": False, "song": None}')
sub_exact("PREP in clip_from_cache",
          'PREP[sid] = {"task": asyncio.create_task(prep()), "src": src, "created": time.time(), "used": False}',
          'PREP[sid] = {"task": asyncio.create_task(prep()), "src": src, "created": time.time(),\n'
          '                 "used": False, "song": song}')

# 8) clip button: progress bar with seconds + result as soon as the name is known
sub_re("show_prepared + tick_button",
       r"async def show_prepared\(.*?\n\n\n",
       '''async def tick_button(q, entry, lang: str):
    """Progress bar with a seconds counter on the button, until the song name is known."""
    t0 = time.time()
    while not (entry.get("song") or entry["task"].done()):
        s = int(time.time() - t0)
        k = min(5, 1 + s // 4)
        try:
            await q.edit_message_reply_markup(reply_markup=Markup(
                [[Btn(f"⏳ {'▰' * k}{'▱' * (5 - k)}  {s}s", callback_data="noop:")]]))
        except Exception:
            pass
        await asyncio.sleep(2)


async def show_prepared(q, sid: str, entry: dict, lang: str):
    """The user tapped 'identify': show the progress bar, then the result as soon as the name is known."""
    if entry.get("used"):
        return await q.answer()
    await q.answer(tr(lang, "wait_toast"))
    task = entry["task"]
    ticker = asyncio.create_task(tick_button(q, entry, lang))
    t0 = time.time()
    try:
        while not (entry.get("song") or task.done()) and time.time() - t0 < 240:
            await asyncio.sleep(0.3)
    finally:
        ticker.cancel()
    res = task.result() if task.done() and not task.cancelled() else {}
    song = entry.get("song") or res.get("song")
    if not song:
        drop_entry(sid)  # the next tap recognizes again
        try:
            await q.edit_message_reply_markup(reply_markup=clip_keyboard(sid, lang))
        except Exception:
            pass
        key = "quota_short" if res.get("blocked") else "not_found"
        return await q.message.reply_text(tr(lang, key))
    if entry.get("used"):
        return
    entry["used"] = True
    src = entry["src"]
    try:
        await q.edit_message_caption(caption=result_caption(song, src), parse_mode="HTML",
                                     reply_markup=result_keyboard(sid, song, lang))
    except Exception:
        log.warning("edit caption failed", exc_info=True)
    try:
        r2 = await asyncio.wait_for(asyncio.shield(task), timeout=240)
        a = (r2 or {}).get("audio") or await prepare_song_audio(song)
        await deliver_audio(q.message, a, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err"))
    PREP.pop(sid, None)


''')

# 9) voice: live seconds counter while recognizing
sub_exact("voice progress counter",
          "            song = await asyncio.to_thread(recognize, outs)\n",
          '''            status = await msg.reply_text(progress_text(tr(lang, "identifying"), 0))

            async def tick():
                t0 = time.time()
                while True:
                    await asyncio.sleep(2)
                    s = int(time.time() - t0)
                    try:
                        await status.edit_text(progress_text(tr(lang, "identifying"), s))
                    except Exception:
                        pass

            ticker = asyncio.create_task(tick())
            try:
                song = await asyncio.to_thread(recognize, outs)
            finally:
                ticker.cancel()
                try:
                    await status.delete()
                except Exception:
                    pass
''')

# ---- write only if the result compiles ----
print()
for st, name in report:
    print(st, name)
print()
if src == orig:
    print("nothing changed.")
    sys.exit(0)
with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as t:
    t.write(src)
    tmp = t.name
r = subprocess.run([sys.executable, "-m", "py_compile", tmp], capture_output=True, text=True)
if r.returncode != 0:
    print("COMPILE FAILED, bot.py was NOT changed:\n", r.stderr)
    sys.exit(1)
shutil.copy(PATH, PATH + ".before_speed")
open(PATH, "w", encoding="utf-8").write(src)
os.remove(tmp)
print("done. backup: bot.py.before_speed  -> now: systemctl restart whatsmusic")
