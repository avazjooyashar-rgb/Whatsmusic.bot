# =====================================================================
# Recognition upgrade for whatsmusic-bot/bot.py
# Apply the 5 steps below in order. Take a backup first:
#   cp bot.py bot.py.bak
# =====================================================================

# ---------------------------------------------------------------------
# STEP 0: env / install
#   export HUM_MIN_SCORE=20
#   export WHISPER_MODEL=small
#   pip install -U faster-whisper --break-system-packages   (if not installed)
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# STEP 1: in acr_identify(), replace the WHOLE block that starts with
#         `if not music:` and ends with `return None` (before `m = music[0]`)
#         with this block:
# ---------------------------------------------------------------------
    if not music:
        # melody match (covers / someone else singing your song): keep several candidates
        hum = [h for h in (md.get("humming") or []) if h.get("title")]
        hum.sort(key=lambda x: x.get("score") or 0, reverse=True)
        log.info("acr humming: %s", [(h.get("title"), h.get("score")) for h in hum[:5]])
        good = []
        for h in hum[:5]:
            score = h.get("score") or 0
            if score < HUM_MIN_SCORE:
                continue
            artist = ", ".join(a["name"] for a in (h.get("artists") or []) if a.get("name"))
            good.append({"title": h["title"], "artist": artist, "link": None, "yt": None,
                         "wmul": min(0.9, 0.4 + score / 150)})
        if good:
            top = good[0]
            top["more"] = [dict(g, wmul=g["wmul"] * 0.6) for g in good[1:]]
            return top
        log.info("acr: no music match (metadata keys: %s)", list(md.keys()))
        return None


# ---------------------------------------------------------------------
# STEP 2: inside recognize() -> run_batch(), replace the part after
#         `if not res or title_key(...) in rejected: continue`
#         (the last 2 lines of the loop) with:
# ---------------------------------------------------------------------
                more = res.pop("more", [])
                base_w = ENGINE_W.get(name, 0.8) - 0.01 * order.get(name, 0)
                w = base_w * res.get("wmul", 1.0)
                hits.append({"song": res, "engine": res["engine"], "sample": idx, "w": w})
                for mo in more:  # runner-up melody matches
                    if title_key(mo.get("title")) in rejected:
                        continue
                    mo["engine"] = res["engine"]
                    hits.append({"song": mo, "engine": mo["engine"], "sample": idx,
                                 "w": base_w * mo.get("wmul", 0.5)})


# ---------------------------------------------------------------------
# STEP 3: replace transcribe() and lyrics_identify() with these,
#         and ADD the two new helpers (_parse_yt_title, yt_lyrics_search)
#         plus make_variants() anywhere above recognize().
#         Also change the default:  WHISPER_MODEL default "base" -> "small"
# ---------------------------------------------------------------------
def transcribe(path: str) -> str:
    """Speech-to-text of the SINGING (no VAD: it throws away vocals mixed with music)."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return ""
    with _WHISPER_LOCK:
        if _WHISPER["model"] is None:
            _WHISPER["model"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segs, _info = _WHISPER["model"].transcribe(
            path, vad_filter=False, beam_size=5, temperature=0.0,
            condition_on_previous_text=False, no_speech_threshold=0.4,
            compression_ratio_threshold=2.2)
        out, last = [], None
        for sg in segs:
            t = sg.text.strip()
            if t and t != last:  # drop looping hallucinations
                out.append(t)
            last = t
        return " ".join(out).strip()


def _parse_yt_title(title: str, channel: str):
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title or "")
    t = re.sub(r"(?i)\b(official|lyrics?|lyric video|audio|music video|video|hd|4k)\b|متن( آهنگ)?|با متن",
               " ", t)
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", t) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    ch = re.sub(r"(?i)\s*-\s*topic$", "", channel or "").strip()
    return ch, (parts[0] if parts else "")


def yt_lyrics_search(words):
    """Search the heard words on YouTube, then CHECK each candidate's real lyrics against them."""
    q = " ".join(words[:14]) + " lyrics"
    try:
        items = yt_search(q, 6)
    except Exception as e:
        log.warning("yt lyrics search failed: %s", e)
        return None
    for it in items[:5]:
        a, t = _parse_yt_title(it.get("title"), it.get("channel") or it.get("uploader") or "")
        for artist, name in ((a, t), (t, a)):  # titles come as "Artist - Song" or "Song - Artist"
            if not artist or not name:
                continue
            try:
                ly = get_lyrics(artist, name)
            except Exception:
                ly = None
            if ly and lyric_overlap(words, ly) >= 0.35:
                return {"title": name, "artist": artist, "link": None}
            if ly:
                break  # lyrics found but they don't match: don't try the swapped order
    return None


def lyrics_identify(paths):
    texts = []
    for p in paths[:3]:  # several pieces -> more words -> a much better match
        t = transcribe(p)
        if t:
            texts.append(t)
    if not texts:
        return None
    words = re.findall(r"\w+", " ".join(texts).lower())
    if len(words) < 6:  # instrumental / humming / too little singing
        return None
    with db() as c:
        accs = c.execute("SELECT * FROM accounts WHERE enabled=1 AND type='audd' ORDER BY id").fetchall()
    for acc in accs:
        try:
            res = audd_find_lyrics(words, acc)
            with db() as c:
                c.execute("UPDATE accounts SET uses=uses+1 WHERE id=?", (acc["id"],))
            if res:
                return res
            break
        except Exception as e:
            log.warning("lyrics search failed: %s", e)
            with db() as c:
                c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (acc["id"],))
    best = max(texts, key=len)  # the longest transcript makes the best search query
    return yt_lyrics_search(re.findall(r"\w+", best.lower()) or words)


def make_variants(src: str) -> list:
    """Slowed / sped-up (pitch + tempo) copies: reels often re-upload songs like that."""
    base = re.sub(r"_[^_/]*$", "", os.path.splitext(src)[0])
    outs = []
    for i, af in enumerate(("asetrate=44100*1.12,aresample=44100", "asetrate=44100*0.89,aresample=44100")):
        out = f"{base}v{i}.mp3"
        try:
            subprocess.run(["ffmpeg", "-y", "-i", src, "-af", af, "-b:a", "128k", out],
                           check=True, capture_output=True, timeout=60)
            outs.append(out)
        except Exception:
            pass
    return outs


# ---------------------------------------------------------------------
# STEP 4: in recognize(), right AFTER the `if LYRICS_ENGINE ...:` block
#         and BEFORE `if not ranked: return None`, add:
# ---------------------------------------------------------------------
    if not ranked and paths:  # nothing at all: try slowed / sped-up versions of the best piece
        try:
            vs = make_variants(paths[0])
            if vs:
                run_batch(list(enumerate(vs, start=50)))
                ranked = _group(hits)
        except Exception as e:
            log.warning("variant stage failed: %s", e)


# ---------------------------------------------------------------------
# STEP 5: restart
#   systemctl restart <your-bot-service>     (or restart however you run it)
#   then watch:  journalctl -u <service> -f | grep -E "acr humming|recognize"
# ---------------------------------------------------------------------
