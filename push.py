"""
RescuNet Web Push - alerts that reach a phone/computer even when the website is CLOSED.

How it works: the browser registers a service worker (static/sw.js). The push service of the browser vendor wakes
that worker when the server sends a message, and the worker shows a notification (with vibration) even if no
RescuNet tab is open. Tapping it opens the site and the full-screen siren starts.

Needs: HTTPS (or localhost), pywebpush, and VAPID keys (run  python make_vapid_keys.py ).
  VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT (e.g. mailto:you@example.com)
Honest limits: a web notification uses the phone's normal notification sound (it cannot loop a siren), and it is
silenced by Do-Not-Disturb. iPhones need the site added to the Home Screen first.
"""
import json
import logging
import os
import threading
from datetime import datetime

log = logging.getLogger("rescunet.push")
PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
SYNC = os.environ.get("PUSH_SYNC") == "1"          # send in the same thread (used by the tests)
MAX_SUBS = 5000

try:
    from pywebpush import webpush, WebPushException
except Exception:                                   # package not installed -> push stays switched off
    webpush, WebPushException = None, Exception

subs = []        # {"endpoint","keys":{"p256dh","auth"},"user_id","lat","lng","lang"}
history = []     # last send results (shown in the admin panel)
_lock = threading.Lock()
_tl = threading.local()          # drill mode: nothing is sent to phones


def suppress(flag):
    _tl.on = bool(flag)


def configured():
    return bool(webpush and PUBLIC and PRIVATE)


def status():
    missing = ([] if webpush else ["pywebpush package"]) + ([] if PUBLIC else ["VAPID_PUBLIC_KEY"]) + ([] if PRIVATE else ["VAPID_PRIVATE_KEY"])
    return {"configured": configured(), "subscribers": len(subs), "missing": missing, "recent": history[:5]}


def add(sub, user_id, lat, lng, lang):
    """Store (or refresh) a browser subscription. Returns (ok, error)."""
    try:
        endpoint, keys = sub["endpoint"], sub["keys"]
        assert isinstance(endpoint, str) and endpoint.startswith("https://") and len(endpoint) < 600
        assert isinstance(keys["p256dh"], str) and isinstance(keys["auth"], str) and len(keys["p256dh"]) < 200 and len(keys["auth"]) < 100
    except (KeyError, TypeError, AssertionError):
        return False, "Invalid push subscription"
    with _lock:
        for s in subs:
            if s["endpoint"] == endpoint:
                s.update(user_id=user_id, lat=lat, lng=lng, lang=lang, keys={"p256dh": keys["p256dh"], "auth": keys["auth"]})
                return True, None
        if len(subs) >= MAX_SUBS:
            return False, "Too many subscriptions"
        subs.append({"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                     "user_id": user_id, "lat": lat, "lng": lng, "lang": lang})
    return True, None


def remove(endpoint):
    with _lock:
        subs[:] = [s for s in subs if s["endpoint"] != endpoint]


def detach(endpoint):
    """Logout: keep the device subscribed for public alerts but forget which user it belonged to."""
    for s in subs:
        if s["endpoint"] == endpoint:
            s["user_id"] = None


def set_location(endpoint, lat, lng):
    for s in subs:
        if s["endpoint"] == endpoint:
            s["lat"], s["lng"] = lat, lng


def notify(targets, payload):
    """payload: kind, title, body (+ title_hi, body_hi), url, tag, urgent. Never blocks a web request."""
    targets = list(targets)
    if getattr(_tl, "on", False):
        history.insert(0, {"time": datetime.now().isoformat(timespec="seconds"), "kind": payload.get("kind"),
                           "targets": len(targets), "ok": 0, "failed": 0, "note": "DRILL - not sent"})
        del history[30:]
        return 0
    if not targets or not configured():
        return 0
    if SYNC:
        _send_all(targets, payload)
    else:
        threading.Thread(target=_send_all, args=(targets, payload), daemon=True).start()
    return len(targets)


def _send_all(targets, payload):
    ok = failed = 0
    dead = []
    for s in targets:
        hi = s.get("lang") == "hi"
        data = {"kind": payload.get("kind", "info"), "url": payload.get("url", "/"), "tag": payload.get("tag", "rescunet"),
                "urgent": bool(payload.get("urgent")),
                "title": (hi and payload.get("title_hi")) or payload["title"],
                "body": (hi and payload.get("body_hi")) or payload["body"]}
        try:
            webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]}, data=json.dumps(data),
                    vapid_private_key=PRIVATE, vapid_claims={"sub": SUBJECT}, ttl=3600, headers={"Urgency": "high"})
            ok += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):               # the browser says this subscription no longer exists
                dead.append(s["endpoint"])
            failed += 1
            log.warning("push failed (%s): %s", code, e)
        except Exception as e:                   # never crash the server because of a push problem
            failed += 1
            log.warning("push error: %s", e)
    for ep in dead:
        remove(ep)
    history.insert(0, {"time": datetime.now().isoformat(timespec="seconds"), "kind": payload.get("kind"),
                       "targets": len(targets), "ok": ok, "failed": failed})
    del history[30:]
