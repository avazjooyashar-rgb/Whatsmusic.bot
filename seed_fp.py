#!/usr/bin/env python3
"""
seed_fp.py - fill our own fingerprint database from YouTube playlists / videos / searches.

Only the HASHES are kept. The downloaded audio is deleted right after it is fingerprinted.

Run from the bot folder (so it uses the same fingerprints.db as the bot):

    nice -n 10 ./venv/bin/python seed_fp.py "PLAYLIST_URL" --limit 20
    nice -n 10 ./venv/bin/python seed_fp.py --file lists.txt --limit 300
    ./venv/bin/python seed_fp.py --stats

Inputs can be playlist links, single video links, or search queries written as  ytsearch10:artist name
Best sources: album playlists and "Topic" channels (clean artist + title). Messy video titles make messy entries.
"""
import argparse
import glob
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unicodedata

import yt_dlp

from fp_engine import Engine, fingerprint_file

COOKIES = os.environ.get("COOKIES_FILE", "cookies.txt")
DB = os.environ.get("FP_DB", "fingerprints.db")
DONE_FILE = "seeded_ids.txt"  # videos already handled: a re-run continues where it stopped
MIN_SEC, MAX_SEC = 60, 720

_JUNK_BRACKETS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_JUNK_WORDS = re.compile(
    r"(?i)\b(official|lyrics?|lyric video|audio|music video|video|hd|4k|visuali[sz]er)\b|متن( آهنگ)?|با متن")
_BAD_VERSION = re.compile(
    r"(?i)mash\s?-?up|remix|medley|megamix|nightcore|slowed|sped\s?up|speed\s?up|reverb|karaoke|8d\s?audio|"
    r"bass\s?boosted|\bcover\b|\blive\b|ریمیکس|ریمیک|مشاپ|بازخوانی|کاور")


def clean(s: str) -> str:
    s = _JUNK_BRACKETS.sub(" ", s or "")
    s = _JUNK_WORDS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -–—|")


def parse_meta(info: dict):
    """(artist, title) or None when the name is not trustworthy."""
    if not info:
        return None
    raw_title = info.get("title") or ""
    if _BAD_VERSION.search(raw_title):
        return None
    artist = info.get("artist") or ", ".join(info.get("artists") or [])
    track = info.get("track")
    if artist and track:  # official music metadata (YouTube Music / Topic uploads)
        return artist.strip(), track.strip()
    t = clean(raw_title)
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", t) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    ch = re.sub(r"(?i)\s*-\s*topic$", "", info.get("channel") or info.get("uploader") or "").strip()
    if ch and parts:
        return ch, parts[0]
    return None


def ydl_opts(outdir=None, flat=False):
    o = {"quiet": True, "noplaylist": not flat, "socket_timeout": 20, "retries": 3, "fragment_retries": 3}
    if os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    if flat:
        o.update({"extract_flat": "in_playlist", "skip_download": True})
    else:
        o.update({"format": "bestaudio[abr<=130]/bestaudio/best",
                  "outtmpl": f"{outdir}/%(id)s.%(ext)s",
                  "max_filesize": 120 * 1024 * 1024,
                  "match_filter": yt_dlp.utils.match_filter_func(
                      f"duration >=? {MIN_SEC} & duration <=? {MAX_SEC} & !is_live")})
    return o


def expand(source: str, per_source_cap: int):
    """A playlist / search / video link -> list of (video_id, url)."""
    with yt_dlp.YoutubeDL(ydl_opts(flat=True)) as y:
        info = y.extract_info(source, download=False)
    entries = info.get("entries") if info else None
    if entries is None:
        entries = [info] if info else []
    out = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        out.append((vid, e.get("url") if str(e.get("url", "")).startswith("http")
                    else f"https://www.youtube.com/watch?v={vid}"))
        if len(out) >= per_source_cap:
            break
    return out


def download_one(url: str, outdir: str):
    """Returns (audio_path or None, info)."""
    with yt_dlp.YoutubeDL(ydl_opts(outdir)) as y:
        info = y.extract_info(url, download=True)
        if info and "entries" in info:
            info = (info["entries"] or [None])[0]
    files = [f for f in glob.glob(f"{outdir}/*") if not f.endswith((".part", ".ytdl", ".json", ".aria2"))]
    return (files[0] if files else None), info


def seed_file(eng: Engine, path: str, artist: str, title: str) -> int:
    hashes = fingerprint_file(path)
    if not hashes:
        raise ValueError("no fingerprints (silent / too short)")
    return eng.ingest_verified(hashes, title, artist)


def load_done():
    return set(open(DONE_FILE, encoding="utf-8").read().split()) if os.path.exists(DONE_FILE) else set()


def mark_done(vid):
    with open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(vid + "\n")


def show_stats(eng: Engine):
    s = eng.db.stats()
    size = os.path.getsize(DB) / 1e6 if os.path.exists(DB) else 0
    per = s["hashes"] // s["tracks"] if s["tracks"] else 0
    print(f"tracks: {s['tracks']}  hashes: {s['hashes']}  (~{per} per song)  db file: {size:.1f} MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*", help="playlist / video links or ytsearchN:query")
    ap.add_argument("--file", help="text file with one source per line")
    ap.add_argument("--limit", type=int, default=50, help="max NEW songs to add in this run")
    ap.add_argument("--delay", type=float, default=4.0, help="average pause between downloads (seconds)")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    eng = Engine(DB)
    eng.db.conn.execute("PRAGMA busy_timeout=60000")  # the bot may write at the same time
    if args.stats:
        return show_stats(eng)

    sources = list(args.sources)
    if args.file:
        sources += [ln.strip() for ln in open(args.file, encoding="utf-8")
                    if ln.strip() and not ln.startswith("#")]
    if not sources:
        sys.exit("give at least one playlist link / search (or --stats)")

    done = load_done()
    added = skipped = failed = streak = 0
    for src in sources:
        if added >= args.limit:
            break
        print(f"\n== {src}")
        try:
            vids = expand(src, per_source_cap=args.limit * 3)
        except Exception as e:
            print("   cannot read this source:", str(e)[:120])
            continue
        for vid, url in vids:
            if added >= args.limit:
                break
            if vid in done:
                continue
            tmp = tempfile.mkdtemp(prefix="seed_")
            try:
                path, info = download_one(url, tmp)
                if not path:
                    skipped += 1
                    mark_done(vid)
                    print(f"   skip (too short/long/unavailable): {vid}")
                    continue
                streak = 0
                meta = parse_meta(info)
                if not meta:
                    skipped += 1
                    mark_done(vid)
                    print(f"   skip (untrusted name): {(info or {}).get('title')}")
                    continue
                artist, title = meta
                n = seed_file(eng, path, artist, title)
                mark_done(vid)
                if n:
                    added += 1
                    print(f"   + {artist} - {title}  ({n} hashes)   [{added}/{args.limit}]")
                else:
                    skipped += 1
                    print(f"   = already have: {artist} - {title}")
            except Exception as e:
                failed += 1
                streak += 1
                print(f"   ! {vid}: {str(e)[:120]}")
                if streak >= 5:
                    print("\n5 downloads failed in a row: YouTube may be limiting this server. Stopping.\n"
                          "Wait an hour (or check cookies.txt) and run again, it continues where it stopped.")
                    show_stats(eng)
                    return
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            time.sleep(random.uniform(args.delay * 0.5, args.delay * 1.5))

    print(f"\nadded {added}, skipped {skipped}, failed {failed}")
    show_stats(eng)


if __name__ == "__main__":
    main()
