"""
fp_engine.py - موتور تشخیص آهنگ (Audio Fingerprinting)

نیازمندی‌ها:
    sudo apt install ffmpeg
    pip install numpy scipy

استفاده سریع:
    python fp_engine.py add song.mp3 --title "Name" --artist "Artist"
    python fp_engine.py add-dir ./songs          # فایل‌ها به شکل "Artist - Title.mp3"
    python fp_engine.py recognize clip.ogg
    python fp_engine.py stats
    python fp_engine.py find longing             # جستجو در دیتابیس (id، منبع، مدت، تعداد هش)
    python fp_engine.py delete 123               # حذف یک ترک
    python fp_engine.py rename 123 --title "Longing" --artist "IMAN"
    python fp_engine.py clean-names              # تمیز کردن اسم‌های خراب (لینک، _ ، ...)

تغییرات این نسخه (API قبلی سازگار می‌ماند؛ seed_fp.py و bot.py بدون تغییر کار می‌کنند):
    * هویت ترک = «ضبط (recording)» است، نه فقط «اسم + خواننده».
      دو ضبط هم‌اسم (مثلاً IMAN - Longing با ۳:۰۷ و ۴:۱۲، یا یک قطعه‌ی پیانو که هزار نفر زدن) دیگه
      توی یک ردیف ادغام نمیشن. تشخیص «همون ضبط» با مدت‌زمان + تطبیق واقعی اثرانگشت انجام میشه.
    * UNIQUE(title, artist) از جدول tracks برداشته شد (مهاجرت خودکار، اول بکاپ بگیر).
    * ستون‌های جدید: duration (ثانیه) و file_id (فایل تلگرام همون ضبط).
    * recognize / identify حالا track_id، duration، file_id و alternatives (نسخه‌های هم‌اسم) هم برمی‌گردونه
      تا ربات دقیقاً همون نسخه رو بفرسته. پارامتر exclude برای دکمه‌ی «آهنگ اشتباه» (نتیجه‌ی بعدی).
    * learn() خودکار (source=api) اگه با نتیجه‌ی قوی دیتابیس تناقض داشت، دیتابیس رو خراب نمی‌کنه.
    * flag_wrong(track_id): ترک‌هایی که خودکار از API یاد گرفته شدن پاک میشن؛ manual/verified حفظ میشن.
    * clean_name لینک‌ها (t.me/..., http...) و @id رو هم پاک می‌کنه.
"""
import argparse
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from collections import defaultdict

import numpy as np
from scipy.ndimage import maximum_filter
from scipy.signal import spectrogram

# ---------------- تنظیمات ----------------
SAMPLE_RATE = 11025        # نرخ نمونه برداری بعد از تبدیل
NPERSEG = 2048             # اندازه پنجره FFT
HOP = 512                  # گام بین پنجره‌ها (~46ms)
PEAK_NEIGHBORHOOD = (20, 20)   # (فرکانس، زمان) برای پیدا کردن قله‌ها
PEAK_DB_BELOW_MAX = 45     # قله‌های ضعیف‌تر از (حداکثر - این مقدار dB) حذف میشن
FAN_OUT = 10               # هر قله با چند قله بعدی جفت میشه
MIN_DT, MAX_DT = 1, 200    # بازه مجاز فاصله زمانی جفت (فریم)
MIN_ALIGNED = int(os.environ.get("FP_MIN_ALIGNED", "8"))   # حداقل تعداد هش هم‌راستا برای قبول نتیجه
MIN_RATIO = 1.5            # نسبت امتیاز اول به دوم
LEARN_SKIP_SCORE = 40      # اگه تشخیص محلی از این قوی‌تر بود، یاد نگیر (جلوگیری از تکرار)
VERIFY_ALIGNED = int(os.environ.get("FP_VERIFY_ALIGNED", "12"))  # حداقل هم‌راستایی برای «همون ضبط»
DUR_TOL = float(os.environ.get("FP_DUR_TOL", "3.0"))   # اختلاف مجاز مدت (ثانیه) برای «همون ضبط»
ALT_RATIO = 0.5            # نسخه‌های هم‌اسم با امتیاز >= این نسبت از اولی در alternatives میان
DB_PATH = os.environ.get("FP_DB", "fingerprints.db")


