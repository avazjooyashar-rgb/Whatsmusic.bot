import sys, py_compile

SRC = "/tmp/gh_bot.py"
OUT = "/tmp/bot_merged.py"
s = open(SRC, encoding="utf-8").read()
n_ok = 0


def rep(old, new, name):
    global s, n_ok
    c = s.count(old)
    if c != 1:
        sys.exit(f"FAILED [{name}]: found {c} times (expected 1). Nothing written.")
    s = s.replace(old, new)
    n_ok += 1


# 1) SONG_KEYS
rep(r'''SONG_KEYS = ("title", "artist", "link", "yt", "dur")''',
    r'''SONG_KEYS = ("title", "artist", "link", "yt", "dur", "track_id", "file_id")''', "SONG_KEYS")

# 2) set_last
rep(r'''    val = f"{song.get('engine') or ''}||{title_key(song.get('title'))}"''',
    r'''    val = f"{song.get('engine') or ''}||{title_key(song.get('title'))}||{song.get('track_id') or ''}"''',
    "set_last")

# 3) fp functions
rep(r'''def _fp_identify(path: str):''', r'''def _fp_identify(path: str, exclude=()):''', "fp_identify sig")
rep(r'''    return _FP_FN(path) if _FP_FN else None
''', r'''    if not _FP_FN:
        return None
    return _FP_FN(path, exclude) if exclude else _FP_FN(path)
''', "fp_identify call")

rep(r'''def local_identify(paths):
    """Own fingerprint DB. Returns a song dict or None.
    fp_engine's function must return {"title","artist"[,"link","yt"]} or None."""
''', r'''def _fp_set_file_id(track_id, file_id):
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
''', "local_identify header")

rep(r'''            r = _fp_identify(p)
''', r'''            r = _fp_identify(p, exclude)
''', "identify call")

rep(r'''                          "link": r.get("link"), "yt": r.get("yt"), "engine": "local#0"})''',
    r'''                          "link": r.get("link"), "yt": r.get("yt"), "engine": "local#0",
                          "track_id": r.get("track_id"), "dur": r.get("duration"),
                          "file_id": r.get("file_id")})''', "found.append")

rep(r'''    for f in found:
        if sum(1 for g in found if same_song(f, g)) >= need:
            return f
    return None''', r'''    for f in found:
        group = [g for g in found if same_song(f, g)]
        if len(group) >= need:
            ids = [g["track_id"] for g in group if g.get("track_id")]
            if ids:  # several recordings with this name: take the one most pieces agree on
                best = max(set(ids), key=ids.count)
                f = next(g for g in group if g.get("track_id") == best)
            return f
    return None''', "group")

# 4) recognize
rep(r'''    rejected = {t[2:] for t in skip if t.startswith("k:")}

    if "local#0" not in skip:
        loc = local_identify(paths)''', r'''    rejected = {t[2:] for t in skip if t.startswith("k:")}
    excl = {int(t[2:]) for t in skip if t[:2] == "t:" and t[2:].isdigit()}

    if "local#0" not in skip:
        loc = local_identify(paths, excl)''', "recognize")

# 5) caption
rep(r'''def result_caption(song: dict, src=None) -> str:
    return f"<code>{esc(song['title'])} — {esc(song['artist'])}</code>\n\n{footer(src)}"''',
    r'''_NO_ARTIST = {"", "unknown", "unknown artist", "نامشخص"}


def has_artist(song: dict) -> bool:
    """False when the artist is unknown (or SHOW_ARTIST=0): then only the song name is shown."""
    if os.environ.get("SHOW_ARTIST", "1") != "1":
        return False
    return (song.get("artist") or "").strip().lower() not in _NO_ARTIST


def result_caption(song: dict, src=None) -> str:
    name = esc(song['title'])
    if has_artist(song):
        name += f" — {esc(song['artist'])}"
    return f"<code>{name}</code>\n\n{footer(src)}"''', "caption")

