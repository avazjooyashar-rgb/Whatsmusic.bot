#!/usr/bin/env python3
"""
patch_bot.py - اعمال تغییرات «نسخه‌ی دقیق» روی bot.py (v7)

    python3 patch_bot.py /root/whatsmusic-bot/bot.py

* اول از bot.py یک بکاپ (bot.py.bak) می‌گیره
* اگه حتی یکی از نقطه‌های تغییر پیدا نشه، هیچ چیزی نوشته نمیشه (همه یا هیچ)
* نیاز به fp_engine.py جدید داره (identify با exclude، set_file_id، flag_wrong)

تغییرات:
  1. نتیجه‌ی دیتابیس محلی حالا track_id، duration (dur) و file_id هم داره
  2. اگه همون ضبط قبلاً به تلگرام فرستاده شده (file_id)، همون فایل مستقیم ارسال میشه
     (اگه تلگرام file_id رو قبول نکرد، خودکار دوباره دانلود میشه)
  3. وقتی از یوتیوب دانلود میشه و مدتش با ضبط دیتابیس (±۴ ثانیه) می‌خونه، file_id همون ضبط ذخیره میشه
  4. انتخاب فایل یوتیوب با duration دقیق ضبط (تحمل ۶ ثانیه به جای ۲۰) انجام میشه
  5. کش «اسم|خواننده» برای آهنگ‌های دیتابیس محلی خاموشه (ضبط‌های هم‌اسم قاطی نشن)
  6. دکمه‌ی «❌ آهنگ اشتباه» روی نتیجه‌ی دیتابیس محلی: همون ضبط حذف/کنار گذاشته میشه
     (flag_wrong) و نتیجه‌ی بعدی گرفته میشه، نه اینکه کل دیتابیس محلی خاموش بشه
  7. صدای ویس که از دیتابیس محلی شناخته بشه، برای «آهنگ اشتباه» نگه داشته میشه
"""
import re
import shutil
import sys

# ----------------------------------------------------------------------------------------
# new function bodies
# ----------------------------------------------------------------------------------------
NEW_LOCAL_IDENTIFY = '''def _fp_set_file_id(track_id, file_id):
    """Remember the Telegram file_id of the EXACT recording in the local DB (needs the new fp_engine)."""
    try:
        fp_engine.set_file_id(track_id, file_id)
    except Exception as e:
        log.warning("fp set_file_id failed: %s", e)


def _fp_flag_wrong(track_id):
    """The user pressed 'wrong song' on a local match: unverified (api-learned) tracks are deleted."""
    try:
        return fp_engine.flag_wrong(track_id)
    except Exception as e:
        log.warning("fp flag_wrong failed: %s", e)


def local_identify(paths, exclude=()):
    """Own fingerprint DB. Returns a song dict or None.
    The song carries track_id / dur / file_id of the exact recording that matched.
    exclude = track ids that must not be returned again (the user said 'wrong song')."""
    if not fp_engine:
        return None
    use = paths[:3]
    found = []
    for p in use:
        try:
            r = fp_engine.identify(p, exclude) if exclude else fp_engine.identify(p)
        except Exception as e:
            log.warning("local fp failed: %s", e)
            continue
        if r and r.get("title"):
            found.append({"title": r["title"], "artist": r.get("artist") or "",
                          "link": r.get("link"), "yt": r.get("yt"), "engine": "local#0",
                          "track_id": r.get("track_id"), "dur": r.get("duration"),
                          "file_id": r.get("file_id")})
    # a single loose hit is not trusted when several pieces were asked: two pieces must agree
    need = 2 if len(use) >= 2 else 1
    for f in found:
        group = [g for g in found if same_song(f, g)]
        if len(group) >= need:
            ids = [g["track_id"] for g in group if g.get("track_id")]
            if ids:  # several recordings with this name: take the one most pieces agree on
                best = max(set(ids), key=ids.count)
                f = next(g for g in group if g.get("track_id") == best)
            return f
    return None'''

