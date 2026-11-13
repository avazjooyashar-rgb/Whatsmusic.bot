#!/usr/bin/env python3
"""patch_bot_v11.py - small bot.py changes that go with the new recognition.py (v11).

    cd ~/whatsmusic-bot && python3 patch_bot_v11.py        # patches ./bot.py (a backup is made first)

What it changes in bot.py:
  1. identify(): a weak own-DB match is no longer thrown away, it goes into the engine vote
  2. a "best guess" answer (song["guess"]) shows a short note under the song name; guesses are never cached
  3. the lyrics stage (words_fn) gets a looser fallback, so a song is also found when no lyrics site knows the text
Every change is checked first: if one place is not found exactly once, NOTHING is written.
"""
import os
import py_compile
import shutil
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "bot.py"
src = open(path, encoding="utf-8").read()

EDITS = []


def edit(old, new):
    EDITS.append((old, new))


# 1) the weak DB vote goes to the engines
edit("    song = await asyncio.to_thread(recognition.ask_local, paths, rejected)\n    if song:\n        return song\n",
     "    song, weak = await asyncio.to_thread(recognition.ask_local_full, paths, rejected)\n    if song:\n        return song\n")
edit("        song = await asyncio.to_thread(recognition.ask_engines, paths, rejected)\n",
     "        song = await asyncio.to_thread(recognition.ask_engines, paths, rejected, None, None, weak)\n")

# 2) guess note + no caching of guesses
edit("LANG_CACHE = {}\n",
     'LANG_CACHE = {}\n'
     'STR["fa"]["guess_note"] = "⚠️ این یه حدسه و ممکنه اشتباه باشه. اگه درست نبود «آهنگ اشتباه» رو بزن."\n'
     'STR["en"]["guess_note"] = "⚠️ This is a best guess and may be wrong. If it is, tap “Wrong song”."\n')
edit("def result_caption(song: dict, src=None) -> str:\n",
     'def result_caption(song: dict, src=None, lang: str = "fa") -> str:\n'
     '    text = _result_caption_base(song, src)\n'
     '    if song.get("guess"):\n'
     '        head, _, tail = text.rpartition("\\n\\n")\n'
     '        text = f"{head}\\n\\n{tr(lang, \'guess_note\')}\\n\\n{tail}"\n'
     '    return text\n'
     '\n\n'
     'def _result_caption_base(song: dict, src=None) -> str:\n')
edit('reply_text(result_caption(song, src), parse_mode="HTML",',
     'reply_text(result_caption(song, src, lang), parse_mode="HTML",')
edit('result_caption(song, entry["src"])', 'result_caption(song, entry["src"], lang)')
edit("cap, kb = result_caption(song, src), result_keyboard(sid, song, lang)",
     "cap, kb = result_caption(song, src, lang), result_keyboard(sid, song, lang)")
edit("    data = _song_json(song) if song else None\n",
     '    data = _song_json(song) if song and not song.get("guess") else None\n')
edit("def media_cache_put(uid: str, song: dict, sid: str):\n",
     'def media_cache_put(uid: str, song: dict, sid: str):\n    if song.get("guess"):\n        return\n')

# 3) looser lyrics search (only for the recognition lyrics stage, never for what users type)
edit("def find_song_by_words(words):\n", "def _find_song_by_words_strict(words):\n")
edit("def footer(src=None) -> str:\n",
     'def _loose_song_by_words(words):\n'
     '    """Last resort of the lyrics stage: no lyrics site knows the text, so the first sensible YouTube result of the\n'
     '    words is used. With 14+ words a second search (other words of the same text) must find the same video."""\n'
     '    def hits(ws):\n'
     '        try:\n'
     '            items = yt_search(" ".join(ws) + " lyrics", 6)\n'
     '        except Exception as e:\n'
     '            log.warning("loose lyrics search failed: %s", e)\n'
     '            return []\n'
     '        return [it for it in items if it.get("id") and it.get("title") and not _JUNK_YT_RE.search(it["title"])\n'
     '                and 60 <= (it.get("duration") or 0) <= 720]\n'
     '\n'
     '    first = hits(words[:12])\n'
     '    if not first:\n'
     '        return None\n'
     '    it = first[0]\n'
     '    if len(words) >= 14 and it["id"] not in {x["id"] for x in hits(words[6:18])[:3]}:\n'
     '        return None\n'
     '    artist, title = _parse_yt_title(it["title"], it.get("channel") or it.get("uploader") or "")\n'
     '    if not artist or not title:\n'
     '        return None\n'
     '    return {"title": title, "artist": artist, "link": None, "loose": True}\n'
     '\n\n'
     'def find_song_by_words(words, loose=False):\n'
     '    song = _find_song_by_words_strict(words)\n'
     '    if song or not loose:\n'
     '        return song\n'
     '    return _loose_song_by_words(words)\n'
     '\n\n'
     'def footer(src=None) -> str:\n')
edit("recognition.init(load_accounts, local_fn=_fp_function(), words_fn=find_song_by_words)",
     "recognition.init(load_accounts, local_fn=_fp_function(), words_fn=lambda w: find_song_by_words(w, loose=True))")

bad = [(i, old[:70]) for i, (old, _) in enumerate(EDITS, 1) if src.count(old) != 1]
if bad:
    for i, o in bad:
        print(f"edit {i} not applied: found {src.count(EDITS[i - 1][0])}x (needs exactly 1): {o!r}")
    sys.exit("nothing was changed")
for old, new in EDITS:
    src = src.replace(old, new, 1)
shutil.copyfile(path, path + ".bak_before_v11")
open(path, "w", encoding="utf-8").write(src)
py_compile.compile(path, doraise=True)
print(f"OK: {len(EDITS)} edits applied to {path} (backup: {path}.bak_before_v11)")
