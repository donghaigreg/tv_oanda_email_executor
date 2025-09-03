#!/usr/bin/env python3
"""
TradingView Email → OANDA Executor (No Webhook, Railway-friendly)
- Polls an IMAP inbox for unread TradingView alerts
- Parses simple key=value lines in the email body
- Places/closes orders on OANDA (practice or live)

Env Vars to set in Railway:
  OANDA_API_KEY
  OANDA_ACCOUNT_ID
  OANDA_ENV                (practice | live) [default: practice]
  TV_EMAIL_HOST            (e.g., imap.gmail.com)
  TV_EMAIL_PORT            (e.g., 993)
  TV_EMAIL_USER            (email inbox for alerts)
  TV_EMAIL_PASS            (password or app password)
  TV_ALLOWED_FROM          (optional; e.g., noreply@tradingview.com)
  TV_SHARED_SECRET         (must match SECRET=... in TV alert body)
  BOT_POLL_SECONDS         (optional; default: 15)

Run locally (optional):
  pip install -r requirements.txt
  python oanda_tv_email_executor.py --poll 15
"""

import os
import time
import json
import email
import imaplib
import requests
from email.header import decode_header
from typing import Dict, Any, Optional
from datetime import datetime

# -------- Env --------
OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV        = os.getenv("OANDA_ENV", "practice").lower()
TV_EMAIL_HOST    = os.getenv("TV_EMAIL_HOST", "")
TV_EMAIL_PORT    = int(os.getenv("TV_EMAIL_PORT", "993"))
TV_EMAIL_USER    = os.getenv("TV_EMAIL_USER", "")
TV_EMAIL_PASS    = os.getenv("TV_EMAIL_PASS", "")
TV_ALLOWED_FROM  = os.getenv("TV_ALLOWED_FROM", "")   # optional safety filter
TV_SHARED_SECRET = os.getenv("TV_SHARED_SECRET", "")
BOT_POLL_SECONDS = int(os.getenv("BOT_POLL_SECONDS", "15"))

BASE_URL = "https://api-fxtrade.oanda.com/v3" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com/v3"

missing = [name for name, val in [
  ("OANDA_API_KEY", OANDA_API_KEY),
  ("OANDA_ACCOUNT_ID", OANDA_ACCOUNT_ID),
  ("TV_EMAIL_HOST", TV_EMAIL_HOST),
  ("TV_EMAIL_USER", TV_EMAIL_USER),
  ("TV_EMAIL_PASS", TV_EMAIL_PASS),
  ("TV_SHARED_SECRET", TV_SHARED_SECRET),
] if not val]

if missing:
    raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

S = requests.Session()
S.headers.update({
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json"
})

def _fmt_price(p: float, dp: int = 5) -> str:
    return f"{p:.{dp}f}"

