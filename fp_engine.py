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
"""
import argparse
import os
import sqlite3
import subprocess
import time
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
MIN_ALIGNED = 8            # حداقل تعداد هش هم‌راستا برای قبول نتیجه
MIN_RATIO = 1.5            # نسبت امتیاز اول به دوم
LEARN_SKIP_SCORE = 40      # اگه تشخیص محلی از این قوی‌تر بود، یاد نگیر (جلوگیری از تکرار)
DB_PATH = os.environ.get("FP_DB", "fingerprints.db")


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
class FingerprintDB:
    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                added_at INTEGER,
                n_hashes INTEGER DEFAULT 0,
                UNIQUE(title, artist)
            );
            CREATE TABLE IF NOT EXISTS fingerprints(
                hash INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                offset INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fp_hash ON fingerprints(hash);
            CREATE INDEX IF NOT EXISTS idx_fp_track ON fingerprints(track_id);
        """)
        self.conn.commit()

    def get_or_create_track(self, title, artist, source="manual") -> int:
        row = self.conn.execute(
            "SELECT id FROM tracks WHERE lower(title)=lower(?) AND lower(artist)=lower(?)",
            (title.strip(), artist.strip())).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO tracks(title, artist, source, added_at) VALUES(?,?,?,?)",
            (title.strip(), artist.strip(), source, int(time.time())))
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

    def stats(self):
        t = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        h = self.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        return {"tracks": t, "hashes": h}


# ---------------- موتور ----------------
class Engine:
    def __init__(self, db_path: str = DB_PATH):
        self.db = FingerprintDB(db_path)

    def add_track(self, path, title, artist, source="manual") -> int:
        hashes = fingerprint_file(path)
        if not hashes:
            raise ValueError("no fingerprints extracted (file too short or silent)")
        tid = self.db.get_or_create_track(title, artist, source)
        self.db.add_hashes(tid, hashes)
        return tid

    def ingest_verified(self, song_hashes, title, artist) -> int:
        """Store the hashes of a WHOLE, verified song. Replaces unverified clip hashes of the same track.
        Returns the number of hashes stored (0 = the track was already complete)."""
        tid = self.db.get_or_create_track(title, artist, "verified")
        conn = self.db.conn
        src, n = conn.execute("SELECT source, n_hashes FROM tracks WHERE id=?", (tid,)).fetchone()
        if src in ("verified", "manual") and n > 0:
            return 0
        conn.execute("DELETE FROM fingerprints WHERE track_id=?", (tid,))
        conn.execute("UPDATE tracks SET source='verified', n_hashes=0 WHERE id=?", (tid,))
        conn.commit()
        self.db.add_hashes(tid, song_hashes)
        return len(song_hashes)

    def recognize(self, path):
        """برمی‌گرداند dict شامل title, artist, score, confidence یا None."""
        q_hashes = fingerprint_file(path)
        if not q_hashes:
            return None
        q_offsets = defaultdict(list)
        for h, off in q_hashes:
            q_offsets[h].append(off)

        # رأی‌گیری بر اساس (track, اختلاف زمانی)؛ آهنگ درست اختلاف ثابتی دارد
        votes = defaultdict(int)
        for h, tid, db_off in self.db.lookup(q_offsets.keys()):
            for q_off in q_offsets[h]:
                votes[(tid, db_off - q_off)] += 1
        if not votes:
            return None

        # امتیاز هر آهنگ = بهترین پشته (با تحمل ±۱ فریم)
        best = {}
        for (tid, diff), c in votes.items():
            s = c + votes.get((tid, diff - 1), 0) + votes.get((tid, diff + 1), 0)
            if s > best.get(tid, 0):
                best[tid] = s
        ranked = sorted(best.items(), key=lambda x: x[1], reverse=True)
        top_tid, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0

        if top_score < MIN_ALIGNED or top_score < MIN_RATIO * max(second, 1):
            return None
        title, artist = self.db.track_info(top_tid)
        return {"track_id": top_tid, "title": title, "artist": artist,
                "score": top_score,
                "confidence": round(top_score / (top_score + second + 10), 2)}

    def learn(self, path, title, artist, source="api") -> bool:
        """
        یادگیری از کلیپ کاربر بعد از اینکه API آهنگ را شناخت.
        فقط هش‌ها ذخیره میشن (نه فایل صوتی). اگه موتور خودمون از قبل قوی تشخیص میده، تکراری ذخیره نمی‌کنیم.
        """
        existing = self.recognize(path)
        if (existing and existing["score"] >= LEARN_SKIP_SCORE
                and existing["title"].lower() == title.strip().lower()
                and existing["artist"].lower() == artist.strip().lower()):
            return False
        self.add_track(path, title, artist, source=source)
        return True


# ---------------- اتصال به ربات ----------------
def recognize_with_fallback(engine: Engine, path: str, external_recognizer):
    """
    external_recognizer(path) -> (title, artist) یا None   (همون ACRCloud/AudD/Shazam خودت)
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


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("file")
    a.add_argument("--title", required=True); a.add_argument("--artist", required=True)
    d = sub.add_parser("add-dir"); d.add_argument("folder")
    r = sub.add_parser("recognize"); r.add_argument("file")
    sub.add_parser("stats")
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


if __name__ == "__main__":
    main()