NEW_SET_LAST = '''def set_last(sid: str, song: dict):
    val = f"{song.get('engine') or ''}||{title_key(song.get('title'))}||{song.get('track_id') or ''}"
    with db() as c:
        c.execute("UPDATE samples SET last=? WHERE id=?", (val, sid))'''

NEW_FETCH_AUDIO = '''async def fetch_audio(target: str, song=None) -> dict:
    # a song from the local DB is ONE specific recording: do not share cached files by "title|artist"
    # with another recording that has the same name
    keys = [target] + (song_keys(song) if song and not song.get("track_id") else [])
    for k in keys:
        hit = await asyncio.to_thread(cache_get, k)
        if hit:
            return {"file_id": hit["file_id"], "title": hit["title"], "info": hit["info"]}
    tmp = tempfile.mkdtemp(dir=DL_DIR)
    fb = None
    if song:
        fb = f"{song['artist']} - {song['title']}"
    elif target.startswith("ytsearch1:"):
        fb = target[10:]
    try:
        async with dl_sem():
            a = await asyncio.to_thread(download_audio, target, tmp, fb)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    a["dir"] = tmp
    a["keys"] = keys
    return a'''

NEW_PREPARE = '''async def prepare_song_audio(song: dict) -> dict:
    """Downloads the ORIGINAL recording. Candidates come from pick_targets (best first). If the downloaded
    length is clearly different from the real song length, the next candidate is tried.
    A song with a stored Telegram file_id (the exact recording) is not downloaded at all."""
    fid = song.get("file_id")
    if fid:
        return {"file_id": fid, "title": song.get("title") or "", "info": None, "local": True}
    targets = await asyncio.to_thread(pick_targets, song)
    want = song.get("dur")
    tol = 6 if (song.get("track_id") and want) else 20  # the length of a local recording is exact
    err = None
    for i, t in enumerate(targets):
        last = i == len(targets) - 1
        try:
            a = await fetch_audio(t, song)
        except Exception as e:
            err = e
            if last:
                raise
            continue
        got = a.get("duration")
        if want and got and not last and "file_id" not in a and abs(got - want) > tol:
            log.info("wrong length (%ss vs %ss) for %s, trying the next candidate", got, int(want), t)
            shutil.rmtree(a["dir"], ignore_errors=True)
            continue
        return a
    raise err or RuntimeError("no download candidate")'''

NEW_DELIVER = '''async def deliver_audio(msg, a: dict, song=None, lang: str = "fa"):
    title = (song or {}).get("title") or a["title"]
    performer = (song or {}).get("artist") or None
    tsong = {"title": title, "artist": performer or ""}
    kb = audio_keyboard(await asyncio.to_thread(track_put, tsong), tsong, lang)
    for _ in range(2):
        if "file_id" not in a:
            break
        try:
            return await msg.reply_audio(a["file_id"], title=title, performer=performer,
                                         caption=audio_caption(info_link(a, tsong)), parse_mode="HTML",
                                         reply_markup=kb)
        except Exception:
            if not a.get("local") or not song:
                raise
            log.warning("stored file_id was rejected, downloading the song again")
            if song.get("track_id"):
                await asyncio.to_thread(_fp_set_file_id, song["track_id"], None)
            song = {k: v for k, v in song.items() if k != "file_id"}
            a = await prepare_song_audio(song)
    info = info_link(a, tsong)
    try:
        with open(a["path"], "rb") as f:
            sent = await msg.reply_audio(f, title=title, performer=performer, duration=a.get("duration"),
                                         caption=audio_caption(info), parse_mode="HTML", reply_markup=kb,
                                         read_timeout=300, write_timeout=300)
    finally:
        shutil.rmtree(a["dir"], ignore_errors=True)
    if sent.audio:
        for k in a["keys"]:
            await asyncio.to_thread(cache_put, k, sent.audio.file_id, title, info)
        tid = (song or {}).get("track_id")
        want, got = (song or {}).get("dur"), a.get("duration")
        if tid and want and got and abs(got - want) <= 4:
            # this file IS the recording of the local DB (same length): next time send it directly
            await asyncio.to_thread(_fp_set_file_id, tid, sent.audio.file_id)'''

