"""
RescuNet SMS layer - lets alerts reach phones that have NO internet (only mobile signal).

SMS_MODE (environment variable)
  demo      (default) nothing is really sent; every message is stored in the Outbox so you can see it
  twilio    needs TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM
  fast2sms  needs FAST2SMS_KEY   (optional FAST2SMS_ROUTE, default "q"; India bulk SMS also needs DLT registration)

Sending never blocks a web request: real sends run in a background thread.
Uses only the Python standard library.
"""
import base64
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("rescunet.sms")
MODE = os.environ.get("SMS_MODE", "demo").lower()
outbox = []                      # newest first: both sent ("out") and received ("in") messages
_lock = threading.Lock()
_counter = [0]


def status():
    """Is the chosen provider configured? (shown in the admin panel; passwords are never shown)"""
    need = {"demo": [], "twilio": ["TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"], "fast2sms": ["FAST2SMS_KEY"]}.get(MODE)
    if need is None:
        return {"mode": MODE, "live": False, "ready": False, "missing": [], "note": f"Unknown SMS_MODE '{MODE}'"}
    missing = [k for k in need if not os.environ.get(k)]
    return {"mode": MODE, "live": MODE != "demo", "ready": not missing, "missing": missing}


def normalize_phone(p, default_cc="91"):
    """'98765 43210' -> '+919876543210' (10-digit numbers get the India code)."""
    d = re.sub(r"\D", "", p or "")
    if not d:
        return ""
    if len(d) == 10:
        d = default_cc + d
    elif len(d) == 11 and d.startswith("0"):
        d = default_cc + d[1:]
    return "+" + d


def _record(direction, phone, text, kind, name, status):
    with _lock:
        _counter[0] += 1
        rec = {"id": _counter[0], "time": datetime.now().isoformat(timespec="seconds"), "dir": direction,
               "to": phone, "name": name, "kind": kind, "text": text, "status": status, "mode": MODE}
        outbox.insert(0, rec)
        del outbox[300:]
    return rec


def log_inbound(phone, text, name=""):
    _record("in", phone, text, "inbound", name, "received")


def send_sms(phone, text, kind="alert", name=""):
    """Queue one SMS. Returns the outbox record (or None if the phone number is empty)."""
    to = normalize_phone(phone)
    if not to:
        return None
    rec = _record("out", to, text[:300], kind, name, "queued")
    if MODE == "demo":
        rec["status"] = "demo (not really sent)"
    else:
        threading.Thread(target=_deliver, args=(rec,), daemon=True).start()
    return rec


def send_many(recipients, text, kind="alert"):
    """recipients = [(phone, name), ...]; the same number is never messaged twice."""
    seen = set()
    for phone, name in recipients:
        n = normalize_phone(phone)
        if n and n not in seen:
            seen.add(n)
            send_sms(n, text, kind, name)
    return len(seen)


def _deliver(rec):
    try:
        if MODE == "twilio":
            _twilio(rec)
        elif MODE == "fast2sms":
            _fast2sms(rec)
        else:
            raise ValueError(f"unknown SMS_MODE '{MODE}'")
        rec["status"] = "sent"
    except Exception as e:                      # never crash the server because of an SMS problem
        rec["status"] = f"failed: {e}"[:140]
        log.warning("SMS to %s failed: %s", rec["to"], e)


def _twilio(rec):
    sid, token, sender = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"], os.environ["TWILIO_FROM"]
    data = urllib.parse.urlencode({"To": rec["to"], "From": sender, "Body": rec["text"]}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    urllib.request.urlopen(req, timeout=15).read()


def _fast2sms(rec):
    data = urllib.parse.urlencode({"route": os.environ.get("FAST2SMS_ROUTE", "q"), "message": rec["text"],
                                   "numbers": rec["to"][-10:]}).encode()
    req = urllib.request.Request("https://www.fast2sms.com/dev/bulkV2", data=data,
                                 headers={"authorization": os.environ["FAST2SMS_KEY"],
                                          "Content-Type": "application/x-www-form-urlencoded"})
    body = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if not body.get("return"):
        raise RuntimeError(str(body.get("message"))[:100])


# ---------------------------------------------------------------- incoming SMS parsing
def parse_command(text):
    """Volunteer replies: 'ACCEPT 12', 'ONWAY 12', 'REACHED 12' -> (action, alert_id)."""
    m = re.match(r"(?i)^\s*(accept|onway|on the way|reached)\s*#?(\d+)", text or "")
    if not m:
        return None
    word = m.group(1).lower()
    return ({"accept": "accept", "reached": "reached"}.get(word, "on_the_way"), int(m.group(2)))


def parse_sos(text):
    """'SOS medical 25.61,85.16' -> (urgency, lat|None, lng|None); None if it is not an SOS."""
    t = (text or "").strip()
    if not re.match(r"(?i)^sos\b", t):
        return None
    low = t.lower()
    urgency = "general"
    if any(w in low for w in ("medical", "doctor", "injur", "hurt")):
        urgency = "critical_medical"
    elif any(w in low for w in ("food", "water", "hungry")):
        urgency = "stranded_food_needed"
    m = re.search(r"(-?\d{1,2}\.\d+)\s*[,\s]\s*(-?\d{1,3}\.\d+)", t)
    return (urgency, float(m.group(1)), float(m.group(2))) if m else (urgency, None, None)
