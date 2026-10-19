"""Account tester: calls every ACRCloud / AudD account for real and says exactly what state it is in.

Drop this next to panel.py (admin/tester.py). See the two small edits for panel.py and shared.py.
Each test uses ONE request from each account's quota.
"""
import asyncio
import base64
import hashlib
import hmac
import io
import logging
import math
import struct
import time
import wave

import httpx
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

from . import shared as S

log = logging.getLogger("admin.tester")

TIMEOUT = 25
PARALLEL = 5

LABEL = {
    "ok": "🟢 سالم",
    "quota": "🟡 سهمیه تموم شده",
    "invalid": "🔴 کلید/امضا نامعتبر",
    "net": "⚪️ خطای شبکه",
    "unknown": "🟠 پاسخ ناشناخته",
}


def sample_wav(seconds: int = 6, rate: int = 8000) -> bytes:
    """A short synthetic tone. Valid audio, but not a real song -> 'no result' is the expected answer."""
    frames = bytearray()
    for i in range(seconds * rate):
        t = i / rate
        v = (math.sin(2 * math.pi * (300 + 120 * math.sin(1.3 * t)) * t)
             + 0.6 * math.sin(2 * math.pi * (520 + 80 * math.sin(0.7 * t)) * t)
             + 0.4 * math.sin(2 * math.pi * 880 * t))
        frames += struct.pack("<h", int(5500 * v))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _res(state: str, t0: float, detail: str) -> dict:
    return {"state": state, "ms": int((time.monotonic() - t0) * 1000), "detail": detail}


async def test_acr(cli: httpx.AsyncClient, acc, wav: bytes) -> dict:
    t0 = time.monotonic()
    host = (acc["host"] or "").strip().replace("https://", "").replace("http://", "").strip("/")
    if not host or not acc["key"] or not acc["secret"]:
        return _res("invalid", t0, "host/key/secret ناقصه")
    ts = str(int(time.time()))
    to_sign = "\n".join(["POST", "/v1/identify", acc["key"], "audio", "1", ts])
    sig = base64.b64encode(
        hmac.new(acc["secret"].encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    data = {"access_key": acc["key"], "sample_bytes": str(len(wav)), "timestamp": ts,
            "signature": sig, "data_type": "audio", "signature_version": "1"}
    try:
        r = await cli.post(f"https://{host}/v1/identify", data=data,
                           files={"sample": ("t.wav", wav, "audio/wav")})
    except httpx.TimeoutException:
        return _res("net", t0, "timeout")
    except httpx.HTTPError as e:
        return _res("net", t0, type(e).__name__)
    try:
        st = r.json().get("status", {})
    except Exception:
        return _res("net", t0, f"HTTP {r.status_code} (جواب JSON نبود، host رو چک کن)")
    code, msg = st.get("code"), st.get("msg", "")
    if code in (0, 1001):
        return _res("ok", t0, "کلید و امضا درسته" + (" | آهنگ پیدا نشد (طبیعیه)" if code == 1001 else ""))
    if code == 2004:  # auth already passed before fingerprinting
        return _res("ok", t0, "کلید درسته (صدای تست فینگرپرینت نشد)")
    if code in (3001, 3014):
        return _res("invalid", t0, f"کد {code}: {msg}")
    if code == 3003:
        return _res("quota", t0, f"کد {code}: {msg}")
    return _res("unknown", t0, f"کد {code}: {msg}")


async def test_audd(cli: httpx.AsyncClient, acc, wav: bytes) -> dict:
    t0 = time.monotonic()
    if not acc["key"]:
        return _res("invalid", t0, "توکن خالیه")
    try:
        r = await cli.post("https://api.audd.io/", data={"api_token": acc["key"]},
                           files={"file": ("t.wav", wav, "audio/wav")})
    except httpx.TimeoutException:
        return _res("net", t0, "timeout")
    except httpx.HTTPError as e:
        return _res("net", t0, type(e).__name__)
    try:
        js = r.json()
    except Exception:
        return _res("net", t0, f"HTTP {r.status_code} (جواب JSON نبود)")
    if js.get("status") == "success":
        return _res("ok", t0, "توکن درسته" + (" | آهنگ پیدا نشد (طبیعیه)" if not js.get("result") else ""))
    err = js.get("error") or {}
    code, msg = err.get("error_code"), err.get("error_message", "")
    if code == 900:
        return _res("invalid", t0, f"کد {code}: {msg}")
    if code in (901, 902):
        return _res("quota", t0, f"کد {code}: {msg}")
    return _res("unknown", t0, f"کد {code}: {msg}")


async def test_all(rows) -> list:
    wav = sample_wav()
    sem = asyncio.Semaphore(PARALLEL)
    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        async def one(acc):
            async with sem:
                try:
                    fn = test_acr if acc["type"] == "acr" else test_audd
                    return await fn(cli, acc, wav)
                except Exception as e:  # never let one account kill the report
                    log.exception("test failed for #%s", acc["id"])
                    return {"state": "unknown", "ms": 0, "detail": f"{type(e).__name__}: {e}"}
        return await asyncio.gather(*(one(a) for a in rows))


def report(rows, results) -> str:
    counts = {}
    lines = []
    for acc, res in zip(rows, results):
        counts[res["state"]] = counts.get(res["state"], 0) + 1
        enabled = "فعال" if acc["enabled"] else "غیرفعال"
        lines.append(
            f'{LABEL[res["state"]]}\n'
            f'#{acc["id"]} {acc["type"]} …{(acc["key"] or "")[-4:]} | {enabled} | {res["ms"]}ms\n'
            f'↳ {res["detail"]}\n'
            f'📈 استفاده {acc["uses"]} | خطا {acc["errors"]}')
    summary = " | ".join(f'{LABEL[k].split(" ")[0]} {v}' for k, v in counts.items())
    return f"🧪 نتیجه تست اکانت‌ها\n{summary}\n\n" + "\n\n".join(lines)


async def run_test(q, ctx):
    with S.core.db() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    if not rows:
        return await q.edit_message_text("هیچ اکانتی نیست.", reply_markup=S.BACK)
    try:
        await q.edit_message_text(f"🧪 دارم {len(rows)} اکانت رو تست می‌کنم...")
    except Exception:
        pass
    results = await test_all(rows)
    ctx.user_data["acc_bad"] = [a["id"] for a, r in zip(rows, results) if r["state"] == "invalid"]
    kb = [[Btn("🔄 تست دوباره", callback_data="adm:test")]]
    if ctx.user_data["acc_bad"]:
        kb.append([Btn(f'⏸ غیرفعال‌کردن {len(ctx.user_data["acc_bad"])} اکانت نامعتبر',
                       callback_data="adm:testoff")])
    kb.append([Btn("⬅️ بازگشت", callback_data="adm:menu")])
    text = report(rows, results)
    try:
        await q.edit_message_text(text[:4000], reply_markup=Markup(kb))
    except Exception:
        log.exception("could not show report")


async def disable_bad(q, ctx):
    ids = ctx.user_data.pop("acc_bad", [])
    if ids:
        with S.core.db() as c:
            c.executemany("UPDATE accounts SET enabled=0 WHERE id=?", [(i,) for i in ids])
    await q.edit_message_text(f"⏸ {len(ids)} اکانت نامعتبر غیرفعال شد.", reply_markup=S.BACK)