# 6) fetch_audio
rep(r'''    keys = [target] + (song_keys(song) if song else [])''',
    r'''    # a song from the local DB is ONE specific recording: do not share cached files by "title|artist"
    # with another recording that has the same name
    keys = [target] + (song_keys(song) if song and not song.get("track_id") else [])''', "fetch_audio")

# 7) prepare_song_audio
rep(r'''    length is clearly different from the real song length, the next candidate is tried."""
    targets = await asyncio.to_thread(pick_targets, song)
    want = song.get("dur")
    err = None''', r'''    length is clearly different from the real song length, the next candidate is tried.
    A song with a stored Telegram file_id (the exact recording) is not downloaded at all."""
    fid = song.get("file_id")
    if fid:
        return {"file_id": fid, "title": song.get("title") or "", "info": None, "local": True}
    targets = await asyncio.to_thread(pick_targets, song)
    want = song.get("dur")
    tol = 6 if (song.get("track_id") and want) else 20  # the length of a local recording is exact
    err = None''', "prepare head")
rep(r'''abs(got - want) > 20:''', r'''abs(got - want) > tol:''', "prepare tol")

# 8) deliver_audio
rep(r'''    info = info_link(a, tsong)
    if "file_id" in a:
        return await msg.reply_audio(a["file_id"], title=title, performer=performer,
                                     caption=audio_caption(info), parse_mode="HTML",
                                     reply_markup=kb)
    try:
        if os.path.getsize(a["path"]) > MAX_BYTES:''', r'''    for _ in range(2):
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
        if os.path.getsize(a["path"]) > MAX_BYTES:''', "deliver head")

rep(r'''        for k in a["keys"]:
            await asyncio.to_thread(cache_put, k, sent.audio.file_id, title, info)
''', r'''        for k in a["keys"]:
            await asyncio.to_thread(cache_put, k, sent.audio.file_id, title, info)
        tid = (song or {}).get("track_id")
        want, got = (song or {}).get("dur"), a.get("duration")
        if tid and want and got and abs(got - want) <= 4:
            # this file IS the recording of the local DB (same length): next time send it directly
            await asyncio.to_thread(_fp_set_file_id, tid, sent.audio.file_id)
''', "deliver tail")

# 9) handle_media keep sample
rep(r'''                song["alts"] = []
                try:
                    os.remove(raw)
                except OSError:
                    pass
                await asyncio.to_thread(add_sample, sid, "")''', r'''                song["alts"] = []
                keep = os.path.join(SAMPLES_DIR, f"{sid}_0.ogg")
                try:
                    os.replace(raw, keep)  # kept: "wrong song" can re-check this audio
                except OSError:
                    keep = ""
                await asyncio.to_thread(add_sample, sid, keep)''', "handle_media")

# 10) on_rec bad
rep(r'''        eng, _, key = row["last"].partition("||")
        if key:''', r'''        eng, _, rest = row["last"].partition("||")
        key, _, ftid = rest.partition("||")
        if eng == "local#0" and ftid.isdigit():
            # a wrong match from our own DB: only that recording is dropped
            # (the same name may be ANOTHER recording that is right)
            tried.append("t:" + ftid)
            await asyncio.to_thread(_fp_flag_wrong, int(ftid))
            eng, key = "", ""
        if key:''', "on_rec")

# 11) learn_forward
rep(r'''    app.add_error_handler(on_error)
    try:
        from admin import register''', r'''    # teach the bot songs by forwarding them to its private chat (owner only, /learn on|off)
    try:
        import learn_forward
        learn_forward.register(app)
    except Exception:
        log.exception("learn_forward not loaded - the bot continues without it")
    app.add_error_handler(on_error)
    try:
        from admin import register''', "learn_forward")

open(OUT, "w", encoding="utf-8").write(s)
py_compile.compile(OUT, doraise=True)
print(f"OK: {n_ok} changes applied, compile OK -> {OUT}")