# ---------------- اسم‌ها ----------------
_URL = re.compile(r"(?:https?://|t\.me/|www\.)\S+|(?<!\w)@\w+", re.I)
_DOMAIN = re.compile(r"\s*[~|\-\u2013]*\s*\b(?:www\.)?[\w-]+\.(?:com|ir|net|org|info|co)\b", re.I)
_JUNK = re.compile(r"\[\s*\d{2,3}\s*(?:kbps)?\s*\]|<unknown>|\(\s*official[^)]*\)|\s*-\s*topic$", re.I)
_SPLIT = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with|x|and|و)\s+|\s*[,&;/+،]\s*", re.I)


def clean_name(s: str) -> str:
    """'Red Wine ~ Music-Fa.Com' -> 'Red Wine', 'Song [128]' -> 'Song', 't.me/x_y' -> '', '<unknown>' -> ''."""
    s = _URL.sub(" ", s or "")      # قبل از تبدیل _ ، چون لینک‌ها underscore دارن
    s = s.replace("_", " ")
    s = _JUNK.sub(" ", s)
    s = _DOMAIN.sub(" ", s)
    s = re.sub(r"\s*~\s*[\w-]+\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip(" -~|")


def _fold(s: str) -> str:
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _tkey(title: str) -> str:
    # NOT stripping brackets on purpose: "Raaze del (SlowRemix)" is another recording than "Raaze del"
    return re.sub(r"[\W_]+", "", _fold(clean_name(title)))


def _akeys(artist: str) -> set:
    keys = {re.sub(r"[\W_]+", "", _fold(p)) for p in _SPLIT.split(clean_name(artist)) if p and p.strip()}
    keys.discard("")
    keys.discard("unknown")
    return keys


def _same_artist(a: set, b: set) -> bool:
    if not a or not b:
        return False
    if a & b:
        return True
    return any(x in y or y in x for x in a for y in b if len(x) >= 4 and len(y) >= 4)


def _same_name(t1: str, a1: str, t2: str, a2: str) -> bool:
    """Same title + same artist by NAME only (says nothing about being the same recording)."""
    c1, c2 = clean_name(t1).lower(), clean_name(t2).lower()
    ca1, ca2 = clean_name(a1).lower(), clean_name(a2).lower()
    if c1 and c1 == c2 and ca1 == ca2:
        return True
    k = _tkey(t1)
    if not k or k != _tkey(t2):
        return False
    ak1, ak2 = _akeys(a1), _akeys(a2)
    return _same_artist(ak1, ak2) or not ak1 or not ak2


# ---------------- صدا -> نمونه‌ها ----------------
def load_audio(path: str) -> np.ndarray:
    """هر فرمتی (mp3, ogg, opus, m4a, ...) را با ffmpeg به mono float32 تبدیل می‌کند."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='ignore')[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------- اثر انگشت ----------------
def find_peaks(samples: np.ndarray):
    """قله‌های اسپکتروگرام را برمی‌گرداند: لیست (time_idx, freq_idx) مرتب بر اساس زمان."""
    if len(samples) < NPERSEG * 2:
        return []
    _, _, spec = spectrogram(samples, fs=SAMPLE_RATE, nperseg=NPERSEG,
                             noverlap=NPERSEG - HOP, mode="magnitude")
    db = 20.0 * np.log10(spec + 1e-10)
    thresh = db.max() - PEAK_DB_BELOW_MAX
    is_peak = (maximum_filter(db, size=PEAK_NEIGHBORHOOD) == db) & (db > thresh)
    freqs, times = np.nonzero(is_peak)
    order = np.argsort(times, kind="stable")
    return list(zip(times[order].tolist(), freqs[order].tolist()))


def make_hashes(peaks):
    """لیست (hash:int, time_offset:int). هش = ترکیب f1(11bit) | f2(11bit) | dt(8bit)."""
    out = []
    n = len(peaks)
    for i in range(n):
        t1, f1 = peaks[i]
        for j in range(i + 1, min(i + 1 + FAN_OUT, n)):
            t2, f2 = peaks[j]
            dt = t2 - t1
            if MIN_DT <= dt <= MAX_DT:
                out.append(((f1 << 19) | (f2 << 8) | dt, t1))
    return out


def fingerprint_file(path: str):
    return make_hashes(find_peaks(load_audio(path)))


def duration_from_hashes(hashes):
    """Approximate length (seconds) of a WHOLE song from its hashes. None for empty input."""
    if not hashes:
        return None
    return max(off for _, off in hashes) * HOP / SAMPLE_RATE


def best_alignment(song_hashes, clip_hashes) -> int:
    """How many hashes of a short clip line up (same time shift) with the hashes of a full song.
    Used to VERIFY that a clip really comes from a given audio before it is learned."""
    idx = defaultdict(list)
    for h, off in song_hashes:
        idx[h].append(off)
    votes = defaultdict(int)
    for h, qoff in clip_hashes:
        for off in idx.get(h, ()):
            votes[off - qoff] += 1
    best = 0
    for d, c in votes.items():
        s = c + votes.get(d - 1, 0) + votes.get(d + 1, 0)
        if s > best:
            best = s
    return best


# ---------------- دیتابیس ----------------
_TRACKS_SQL = """
CREATE TABLE tracks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    source TEXT DEFAULT 'manual',
    added_at INTEGER,
    n_hashes INTEGER DEFAULT 0,
    duration REAL,
    file_id TEXT
)"""
_MIGRATE_LOCK = threading.Lock()


class FingerprintDB:
    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with _MIGRATE_LOCK:
            self._migrate()
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS fingerprints(
                hash INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                offset INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fp_hash ON fingerprints(hash);
            CREATE INDEX IF NOT EXISTS idx_fp_track ON fingerprints(track_id);
            CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
        """)
        self._backfill_durations()
        self.conn.commit()

    def _migrate(self):
        c = self.conn
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tracks'").fetchone()
        if row is None:
            c.execute(_TRACKS_SQL)
            c.commit()
            return
        if "UNIQUE" in (row[0] or "").upper():
            # old schema: UNIQUE(title, artist) merged different recordings with the same name
            c.executescript(f"""
                BEGIN;
                DROP TABLE IF EXISTS tracks_old;
                ALTER TABLE tracks RENAME TO tracks_old;
                {_TRACKS_SQL};
                INSERT INTO tracks(id, title, artist, source, added_at, n_hashes)
                    SELECT id, title, artist, source, added_at, n_hashes FROM tracks_old;
                DROP TABLE tracks_old;
                COMMIT;
            """)
            return
        cols = {r[1] for r in c.execute("PRAGMA table_info(tracks)")}
        for col, typ in (("duration", "REAL"), ("file_id", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE tracks ADD COLUMN {col} {typ}")
        c.commit()

    def _backfill_durations(self):
        """Old whole-song tracks get a duration estimated from their hashes (one-time)."""
        c = self.conn
        rows = c.execute("SELECT id FROM tracks WHERE duration IS NULL "
                         "AND source IN ('manual','verified') AND n_hashes > 0").fetchall()
        for (tid,) in rows:
            m = c.execute("SELECT MAX(offset) FROM fingerprints WHERE track_id=?", (tid,)).fetchone()[0]
            if m:
                c.execute("UPDATE tracks SET duration=? WHERE id=?", (m * HOP / SAMPLE_RATE, tid))

    # ---- ترک‌ها ----
    def _same_recording(self, cand_id, cand_dur, duration, hashes) -> bool:
        """Is the candidate track the SAME RECORDING as the new audio (duration and/or hashes)?"""
        if duration is not None and cand_dur is not None and abs(duration - cand_dur) > DUR_TOL:
            return False                                   # a different length = a different recording
        if hashes:
            rows = self.conn.execute(
                "SELECT hash, offset FROM fingerprints WHERE track_id=?", (cand_id,)).fetchall()
            if rows:
                return best_alignment(rows, hashes) >= VERIFY_ALIGNED
        return True                                        # nothing to compare with: same name = same

    def get_or_create_track(self, title, artist, source="manual", duration=None, hashes=None) -> int:
        """Returns the id of the track that IS this recording; creates a new track if there is none.
        Same name but another recording (other length / hashes don't align) -> a separate track."""
        title = clean_name(title) or (title or "").strip()
        artist = clean_name(artist) or (artist or "").strip() or "Unknown"
        for tid, t, a, dur in self.conn.execute(
                "SELECT id, title, artist, duration FROM tracks").fetchall():
            if _same_name(title, artist, t, a) and self._same_recording(tid, dur, duration, hashes):
                return tid
        cur = self.conn.execute(
            "INSERT INTO tracks(title, artist, source, added_at, duration) VALUES(?,?,?,?,?)",
            (title, artist, source, int(time.time()), duration))
        self.conn.commit()
        return cur.lastrowid

    def add_hashes(self, track_id: int, hashes):
        self.conn.executemany(
            "INSERT INTO fingerprints(hash, track_id, offset) VALUES(?,?,?)",
            [(h, track_id, off) for h, off in hashes])
        self.conn.execute("UPDATE tracks SET n_hashes = n_hashes + ? WHERE id=?",
                          (len(hashes), track_id))
        self.conn.commit()

    def lookup(self, hash_values):
        """برای مجموعه‌ای از هش‌ها، (hash, track_id, offset) برمی‌گرداند."""
        rows = []
        hv = list(hash_values)
        for i in range(0, len(hv), 500):
            chunk = hv[i:i + 500]
            q = ",".join("?" * len(chunk))
            rows.extend(self.conn.execute(
                f"SELECT hash, track_id, offset FROM fingerprints WHERE hash IN ({q})",
                chunk).fetchall())
        return rows

    def track_info(self, track_id):
        return self.conn.execute(
            "SELECT title, artist FROM tracks WHERE id=?", (track_id,)).fetchone()

    def track_row(self, track_id):
        """(title, artist, source, duration, file_id) or None."""
        return self.conn.execute(
            "SELECT title, artist, source, duration, file_id FROM tracks WHERE id=?",
            (track_id,)).fetchone()

    def set_file_id(self, track_id, file_id):
        self.conn.execute("UPDATE tracks SET file_id=? WHERE id=?", (file_id, track_id))
        self.conn.commit()

    def delete_track(self, track_id):
        self.conn.execute("DELETE FROM fingerprints WHERE track_id=?", (track_id,))
        self.conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        self.conn.commit()

    def stats(self):
        t = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        h = self.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        return {"tracks": t, "hashes": h}


# ---------------- موتور ----------------
class Engine:
    def __init__(self, db_path: str = DB_PATH):
        self.db = FingerprintDB(db_path)

    def add_track(self, path, title, artist, source="manual") -> int:
        samples = load_audio(path)
        hashes = make_hashes(find_peaks(samples))
        if not hashes:
            raise ValueError("no fingerprints extracted (file too short or silent)")
        whole = source in ("manual", "verified")   # a full song; 'api' = only a clip of the user
        duration = len(samples) / SAMPLE_RATE if whole else None
        tid = self.db.get_or_create_track(title, artist, source, duration=duration, hashes=hashes)
        conn = self.db.conn
        src, n = conn.execute("SELECT source, n_hashes FROM tracks WHERE id=?", (tid,)).fetchone()
        if src in ("manual", "verified") and n > 0:
            return tid  # this recording is already stored completely: do not double the hashes
        if whole:
            conn.execute("DELETE FROM fingerprints WHERE track_id=?", (tid,))  # replace clip-only hashes
            conn.execute("UPDATE tracks SET source=?, n_hashes=0, duration=? WHERE id=?",
                         (source, duration, tid))
            conn.commit()
        self.db.add_hashes(tid, hashes)
        return tid

    def ingest_verified(self, song_hashes, title, artist, duration=None) -> int:
        """Store the hashes of a WHOLE, verified song. Replaces unverified clip hashes of the same recording.
        Returns the number of hashes stored (0 = this recording was already complete)."""
        dur = duration if duration is not None else duration_from_hashes(song_hashes)
        tid = self.db.get_or_create_track(title, artist, "verified", duration=dur, hashes=song_hashes)
        conn = self.db.conn
        src, n = conn.execute("SELECT source, n_hashes FROM tracks WHERE id=?", (tid,)).fetchone()
        if src in ("verified", "manual") and n > 0:
            return 0
        conn.execute("DELETE FROM fingerprints WHERE track_id=?", (tid,))
        conn.execute("UPDATE tracks SET source='verified', n_hashes=0, duration=? WHERE id=?", (dur, tid))
        conn.commit()
        self.db.add_hashes(tid, song_hashes)
        return len(song_hashes)

    def _ranked(self, path, exclude=()):
        """[(track_id, score)] sorted by score, best first."""
        q_hashes = fingerprint_file(path)
        if not q_hashes:
            return []
        q_offsets = defaultdict(list)
        for h, off in q_hashes:
            q_offsets[h].append(off)

        # رأی‌گیری بر اساس (track, اختلاف زمانی)؛ آهنگ درست اختلاف ثابتی دارد
        votes = defaultdict(int)
        for h, tid, db_off in self.db.lookup(q_offsets.keys()):
            if tid in exclude:
                continue
            for q_off in q_offsets[h]:
                votes[(tid, db_off - q_off)] += 1

        # امتیاز هر آهنگ = بهترین پشته (با تحمل ±۱ فریم)
        best = {}
        for (tid, diff), c in votes.items():
            s = c + votes.get((tid, diff - 1), 0) + votes.get((tid, diff + 1), 0)
            if s > best.get(tid, 0):
                best[tid] = s
        return sorted(best.items(), key=lambda x: x[1], reverse=True)

    def recognize(self, path, exclude=()):
        """dict: track_id, title, artist, score, confidence, duration, file_id, alternatives  |  None.
        exclude = track_id هایی که نباید برگردن (دکمه‌ی «آهنگ اشتباه» -> نتیجه‌ی بعدی).
        alternatives = نسخه‌های هم‌اسم (ضبط‌های دیگه‌ی همین آهنگ) که کلیپ تا حدی بهشون می‌خوره."""
        exclude = set(exclude or ())
        ranked = self._ranked(path, exclude)
        if not ranked:
            return None
        top_tid, top_score = ranked[0]
        top = self.db.track_row(top_tid)
        second = 0
        alts = []
        for tid, sc in ranked[1:12]:
            r = self.db.track_row(tid)
            if r is None:
                continue
            if _same_name(top[0], top[1], r[0], r[1]):
                # another recording of the same song is not a "rival", it is an alternative version
                if sc >= ALT_RATIO * top_score:
                    alts.append({"track_id": tid, "title": r[0], "artist": r[1],
                                 "score": sc, "duration": r[3], "file_id": r[4]})
            elif not second:
                second = sc
            if second and sc < ALT_RATIO * top_score:
                break

        if top_score < MIN_ALIGNED or top_score < MIN_RATIO * max(second, 1):
            return None
        return {"track_id": top_tid, "title": top[0], "artist": top[1],
                "score": top_score,
                "confidence": round(top_score / (top_score + second + 10), 2),
                "duration": top[3], "file_id": top[4], "alternatives": alts}

    def learn(self, path, title, artist, source="api") -> bool:
        """
        یادگیری از کلیپ کاربر بعد از اینکه API آهنگ را شناخت.
        فقط هش‌ها ذخیره میشن (نه فایل صوتی).
          * اگه موتور خودمون از قبل همین آهنگ رو قوی تشخیص میده: تکراری ذخیره نمی‌کنیم.
          * اگه source=api و دیتابیس قوی یه آهنگ دیگه میگه: قاطی نمی‌کنیم (جلوی خراب شدن دیتابیس).
          * اگه اسم تکراری ولی کلیپ به ضبط قبلی نمی‌خوره: یه ترک جدا (نسخه‌ی دیگه) ساخته میشه.
        """
        existing = self.recognize(path)
        if existing and existing["score"] >= LEARN_SKIP_SCORE:
            if _same_name(existing["title"], existing["artist"], title, artist):
                return False
            if source == "api":
                return False
        self.add_track(path, title, artist, source=source)
        return True

    def flag_wrong(self, track_id) -> str:
        """دکمه‌ی «آهنگ اشتباه»: ترک‌های خودکار (api) پاک میشن؛ manual/verified دست نمی‌خورن.
        بعدش با recognize(path, exclude={track_id}) نتیجه‌ی بعدی رو بگیر."""
        row = self.db.track_row(track_id)
        if row is None:
            return "missing"
        if row[2] == "api":
            self.db.delete_track(track_id)
            return "deleted"
        return "kept"

    def set_file_id(self, track_id, file_id):
        self.db.set_file_id(track_id, file_id)


# ---------------- API برای bot.py ----------------
_TL = threading.local()


def _thread_engine() -> Engine:
    """One Engine (= one sqlite connection) per thread: the bot calls us from many threads at once."""
    eng = getattr(_TL, "eng", None)
    if eng is None:
        eng = _TL.eng = Engine()
    return eng


def identify(path: str, exclude=()):
    """The function bot.py calls (fp_engine.identify).
    Returns {"title", "artist", "score", "confidence", "track_id", "duration", "file_id", "alternatives"} or None."""
    return _thread_engine().recognize(path, exclude)


def flag_wrong(track_id) -> str:
    return _thread_engine().flag_wrong(track_id)


def set_file_id(track_id, file_id):
    _thread_engine().set_file_id(track_id, file_id)


# ---------------- اتصال به ربات ----------------
def recognize_with_fallback(engine: Engine, path: str, external_recognizer):
    """
    external_recognizer(path) -> (title, artist) یا None   (همون ACRCloud/Shazam خودت)
    اول دیتابیس خودمون؛ اگه نبود API؛ اگه API پیدا کرد، یاد می‌گیریم.
    برمی‌گرداند: (title, artist, source) یا None
    """
    local = engine.recognize(path)
    if local:
        return local["title"], local["artist"], "local"
    ext = external_recognizer(path)
    if ext:
        title, artist = ext
        try:
            engine.learn(path, title, artist)
        except Exception as e:  # یادگیری نباید جواب کاربر را خراب کند
            print("learn failed:", e)
        return title, artist, "api"
    return None


# ---------------- CLI ----------------
def _parse_name(filename):
    base = os.path.splitext(os.path.basename(filename))[0]
    if " - " in base:
        artist, title = base.split(" - ", 1)
        return title.strip(), artist.strip()
    return base.strip(), "Unknown"


def _fmt_dur(d):
    return "-" if not d else f"{int(d) // 60}:{int(d) % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("file")
    a.add_argument("--title", required=True); a.add_argument("--artist", required=True)
    d = sub.add_parser("add-dir"); d.add_argument("folder")
    r = sub.add_parser("recognize"); r.add_argument("file")
    sub.add_parser("stats")
    f = sub.add_parser("find"); f.add_argument("text")
    x = sub.add_parser("delete"); x.add_argument("id", type=int)
    rn = sub.add_parser("rename"); rn.add_argument("id", type=int)
    rn.add_argument("--title"); rn.add_argument("--artist")
    sub.add_parser("clean-names")
    args = ap.parse_args()

    eng = Engine()
    if args.cmd == "add":
        print("track id:", eng.add_track(args.file, args.title, args.artist))
    elif args.cmd == "add-dir":
        exts = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".opus")
        for fn in sorted(os.listdir(args.folder)):
            if fn.lower().endswith(exts):
                title, artist = _parse_name(fn)
                try:
                    eng.add_track(os.path.join(args.folder, fn), title, artist)
                    print("added:", artist, "-", title)
                except Exception as e:
                    print("skip:", fn, e)
    elif args.cmd == "recognize":
        print(eng.recognize(args.file) or "not found")
    elif args.cmd == "stats":
        print(eng.db.stats())
    elif args.cmd == "find":
        like = f"%{args.text}%"
        rows = eng.db.conn.execute(
            "SELECT id, title, artist, source, duration, n_hashes FROM tracks "
            "WHERE title LIKE ? OR artist LIKE ? ORDER BY id", (like, like)).fetchall()
        for tid, t, ar, src, dur, n in rows:
            print(f"{tid:>6} | {t} | {ar} | {src} | {_fmt_dur(dur)} | {n} hashes")
        print(len(rows), "track(s)")
    elif args.cmd == "delete":
        row = eng.db.track_row(args.id)
        if not row:
            print("no such track")
        else:
            eng.db.delete_track(args.id)
            try:
                eng.db.conn.execute("VACUUM")
            except sqlite3.Error:
                pass
            print("deleted:", row[1], "-", row[0])
    elif args.cmd == "rename":
        row = eng.db.track_row(args.id)
        if not row:
            print("no such track")
        else:
            t = clean_name(args.title) if args.title else row[0]
            ar = clean_name(args.artist) if args.artist else row[1]
            eng.db.conn.execute("UPDATE tracks SET title=?, artist=? WHERE id=?", (t, ar, args.id))
            eng.db.conn.commit()
            print("renamed:", row[1], "-", row[0], "->", ar, "-", t)
    elif args.cmd == "clean-names":
        n = 0
        for tid, t, ar in eng.db.conn.execute("SELECT id, title, artist FROM tracks").fetchall():
            nt, na = clean_name(t) or t, clean_name(ar) or "Unknown"
            if (nt, na) != (t, ar):
                eng.db.conn.execute("UPDATE tracks SET title=?, artist=? WHERE id=?", (nt, na, tid))
                print(f"{tid}: {ar} - {t}  ->  {na} - {nt}")
                n += 1
        eng.db.conn.commit()
        print(n, "track(s) cleaned")


if __name__ == "__main__":
    main()