FUNCS = [
    ("local_identify", NEW_LOCAL_IDENTIFY),
    ("set_last", NEW_SET_LAST),
    ("fetch_audio", NEW_FETCH_AUDIO),
    ("prepare_song_audio", NEW_PREPARE),
    ("deliver_audio", NEW_DELIVER),
]


# ----------------------------------------------------------------------------------------
def replace_func(src: str, name: str, new: str) -> str:
    ms = list(re.finditer(rf"^(?:async )?def {name}\(", src, re.M))
    if len(ms) != 1:
        raise SystemExit(f"[x] function {name}: found {len(ms)} times (expected 1). Nothing was changed.")
    start = ms[0].start()
    end = src.find("\n\n\n", ms[0].end())
    if end == -1:
        end = len(src)
    return src[:start] + new.rstrip("\n") + src[end:]


def replace_once(src: str, old: str, new: str) -> str:
    n = src.count(old)
    if n != 1:
        raise SystemExit(f"[x] anchor found {n} times (expected 1). Nothing was changed.\n    {old.strip()[:100]}")
    return src.replace(old, new)


def main(path: str):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if "_fp_flag_wrong" in src:
        raise SystemExit("bot.py is already patched.")

    for name, new in FUNCS:
        src = replace_func(src, name, new)

    # the exact recording's ids must survive the clip / voice caches
    src = replace_once(src, 'SONG_KEYS = ("title", "artist", "link", "yt", "dur")',
                       'SONG_KEYS = ("title", "artist", "link", "yt", "dur", "track_id", "file_id")')

    # recognize(): tokens "t:<id>" in skip = local recordings the user rejected
    src = replace_once(
        src, '    rejected = {t[2:] for t in skip if t.startswith("k:")}\n',
        '    rejected = {t[2:] for t in skip if t.startswith("k:")}\n'
        '    excl = {int(t[2:]) for t in skip if t[:2] == "t:" and t[2:].isdigit()}\n')
    src = replace_once(src, '        loc = local_identify(paths)\n',
                       '        loc = local_identify(paths, excl)\n')

    # on_rec(): "wrong song" on a local match drops only that recording
    src = replace_once(
        src, '        eng, _, key = row["last"].partition("||")\n',
        '        eng, _, rest = row["last"].partition("||")\n'
        '        key, _, ftid = rest.partition("||")\n'
        '        if eng == "local#0" and ftid.isdigit():\n'
        '            # a wrong match from our own DB: only that recording is dropped\n'
        '            # (the same name may be ANOTHER recording that is right)\n'
        '            tried.append("t:" + ftid)\n'
        '            await asyncio.to_thread(_fp_flag_wrong, int(ftid))\n'
        '            eng, key = "", ""\n')

    # handle_media(): keep the voice of a local match so that "wrong song" can check it again
    pat = re.compile(r'(song\["alts"\] = \[\]\n)([ \t]+)try:\n[ \t]+os\.remove\(raw\)\n'
                     r'[ \t]+except OSError:\n[ \t]+pass\n[ \t]+await asyncio\.to_thread\(add_sample, sid, ""\)\n')
    if len(pat.findall(src)) != 1:
        raise SystemExit("[x] handle_media block not found. Nothing was changed.")

    def keep(m):
        i = m.group(2)
        return (m.group(1) + f'{i}keep = os.path.join(SAMPLES_DIR, f"{{sid}}_0.ogg")\n'
                f'{i}try:\n{i}    os.replace(raw, keep)  # kept: "wrong song" can re-check this audio\n'
                f'{i}except OSError:\n{i}    keep = ""\n'
                f'{i}await asyncio.to_thread(add_sample, sid, keep)\n')
    src = pat.sub(keep, src)

    shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("OK. backup:", path + ".bak")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
