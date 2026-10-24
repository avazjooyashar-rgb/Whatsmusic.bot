#!/usr/bin/env python3
"""
ingest_channel.py - learn every song of a Telegram channel into fingerprints.db (hashes only).

Standalone: it does NOT touch bot.py and does NOT use the bot token, so it cannot clash with the running bot.
Put it in the bot folder (next to fp_engine.py) so it uses the same fingerprints.db.

    pip install telethon
    export TG_API_ID=123456 TG_API_HASH=abcdef...      # my.telegram.org -> API development tools
    ./venv/bin/python ingest_channel.py --channel "واتس موزیک"
    ./venv/bin/python ingest_channel.py --channel "واتس موزیک" --all     # ignore the saved position

The first run asks for your phone number and the login code once (it creates ingest.session: keep it private).
Run it again whenever you add songs: it continues after the last message it handled.
Slowed / remix versions are NOT skipped here (you put them in the channel on purpose); only the duration is checked.
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile

from fp_engine import Engine, fingerprint_file

DB = os.environ.get("FP_DB", "fingerprints.db")
STATE_FILE = "ingest_state.json"
SESSION = "ingest"

_DOMAIN = re.compile(r"\s*[~|\-\u2013]*\s*\b(?:www\.)?[\w-]+\.(?:com|ir|net|org|info|co)\b", re.I)
_JUNK = re.compile(r"\[\s*\d{2,3}\s*(?:kbps)?\s*\]|<unknown>|\(\s*official[^)]*\)|\s*-\s*topic$", re.I)


def clean_name(s: str) -> str:
    """'Red Wine ~ Music-Fa.Com' -> 'Red Wine', 'Song [128]' -> 'Song', '<unknown>' -> ''."""
    s = (s or "").replace("_", " ")
    s = _JUNK.sub(" ", s)
    s = _DOMAIN.sub(" ", s)
    s = re.sub(r"\s*~\s*[\w-]+\s*$", "", s)             # 'Puzzle ~ UpMusic' -> 'Puzzle'
    return re.sub(r"\s+", " ", s).strip(" -~|")


def name_from_file(fname: str):
    """'Artist - Title.mp3' -> (title, artist); anything else -> (name, '')."""
    base = clean_name(os.path.splitext(fname or "")[0])
    if " - " in base:
        artist, title = base.split(" - ", 1)
        return title.strip(), artist.strip()
    return base, ""


def learn_file(eng: Engine, path: str, title: str, artist: str) -> int:
    hashes = fingerprint_file(path)
    if not hashes:
        raise ValueError("no fingerprints (silent / too short)")
    return eng.ingest_verified(hashes, title, artist or "Unknown")


def load_state() -> dict:
    return json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


async def find_channel(client, name: str):
    want = name.lstrip("@").lower()
    async for d in client.iter_dialogs():
        uname = (getattr(d.entity, "username", None) or "").lower()
        if d.name == name or str(d.id) == name or (uname and uname == want):
            return d.entity
    return await client.get_entity(name)


async def run(args):
    from telethon import TelegramClient
    from telethon.tl.types import (DocumentAttributeAudio, DocumentAttributeFilename,
                                   InputMessagesFilterMusic)
    try:
        api_id, api_hash = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    except KeyError:
        sys.exit("set TG_API_ID and TG_API_HASH first (my.telegram.org -> API development tools)")

    eng = Engine(DB)
    eng.db.conn.execute("PRAGMA busy_timeout=60000")      # the bot may write at the same time
    added = had = skipped = failed = 0
    async with TelegramClient(SESSION, api_id, api_hash) as client:
        ent = await find_channel(client, args.channel)
        key = str(ent.id)
        state = load_state()
        last = 0 if args.all else state.get(key, 0)
        print(f"channel: {getattr(ent, 'title', args.channel)}  (continuing after message {last})")
        async for msg in client.iter_messages(ent, reverse=True, min_id=last,
                                              filter=InputMessagesFilterMusic):
            try:
                doc = msg.document
                audio = next((a for a in doc.attributes if isinstance(a, DocumentAttributeAudio)), None)
                fname = next((a.file_name for a in doc.attributes
                              if isinstance(a, DocumentAttributeFilename)), "")
                dur = (audio.duration if audio else 0) or 0
                if dur and not (args.min_sec <= dur <= args.max_sec):
                    skipped += 1
                    print(f"   skip (duration {dur}s): {fname or msg.id}")
                    continue
                title = clean_name(audio.title if audio else "")
                artist = clean_name(audio.performer if audio else "")
                if not title:
                    title, artist2 = name_from_file(fname)
                    artist = artist or artist2
                if not title:
                    skipped += 1
                    print(f"   skip (no name): message {msg.id}")
                    continue
                tmp = tempfile.mkdtemp(prefix="chan_")
                try:
                    path = await client.download_media(msg, file=os.path.join(tmp, "a"))
                    n = await asyncio.to_thread(learn_file, eng, path, title, artist)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                if n:
                    added += 1
                    print(f"   + {artist or 'Unknown'} - {title}  ({n} hashes)")
                else:
                    had += 1
                    print(f"   = already have: {artist or 'Unknown'} - {title}")
            except Exception as e:
                failed += 1
                print(f"   ! message {msg.id}: {str(e)[:120]}")
            finally:
                state[key] = max(state.get(key, 0), msg.id)
                save_state(state)
    print(f"\nadded {added}, already had {had}, skipped {skipped}, failed {failed}")
    print(eng.db.stats())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True, help="channel title, @username or id")
    ap.add_argument("--all", action="store_true", help="ignore the saved position and go through everything")
    ap.add_argument("--min-sec", type=int, default=60)
    ap.add_argument("--max-sec", type=int, default=720)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