def market_order(instrument: str, units: int, tp: Optional[float] = None, sl: Optional[float] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/orders"
    order: Dict[str, Any] = {
        "type": "MARKET",
        "instrument": instrument,
        "units": str(int(units)),
        "timeInForce": "FOK",
        "positionFill": "DEFAULT"
    }
    if tp is not None:
        order["takeProfitOnFill"] = {"price": _fmt_price(tp)}
    if sl is not None:
        order["stopLossOnFill"] = {"price": _fmt_price(sl)}
    r = S.post(url, data=json.dumps({"order": order}), timeout=20)
    r.raise_for_status()
    return r.json()

def close_position(instrument: str, side: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/accounts/{OANDA_ACCOUNT_ID}/positions/{instrument}/close"
    if side == "long":
        payload = {"longUnits": "ALL"}
    elif side == "short":
        payload = {"shortUnits": "ALL"}
    else:
        payload = {"longUnits": "ALL", "shortUnits": "ALL"}
    r = S.put(url, data=json.dumps(payload), timeout=20)
    if r.status_code >= 300 and r.status_code != 404:
        r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"status": "ok"}

def parse_kv(text: str) -> Dict[str, str]:
    """
    Parse key=value lines. Returns uppercase keys.
    Example TV alert body:
      SECRET=supersecret123
      INSTRUMENT=EUR_USD
      ACTION=LONG_ENTRY
      QTY=50000
      TP=1.10500
      SL=1.10000
    """
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out

def act_on_payload(d: Dict[str, str]) -> str:
    if d.get("SECRET", "") != TV_SHARED_SECRET:
        return "IGNORED (bad secret)"

    instrument = d.get("INSTRUMENT", "")
    action     = (d.get("ACTION", "") or "").upper()
    qty_str    = d.get("QTY", "0")
    tp_str     = d.get("TP", "")
    sl_str     = d.get("SL", "")
    if not instrument or not action:
        return "IGNORED (missing instrument/action)"

    # Optional TP/SL
    tp = float(tp_str) if tp_str else None
    sl = float(sl_str) if sl_str else None

    # Exits
    if action == "EXIT_LONG":
        close_position(instrument, "long")
        return f"EXIT_LONG {instrument}"
    if action == "EXIT_SHORT":
        close_position(instrument, "short")
        return f"EXIT_SHORT {instrument}"

    # Entries
    qty = int(qty_str) if qty_str else 0
    if qty <= 0 and action in ("LONG_ENTRY", "SHORT_ENTRY"):
        return "IGNORED (qty<=0)"

    if action == "LONG_ENTRY":
        market_order(instrument, qty, tp=tp, sl=sl)
        return f"LONG_ENTRY {instrument} qty={qty} tp={tp} sl={sl}"
    if action == "SHORT_ENTRY":
        market_order(instrument, -qty, tp=tp, sl=sl)
        return f"SHORT_ENTRY {instrument} qty={qty} tp={tp} sl={sl}"

    return "IGNORED (unknown action)"

def decode_part(msg) -> str:
    payload = msg.get_payload(decode=True)
    try:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")

def fetch_unseen_and_execute():
    M = imaplib.IMAP4_SSL(TV_EMAIL_HOST, TV_EMAIL_PORT)
    M.login(TV_EMAIL_USER, TV_EMAIL_PASS)
    M.select("INBOX")
    typ, data = M.search(None, 'UNSEEN')
    if typ != "OK":
        M.logout()
        return

    for num in data[0].split():
        try:
            typ, msgdata = M.fetch(num, '(RFC822)')
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            from_hdr = msg.get("From", "")
            subject = msg.get("Subject", "")
            decoded = decode_header(subject)
            subject_clean = "".join([(t.decode(enc or "utf-8") if isinstance(t, bytes) else t) for t, enc in decoded])

            if TV_ALLOWED_FROM and TV_ALLOWED_FROM.lower() not in from_hdr.lower():
                M.store(num, '+FLAGS', '\\Seen')
                continue

            body_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body_text += decode_part(part) + "\n"
            else:
                if msg.get_content_type() == "text/plain":
                    body_text = decode_part(msg)

            # Prefer body; fallback to subject
            content = body_text if body_text.strip() else subject_clean
            d = parse_kv(content)

            result = "IGNORED (no parsable content)"
            try:
                result = act_on_payload(d)
            except requests.HTTPError as e:
                result = f"HTTP ERROR: {e} | {getattr(e, 'response', None) and e.response.text}"
            except Exception as e:
                result = f"ERROR: {e}"

            print(f"[{datetime.utcnow().isoformat()}Z] From={from_hdr} Subject={subject_clean} -> {result}")
        finally:
            # Always mark as seen so we don't loop same mail forever
            M.store(num, '+FLAGS', '\\Seen')

    M.close()
    M.logout()

def main():
    print(f"TV Email → OANDA executor started. Env={OANDA_ENV} Poll={BOT_POLL_SECONDS}s")
    while True:
        try:
            fetch_unseen_and_execute()
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
        time.sleep(BOT_POLL_SECONDS)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TV Email → OANDA Executor")
    ap.add_argument("--poll", type=int, default=BOT_POLL_SECONDS, help="seconds between inbox polls")
    args = ap.parse_args()
    if args.poll != BOT_POLL_SECONDS:
        BOT_POLL_SECONDS = args.poll
    main()
