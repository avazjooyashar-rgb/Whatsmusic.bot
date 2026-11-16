"""
lyrics_search.py - پیدا کردن آهنگ از روی چند کلمه‌ی ترانه (جای find_song_by_words قبلی bot.py)

چرا قوی‌تر از قبله:
  - به‌جای یک جستجوی ۱۲ کلمه‌ای (که با یک غلط Whisper خراب می‌شد)، چند پنجره‌ی کوتاه ۷ کلمه‌ای
    از کل متن جستجو می‌شه، همه موازی
  - مقایسه‌ی کلمه‌ها غلط‌املایی‌تحمل (فازی): Whisper اغلب یکی دو حرف رو اشتباه می‌شنوه
  - همه‌ی نتیجه‌ها موازی بررسی می‌شن و «بهترین» انتخاب می‌شه، نه اولین که از آستانه رد شد
  - هر دو ترتیب «خواننده - آهنگ» و «آهنگ - خواننده» بررسی می‌شه
  - سقف زمان کل (TIMEOUT) تا Whisper صف رو معطل نکنه

استفاده (توی bot.py، بعد از تعریف find_song_by_words):
    import lyrics_search
    find_song_by_words = lyrics_search.make_finder(yt_search, get_lyrics, _parse_yt_title, fold)
"""
import difflib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, wait

log = logging.getLogger("lyrics_search")

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lyrsearch")

MIN_OVERLAP = float(os.environ.get("LYRICS_MIN_OVERLAP", "0.5"))    # کمترین درصد کلمه‌ی منطبق
MIN_HITS = int(os.environ.get("LYRICS_MIN_HITS", "4"))              # کمترین تعداد کلمه‌ی منطبق
GOOD_ENOUGH = 0.8                                                   # بالاتر از این دیگه دنبال بهتر نمی‌گرده
WIN = 7                                                             # اندازه‌ی پنجره‌ی جستجو
MAX_WINDOWS = int(os.environ.get("LYRICS_WINDOWS", "4"))
MAX_ITEMS = 10
TIMEOUT = float(os.environ.get("LYRICS_TIMEOUT", "30"))


def make_finder(yt_search, get_lyrics, parse_yt_title, fold):

    def windows(ws):
        if len(ws) <= WIN + 2:
            return [ws]
        step = max(3, (len(ws) - WIN) // max(1, MAX_WINDOWS - 1))
        out, i = [], 0
        while i + WIN <= len(ws) and len(out) < MAX_WINDOWS:
            out.append(ws[i:i + WIN])
            i += step
        if len(ws) % step and len(out) < MAX_WINDOWS:
            out.append(ws[-WIN:])
        return out

    def fuzzy_overlap(words, lyric_text):
        """-> (نسبت، تعداد) کلمه‌های منطبق، با تحمل غلط‌املایی."""
        qs = [w for w in dict.fromkeys(fold(x) for x in words) if len(w) > 2]
        if not qs:
            return 0.0, 0
        lw = set(re.findall(r"\w+", fold(lyric_text)))
        by_len = {}
        for w in lw:
            by_len.setdefault(len(w), []).append(w)
        hits = 0
        for q in qs:
            if q in lw:
                hits += 1
                continue
            if len(q) < 4:
                continue
            cands = [c for n in (len(q) - 1, len(q), len(q) + 1) for c in by_len.get(n, ())]
            if any(c[0] == q[0] and difflib.SequenceMatcher(None, q, c).ratio() >= 0.82 for c in cands):
                hits += 1
        return hits / len(qs), hits

    def search_window(win):
        try:
            return yt_search(" ".join(win) + " lyrics", 6) or []
        except Exception as e:
            log.warning("yt lyrics search failed: %s", e)
            return []

    def check(artist, name, words):
        try:
            ly = get_lyrics(artist, name)
        except Exception:
            ly = None
        if not ly:
            return None
        ratio, hits = fuzzy_overlap(words, ly)
        return (ratio, hits, artist, name)

    def find(words):
        ws = [w for w in words if len(w) > 1]
        if len(ws) < 4:
            return None

        # ۱) چند پنجره موازی جستجو می‌شه؛ نتیجه‌ها به‌صورت نوبتی (round-robin) ادغام می‌شن
        wins = windows(ws)
        results = list(_POOL.map(search_window, wins))
        items, seen = [], set()
        for rank in range(6):
            for res in results:
                if rank < len(res):
                    it = res[rank]
                    if it.get("id") and it["id"] not in seen:
                        seen.add(it["id"])
                        items.append(it)
        items = items[:MAX_ITEMS]
        if not items:
            return None

        # ۲) متن ترانه‌ی همه‌ی کاندیدها موازی گرفته و فازی مقایسه می‌شه
        futs = []
        done_pairs = set()
        for it in items:
            a, t = parse_yt_title(it.get("title"), it.get("channel") or it.get("uploader") or "")
            for artist, name in ((a, t), (t, a)):
                key = (fold(artist), fold(name))
                if artist and name and key not in done_pairs:
                    done_pairs.add(key)
                    futs.append(_POOL.submit(check, artist, name, ws))
        done, _pending = wait(futs, timeout=TIMEOUT)
        best = None
        for f in done:
            try:
                r = f.result()
            except Exception:
                continue
            if r and (best is None or (r[0], r[1]) > (best[0], best[1])):
                best = r
        for f in futs:
            f.cancel()
        if not best:
            return None
        ratio, hits, artist, name = best
        log.info("lyrics search best: %s - %s | overlap=%.2f hits=%d", artist, name, ratio, hits)
        if ratio >= MIN_OVERLAP and hits >= MIN_HITS:
            return {"title": name, "artist": artist, "link": None}
        return None

    return find
