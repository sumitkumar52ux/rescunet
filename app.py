"""
RescuNet - Flask + Flask-SocketIO backend (prototype) with Role-Based Access Control.

Roles
  citizen    - view public data, send a PRIVATE SOS (never the siren)
  volunteer  - citizen rights + receives nearby SOS alerts
  admin      - authorized officials / camp managers: /admin, MASTER SIREN, sensors, camps

Every permission is enforced HERE on the server (hiding a button in the UI is not security).
Run:  python app.py        (admin login: see ADMIN_USER / ADMIN_PASSWORD below)
"""
import hmac
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime
from functools import wraps
from itertools import count

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)
from flask_socketio import SocketIO, join_room
from werkzeug.security import check_password_hash, generate_password_hash

import push         # Web Push (push.py): alerts even when the website is closed
import sms          # SMS layer (sms.py): alerts to phones without internet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rescunet")

app = Flask(__name__)
# Set SECRET_KEY in the environment to keep logins valid across restarts.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(24)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "") == "1")   # set COOKIE_SECURE=1 when hosted on https
# Same-origin only (no "*"): cross-site pages cannot open an authenticated socket.
# ALLOWED_ORIGINS="https://your-app.up.railway.app" (comma separated) is optional; default = same origin
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()] or None
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=_origins)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@123")   # CHANGE for any real use
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")                    # control-room number that receives SMS alerts
SMS_GATEWAY_NUMBER = os.environ.get("SMS_GATEWAY_NUMBER", "")        # number people text when offline (shown in the app)
SMS_WEBHOOK_SECRET = os.environ.get("SMS_WEBHOOK_SECRET", "sms-demo-key")   # protects /api/sms/incoming
IOT_API_KEY = os.environ.get("IOT_API_KEY", "iot-demo-key")       # key sent by real sensor hardware
if os.environ.get("PORT") and ADMIN_PASSWORD == "Admin@123":
    log.warning("SECURITY: running on a host with the DEFAULT admin password. Set ADMIN_PASSWORD, SECRET_KEY, IOT_API_KEY and SMS_WEBHOOK_SECRET now.")
NEARBY_KM = 10          # volunteers within this radius get an SOS
SOS_COOLDOWN_S = 30     # anti-abuse: one SOS per user per 30 s
ALERT_RADIUS_KM = 25    # users within this distance of a sensor get its warnings/siren
WATCH_LEVEL = 12.0      # "rising fast" warnings only matter from this level upwards
RISE_STEP_M = 0.5       # a jump of this much between two readings counts as "rising fast"
SIREN_REPEAT_S = 120    # while a river stays above danger level, repeat the siren at most this often
WARN_REPEAT_S = 120     # same for "rising fast" warnings

# --------------------------------------------------------------------------
# In-memory "database" (each structure mirrors a table)
# --------------------------------------------------------------------------
_ids = {"user": count(1), "alert": count(1), "report": count(1)}
users = {}              # id -> {id, name, username, password_hash, phone, role, skill, lat, lng, last_sos}
distress_alerts = []    # SOS log
audit_log = []          # who did what (siren triggers, sensor/camp edits) - legal accountability
login_failures = {}     # username -> (count, locked_until)

relief_camps = {
    1: {"id": 1, "name": "Gandhi Maidan Camp", "lat": 25.6180, "lng": 85.1415, "capacity": 500, "occupied": 410, "food_stock": 18, "medical_stock": 40},
    2: {"id": 2, "name": "Kosi Relief School Shelter", "lat": 25.5500, "lng": 85.2400, "capacity": 300, "occupied": 120, "food_stock": 75, "medical_stock": 60},
    3: {"id": 3, "name": "Ganga Ghat Community Hall", "lat": 25.6230, "lng": 85.1950, "capacity": 200, "occupied": 195, "food_stock": 42, "medical_stock": 12},
}
iot_water_sensors = {
    1: {"id": 1, "name": "Ganges River Gauge", "lat": 25.6200, "lng": 85.1900, "current_level": 10.0, "baseline": 10.0,
        "warning_level": 14.0, "danger_level": 15.0, "state": "normal", "trend": 0.0, "updated_at": None},
    2: {"id": 2, "name": "Kosi Basin Level", "lat": 25.5400, "lng": 85.2600, "current_level": 10.0, "baseline": 10.0,
        "warning_level": 14.0, "danger_level": 15.0, "state": "normal", "trend": 0.0, "updated_at": None},
}
sensor_meta = {sid: {"last_siren": 0.0, "last_warn": 0.0} for sid in iot_water_sensors}   # internal, not sent to clients
connected = set()          # socket ids currently connected
client_locations = {}      # socket id -> (lat, lng) for users who allowed location
flood_zones = {
    1: {"id": 1, "name": "Gandhi Ghat", "lat": 25.6200, "lng": 85.1900, "radius": 1800, "base_risk": "medium", "sensor_id": 1, "risk": "medium"},
    2: {"id": 2, "name": "Digha Riverside", "lat": 25.6450, "lng": 85.0900, "radius": 2200, "base_risk": "medium", "sensor_id": 1, "risk": "medium"},
    3: {"id": 3, "name": "Kosi Basin Villages", "lat": 25.5400, "lng": 85.2600, "radius": 2600, "base_risk": "low", "sensor_id": 2, "risk": "low"},
    4: {"id": 4, "name": "Kankarbagh", "lat": 25.5900, "lng": 85.1700, "radius": 1500, "base_risk": "low", "sensor_id": 2, "risk": "low"},
    5: {"id": 5, "name": "Bailey Road (higher ground)", "lat": 25.6150, "lng": 85.0800, "radius": 1400, "base_risk": "safe", "sensor_id": None, "risk": "safe"},
}
rescue_teams = {
    1: {"id": 1, "name": "Team 1", "lat": 25.6120, "lng": 85.1700, "status": "On Duty", "distance_km": 2.3},
    2: {"id": 2, "name": "Team 2", "lat": 25.6350, "lng": 85.1100, "status": "On Duty", "distance_km": 4.7},
    3: {"id": 3, "name": "Team 3", "lat": 25.5700, "lng": 85.2200, "status": "On Duty", "distance_km": 5.1},
}
last_siren = {"payload": None, "time": 0.0}   # lets a device that opens the site from a notification start the siren screen
community_reports = []
notifications = [{"type": "info", "title": "RescuNet is live", "body": "Monitoring river sensors and relief camps.", "created_at": ""}]

URGENCY_LEVELS = {"critical_medical", "stranded_food_needed", "general"}
SELF_REGISTER_ROLES = {"citizen", "volunteer"}      # 'admin' / 'camp_manager' can NEVER be self-registered
PRIORITY = {"critical_medical": "high", "stranded_food_needed": "medium", "general": "low"}


def now():
    return datetime.now().isoformat(timespec="seconds")


def seed_admin():
    uid = next(_ids["user"])
    users[uid] = {"id": uid, "name": "Control Room", "username": ADMIN_USER.lower(),
                  "password_hash": generate_password_hash(ADMIN_PASSWORD), "phone": ADMIN_PHONE, "role": "admin",
                  "skill": "", "lat": None, "lng": None, "last_sos": 0}


seed_admin()


# --------------------------------------------------------------------------
# Auth helpers + RBAC decorators
# --------------------------------------------------------------------------
def current_user():
    return users.get(session.get("uid"))


def public_user(u):
    return {"id": u["id"], "name": u["name"], "role": u["role"]} if u else None


def api_roles(*roles):
    """API guard: 401 if not logged in, 403 if the role is not allowed."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            u = current_user()
            if not u:
                return jsonify(error="Login required"), 401
            if u["role"] not in roles:
                return jsonify(error="Forbidden: your role is not authorized for this action"), 403
            return fn(*a, **k)
        return wrapper
    return deco


def audit(actor, action, detail=""):
    audit_log.insert(0, {"time": now(), "actor": actor, "action": action, "detail": detail})
    del audit_log[200:]


def haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------
# Domain helpers
# --------------------------------------------------------------------------
def sensor_state(s):
    if s["current_level"] >= s["danger_level"]:
        return "critical"
    return "warning" if s["current_level"] >= s["warning_level"] else "normal"


def stock_status(pct):
    return "Critical" if pct < 25 else ("Low" if pct < 50 else "Sufficient")


def camp_view(c):
    return {**c, "food_status": stock_status(c["food_stock"]), "medical_status": stock_status(c["medical_stock"])}


def push_notification(ntype, title, body):
    n = {"type": ntype, "title": title, "body": body, "created_at": now()}
    notifications.insert(0, n)
    del notifications[30:]
    socketio.emit("notification", n)


def refresh_zones():
    for z in flood_zones.values():
        sensor = iot_water_sensors.get(z["sensor_id"])
        st = sensor["state"] if sensor else "normal"
        if z["base_risk"] == "safe":
            z["risk"] = "safe"
        elif st == "critical":
            z["risk"] = "high"
        elif st == "warning" and z["base_risk"] == "low":
            z["risk"] = "medium"
        else:
            z["risk"] = z["base_risk"]
    socketio.emit("zones_update", list(flood_zones.values()))


def alert_targets(sensor):
    """Socket ids that should hear about this sensor: inside its radius, or location unknown (safe default)."""
    out = []
    for sid in list(connected):
        loc = client_locations.get(sid)
        if loc is None or haversine_km(sensor["lat"], sensor["lng"], loc[0], loc[1]) <= ALERT_RADIUS_KM:
            out.append(sid)
    return out


def sms_recipients_for_sensor(sensor):
    """Admins always; everyone else with a phone number if inside the sensor radius or location unknown."""
    return [(u["phone"], u["name"]) for u in users.values() if u["phone"] and (
        u["role"] == "admin" or u["lat"] is None or
        haversine_km(sensor["lat"], sensor["lng"], u["lat"], u["lng"]) <= ALERT_RADIUS_KM)]


def sms_all_users():
    return [(u["phone"], u["name"]) for u in users.values() if u["phone"]]


def push_targets_for_sensor(sensor):
    """Push subscriptions inside the sensor radius, or with unknown location, plus every admin device."""
    out = []
    for sub in list(push.subs):
        u = users.get(sub["user_id"]) if sub["user_id"] else None
        if (u and u["role"] == "admin") or sub["lat"] is None or \
                haversine_km(sensor["lat"], sensor["lng"], sub["lat"], sub["lng"]) <= ALERT_RADIUS_KM:
            out.append(sub)
    return out


def push_targets_for_users(ids):
    ids = set(ids)
    return [sub for sub in list(push.subs) if sub["user_id"] in ids]


def emit_warning(sids, severity, message, message_hi=None, sensor=None):
    for sid in sids:
        socketio.emit("flood_warning", {"severity": severity, "message": message, "message_hi": message_hi,
                                        "timestamp": now()}, to=sid)
    if sensor:       # also reaches phones where the website is closed
        rising = severity == "rising"
        push.notify(push_targets_for_sensor(sensor), {
            "kind": "warning", "tag": "warning", "url": "/", "urgent": False,
            "title": "🌊 Water rising fast" if rising else "⚠️ Flood warning",
            "title_hi": "🌊 पानी तेज़ी से बढ़ रहा है" if rising else "⚠️ बाढ़ की चेतावनी",
            "body": message, "body_hi": message_hi})


def broadcast_emergency(message, source, sids=None, message_hi=None, sensor=None):
    """THE siren. sids=None -> every connected client (admin MASTER SIREN).
    sids=[...]        -> only clients near the sensor that crossed the danger level.
    Web Push goes out too, so phones with the website CLOSED are woken up."""
    payload = {"message": message, "message_hi": message_hi, "source": source, "lead_time": "30-60 minutes", "timestamp": now()}
    last_siren.update(payload=payload, time=time.time())
    if sids is None:
        socketio.emit("flood_emergency_alert", payload)
    else:
        for sid in sids:
            socketio.emit("flood_emergency_alert", payload, to=sid)
    push.notify(push_targets_for_sensor(sensor) if sensor else list(push.subs), {
        "kind": "siren", "tag": "siren", "url": "/#siren", "urgent": True,
        "title": "🚨 FLOOD EMERGENCY", "title_hi": "🚨 बाढ़ आपातकाल", "body": message, "body_hi": message_hi})


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    """The service worker must be served from the site root so it can control the whole site."""
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/manifest.json")
def web_manifest():
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")


@app.route("/api/last-siren")
def get_last_siren():
    """Opened from a notification: show the siren screen for an alert from the last 30 minutes."""
    recent = last_siren["payload"] and time.time() - last_siren["time"] < 1800
    return jsonify(payload=last_siren["payload"] if recent else None)


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    d = request.get_json(silent=True) or {}
    u = current_user()
    try:
        lat, lng = float(d["lat"]), float(d["lng"])
        assert -90 <= lat <= 90 and -180 <= lng <= 180
    except (KeyError, TypeError, ValueError, AssertionError):
        lat = lng = None
    done, err = push.add(d.get("subscription"), u["id"] if u else None, lat, lng, "hi" if d.get("lang") == "hi" else "en")
    return (jsonify(ok=True), 200) if done else (jsonify(error=err), 400)


@app.route("/api/push/location", methods=["POST"])
def push_location():
    d = request.get_json(silent=True) or {}
    try:
        push.set_location(d["endpoint"], float(d["lat"]), float(d["lng"]))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="endpoint, lat and lng required"), 400
    return jsonify(ok=True)


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    push.remove((request.get_json(silent=True) or {}).get("endpoint", ""))
    return jsonify(ok=True)


@app.route("/api/admin/push/test", methods=["POST"])
@api_roles("admin")
def push_test():
    """Send a test notification to the admin's own subscribed devices."""
    me = current_user()
    n = push.notify(push_targets_for_users([me["id"]]), {"kind": "info", "tag": "test", "url": "/admin", "urgent": False,
                                                          "title": "RescuNet test", "body": "Background alerts are working."})
    audit(me["username"], "push_test", f"{n} device(s)")
    return jsonify(ok=True, devices=n, configured=push.configured())


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/admin")
def admin_page():
    u = current_user()
    if not u:
        return redirect(url_for("login_page", next="/admin"))
    if u["role"] not in ("admin", "camp_manager"):
        abort(403)
    return render_template("admin.html", admin_name=u["name"], role=u["role"])


@app.route("/admin/tools")
def admin_tools_page():
    u = current_user()
    if not u:
        return redirect(url_for("login_page", next="/admin/tools"))
    if u["role"] != "admin":
        abort(403)
    return render_template("admin_tools.html", admin_name=u["name"])


@app.errorhandler(403)
def forbidden(_):
    return ("<h1 style='font-family:sans-serif'>403 – Access denied</h1><p style='font-family:sans-serif'>"
            "This area is restricted to authorized disaster-management officials. "
            "<a href='/'>Back to RescuNet</a></p>"), 403


def _err(status, msg):
    """JSON for /api/* calls, short HTML page for browser pages."""
    if request.path.startswith("/api/"):
        return jsonify(error=msg), status
    return f"<h1 style='font-family:sans-serif'>{status} - {msg}</h1><p style='font-family:sans-serif'><a href='/'>Back to RescuNet</a></p>", status


@app.errorhandler(404)
def not_found(_):
    return _err(404, "Not found")


@app.errorhandler(405)
def bad_method(_):
    return _err(405, "Method not allowed")


@app.errorhandler(500)
def server_error(e):
    log.exception("Unhandled server error: %s", e)
    return _err(500, "Something went wrong on the server")


@socketio.on_error_default
def socket_error(e):
    log.exception("Socket error: %s", e)


# --------------------------------------------------------------------------
# Auth API
# --------------------------------------------------------------------------
@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.get_json(silent=True) or {}
    name, username = (d.get("name") or "").strip(), (d.get("username") or "").strip().lower()
    password, role = d.get("password") or "", d.get("role", "citizen")
    if role not in SELF_REGISTER_ROLES:
        return jsonify(error="Invalid role. Admin accounts are created by the authority only."), 403
    if not name or len(username) < 3 or len(password) < 6:
        return jsonify(error="Name, username (3+ chars) and password (6+ chars) are required"), 400
    if any(u["username"] == username for u in users.values()):
        return jsonify(error="Username already taken"), 409
    phone = (d.get("phone") or "").strip()[:20]
    if phone and len(re.sub(r"\D", "", phone)) < 10:
        return jsonify(error="Enter a valid 10-digit phone number (needed for SMS alerts)"), 400
    if phone and any(u["phone"] and sms.normalize_phone(u["phone"]) == sms.normalize_phone(phone) for u in users.values()):
        return jsonify(error="This phone number is already registered"), 409
    uid = next(_ids["user"])
    users[uid] = {"id": uid, "name": name[:60], "username": username, "password_hash": generate_password_hash(password),
                  "phone": phone, "role": role, "skill": d.get("skill", "general"),
                  "lat": None, "lng": None, "last_sos": 0}
    session.clear()
    session["uid"] = uid
    return jsonify(ok=True, user=public_user(users[uid])), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.get_json(silent=True) or {}
    username, password = (d.get("username") or "").strip().lower(), d.get("password") or ""
    fails, locked_until = login_failures.get(username, (0, 0))
    if time.time() < locked_until:
        return jsonify(error="Too many attempts. Try again in a minute."), 429
    u = next((x for x in users.values() if x["username"] == username), None)
    if not u or not check_password_hash(u["password_hash"], password):
        fails += 1
        login_failures[username] = (fails, time.time() + 60 if fails >= 5 else 0)
        return jsonify(error="Wrong username or password"), 401
    login_failures.pop(username, None)
    session.clear()
    session["uid"] = u["id"]
    if u["role"] in ("admin", "camp_manager"):
        audit(u["username"], f"{u['role']}_login")
    return jsonify(ok=True, user=public_user(u))


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    if endpoint:
        push.detach(endpoint)          # this device stops receiving the logged-out user's personal pushes
    session.clear()
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Public (read-only) data
# --------------------------------------------------------------------------
def alert_view(a, viewer=None):
    """Copy of an SOS record for one viewer (internal fields removed, contact data limited)."""
    v = {k: x for k, x in a.items() if not k.startswith("_")}
    if viewer and viewer["role"] == "volunteer":
        if viewer["lat"] is not None:
            v["distance_km"] = round(haversine_km(a["latitude"], a["longitude"], viewer["lat"], viewer["lng"]), 1)
        if a["volunteer_id"] != viewer["id"]:
            v["phone"] = ""        # phone is shown only to the volunteer who accepted the SOS
    return v


def active_alerts_for(u):
    live = [a for a in distress_alerts if a["status"] != "resolved"]
    if u and u["role"] == "admin":
        return [alert_view(a) for a in live]
    if u and u["role"] == "volunteer":
        return [alert_view(a, u) for a in live if u["id"] in a["_notified"] or a["volunteer_id"] == u["id"]]
    return []


SMS_STATUS_TEXT = {"accepted": "has accepted your SOS and will help you",
                   "on_the_way": "is on the way to you", "reached": "has reached your location"}


def publish_sos_update(a):
    """Tell the victim, the admins and the alerted volunteers that an SOS changed status (app + SMS)."""
    payload = {"id": a["id"], "status": a["status"], "volunteer_id": a["volunteer_id"], "volunteer_name": a["volunteer_name"]}
    for room in {f"user:{a['user_id']}", "admins", *[f"user:{vid}" for vid in a["_notified"]]}:
        socketio.emit("sos_update", payload, to=room)
    if a["phone"] and a["volunteer_name"] and a["status"] in SMS_STATUS_TEXT:
        sms.send_sms(a["phone"], f"RescuNet: {a['volunteer_name']} {SMS_STATUS_TEXT[a['status']]}.", "sos_status", a["name"])
    if a["user_id"] is not None and a["volunteer_name"] and a["status"] in SMS_STATUS_TEXT:
        push.notify(push_targets_for_users([a["user_id"]]), {
            "kind": "status", "tag": f"sos-{a['id']}", "url": "/", "urgent": False,
            "title": "🙋 Help is coming", "title_hi": "🙋 मदद आ रही है",
            "body": f"{a['volunteer_name']} {SMS_STATUS_TEXT[a['status']]}."})


def find_alert(alert_id):
    return next((x for x in distress_alerts if x["id"] == alert_id), None)


@app.route("/api/state")
def get_state():
    u = current_user()
    mine = [a for a in distress_alerts if u and a["user_id"] == u["id"] and a["status"] != "resolved"]
    return jsonify({
        "me": public_user(u),
        "sensors": list(iot_water_sensors.values()),
        "camps": [camp_view(c) for c in relief_camps.values()],
        "zones": list(flood_zones.values()),
        "teams": list(rescue_teams.values()),
        "reports": community_reports[-10:][::-1],
        "notifications": notifications,
        "alerts": active_alerts_for(u)[-30:][::-1],          # victims' details: volunteers/admins only
        "my_sos": alert_view(mine[-1]) if mine else None,    # the user's own active SOS (status tracker)
        "volunteers": sum(1 for x in users.values() if x["role"] == "volunteer"),
        "sms_number": SMS_GATEWAY_NUMBER,
        "vapid_public_key": push.PUBLIC if push.configured() else "",
        **({"volunteers_list": [{"id": v["id"], "name": v["name"], "skill": v["skill"], "phone": v["phone"],
                                 "lat": v["lat"], "lng": v["lng"]} for v in users.values() if v["role"] == "volunteer"],
            "sms_recipients": len(sms_all_users())} if u and u["role"] == "admin" else {}),
    })


# --------------------------------------------------------------------------
# Victim / volunteer actions
# --------------------------------------------------------------------------
def notify_volunteers(alert, vids):
    """Popup (app) + SMS to the given volunteers. Volunteers can answer the SMS with ACCEPT <id>."""
    for vid in vids:
        socketio.emit("new_distress_alert", alert_view(alert, users[vid]), to=f"user:{vid}")
    link = f"https://maps.google.com/?q={alert['latitude']:.5f},{alert['longitude']:.5f}"
    text = (f"RescuNet SOS #{alert['id']} ({alert['urgency_level'].replace('_', ' ')}) from {alert['name']}. "
            f"Location: {link} Reply ACCEPT {alert['id']} to help.")
    sms.send_many([(users[v]["phone"], users[v]["name"]) for v in vids], text, "sos")
    push.notify(push_targets_for_users(vids), {
        "kind": "sos", "tag": f"sos-{alert['id']}", "url": "/", "urgent": True,
        "title": "🆘 SOS nearby", "title_hi": "🆘 पास में SOS",
        "body": f"{alert['urgency_level'].replace('_', ' ')} - {alert['name']}. Open RescuNet to accept."})


def create_sos(owner, name, phone, lat, lng, urgency, ack=True, accuracy=None):
    """Save an SOS and alert nearby volunteers + admins (app popup AND SMS). Never sounds the siren.
    owner = the user dict, or None for an unregistered person who sent the SOS by SMS."""
    notified = set()
    for v in users.values():
        if v["role"] != "volunteer" or (owner and v["id"] == owner["id"]):
            continue
        # volunteers who share their location are filtered by distance; unknown location = alerted too
        if v["lat"] is None or haversine_km(lat, lng, v["lat"], v["lng"]) <= NEARBY_KM:
            notified.add(v["id"])
    alert = {"id": next(_ids["alert"]), "user_id": owner["id"] if owner else None, "name": name, "phone": phone,
             "latitude": lat, "longitude": lng, "accuracy_m": accuracy, "urgency_level": urgency,
             "priority": PRIORITY.get(urgency, "low"), "status": "open",
             "volunteer_id": None, "volunteer_name": None, "created_at": now(),
             "volunteers_notified": len(notified), "_notified": notified}
    distress_alerts.append(alert)
    notify_volunteers(alert, notified)
    socketio.emit("new_distress_alert", alert_view(alert), to="admins")
    admins = [(x["phone"], x["name"]) for x in users.values() if x["role"] == "admin"]
    push.notify(push_targets_for_users([x["id"] for x in users.values() if x["role"] == "admin"]), {
        "kind": "sos", "tag": f"sos-{alert['id']}", "url": "/admin", "urgent": True,
        "title": "🆘 New SOS", "body": f"{urgency.replace('_', ' ')} - {name}"})
    sms.send_many(admins, f"RescuNet SOS #{alert['id']} ({urgency.replace('_', ' ')}) from {name}: "
                          f"https://maps.google.com/?q={lat:.5f},{lng:.5f}", "sos")
    if ack and phone:
        sms.send_sms(phone, f"RescuNet: your SOS was received. {len(notified)} volunteer(s) nearby alerted. Stay safe.", "sos_ack", name)
    return alert


def apply_response(u, a, action):
    """Volunteer workflow, shared by the web button and SMS replies. Returns (error_message|None, http_status)."""
    if a["status"] == "resolved":
        return "This SOS is already closed", 409
    if action == "accept":
        if u["id"] not in a["_notified"]:
            return "This SOS was not sent to you", 403
        if a["volunteer_id"] not in (None, u["id"]):
            return f"Already taken by {a['volunteer_name']}", 409
        a.update(volunteer_id=u["id"], volunteer_name=u["name"], status="accepted")
    elif action in ("on_the_way", "reached"):
        if a["volunteer_id"] != u["id"]:
            return "Accept this SOS first", 403
        a["status"] = action
    else:
        return "action must be accept, on_the_way or reached", 400
    audit(u["username"], f"sos_{action}", f"SOS #{a['id']}")
    publish_sos_update(a)
    return None, 200


@app.route("/api/alerts/trigger", methods=["POST"])
@api_roles("citizen", "volunteer")
def trigger_alert():
    """Private SOS from the app button."""
    u = current_user()
    active = next((a for a in distress_alerts if a["user_id"] == u["id"] and a["status"] != "resolved"), None)
    if active:
        return jsonify(error="You already have an active SOS. Mark yourself safe before sending another.",
                       alert=alert_view(active)), 409
    if time.time() - u["last_sos"] < SOS_COOLDOWN_S:
        return jsonify(error=f"Please wait {SOS_COOLDOWN_S} seconds between SOS signals"), 429
    d = request.get_json(silent=True) or {}
    try:
        lat, lng = float(d["latitude"]), float(d["longitude"])
        assert -90 <= lat <= 90 and -180 <= lng <= 180
    except (KeyError, TypeError, ValueError, AssertionError):
        return jsonify(error="Valid latitude and longitude are required"), 400
    urgency = d.get("urgency_level", "general")
    if urgency not in URGENCY_LEVELS:
        return jsonify(error=f"urgency_level must be one of {sorted(URGENCY_LEVELS)}"), 400
    u["last_sos"] = time.time()
    try:
        accuracy = round(float(d["accuracy"])) if d.get("accuracy") is not None else None
        accuracy = accuracy if accuracy is not None and 0 <= accuracy <= 100000 else None
    except (TypeError, ValueError):
        accuracy = None
    alert = create_sos(u, u["name"], u["phone"], lat, lng, urgency, accuracy=accuracy)
    return jsonify(ok=True, alert=alert_view(alert), volunteers_notified=alert["volunteers_notified"]), 201


@app.route("/api/alerts/<int:alert_id>/respond", methods=["POST"])
@api_roles("volunteer")
def respond_alert(alert_id):
    """Volunteer workflow: accept -> on_the_way -> reached. The victim sees every step live (app + SMS)."""
    u, a = current_user(), find_alert(alert_id)
    if not a:
        return jsonify(error="SOS not found"), 404
    err, code = apply_response(u, a, (request.get_json(silent=True) or {}).get("action"))
    if err:
        return jsonify(error=err), code
    return jsonify(ok=True, alert=alert_view(a, u))


@app.route("/api/alerts/<int:alert_id>/safe", methods=["POST"])
@api_roles("citizen", "volunteer")
def mark_safe(alert_id):
    """The person who sent the SOS closes it ('I am safe')."""
    u, a = current_user(), find_alert(alert_id)
    if not a or a["user_id"] != u["id"]:
        return jsonify(error="SOS not found"), 404
    a["status"] = "resolved"
    publish_sos_update(a)
    return jsonify(ok=True)


@app.route("/api/volunteers/location", methods=["POST"])
@api_roles("volunteer")
def volunteer_location():
    d = request.get_json(silent=True) or {}
    try:
        u = current_user()
        u["lat"], u["lng"] = float(d["latitude"]), float(d["longitude"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="latitude/longitude required"), 400
    return jsonify(ok=True)


@app.route("/api/reports", methods=["POST"])
@api_roles("citizen", "volunteer", "admin")
def community_report():
    d = request.get_json(silent=True) or {}
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify(error="Describe the issue"), 400
    rep = {"id": next(_ids["report"]), "kind": d.get("kind", "other"), "text": text[:300],
           "latitude": d.get("latitude"), "longitude": d.get("longitude"), "created_at": now()}
    community_reports.append(rep)
    push_notification("info", "Community Report", rep["text"])
    return jsonify(ok=True, report=rep), 201


# --------------------------------------------------------------------------
# IoT sensors: admin session OR hardware key
# --------------------------------------------------------------------------
@app.route("/api/sensors/update", methods=["POST"])
def update_sensor():
    u = current_user()
    key_ok = hmac.compare_digest(request.headers.get("X-IoT-Key", ""), IOT_API_KEY)
    if not (key_ok or (u and u["role"] == "admin")):
        return (jsonify(error="Login required"), 401) if not u else (jsonify(error="Forbidden: admins or IoT devices only"), 403)
    d = request.get_json(silent=True) or {}
    try:
        sensor = iot_water_sensors[int(d["sensor_id"])]
        level = float(d["current_level"])
        assert 0 <= level <= 50
    except (KeyError, TypeError, ValueError, AssertionError):
        return jsonify(error="valid sensor_id and current_level (0-50 m) required"), 400

    meta = sensor_meta[sensor["id"]]
    prev_level, prev_state = sensor["current_level"], sensor["state"]
    sensor["current_level"], sensor["updated_at"] = round(level, 2), now()
    sensor["state"] = sensor_state(sensor)
    sensor["trend"] = round(level - prev_level, 2)
    rising = sensor["trend"] >= RISE_STEP_M and level >= WATCH_LEVEL and sensor["state"] != "critical"
    audit(u["username"] if u and not key_ok else "iot-device", "sensor_update", f"{sensor['name']} = {level:.1f} m")
    socketio.emit("sensor_update", sensor)
    refresh_zones()

    targets, t, emergency = alert_targets(sensor), time.time(), False
    if sensor["state"] == "critical":
        # siren when the river first crosses danger level, then at most once per SIREN_REPEAT_S
        if prev_state != "critical" or t - meta["last_siren"] >= SIREN_REPEAT_S:
            emergency, meta["last_siren"] = True, t
            push_notification("danger", "Flood Alert", f"High water level detected: {sensor['name']} at {level:.1f} m.")
            broadcast_emergency(f"FLOOD EMERGENCY: {sensor['name']} reached {level:.1f} m (danger level "
                                f"{sensor['danger_level']:.1f} m). Move to higher ground and the nearest relief camp NOW.",
                                sensor["name"], sids=targets, sensor=sensor,
                                message_hi=f"बाढ़ आपातकाल: {sensor['name']} का जल स्तर {level:.1f} मीटर पहुँच गया है (खतरे का स्तर "
                                           f"{sensor['danger_level']:.1f} मीटर)। तुरंत ऊँचे स्थान या नज़दीकी राहत शिविर की ओर जाएँ।")
            audit("system", "AUTO_SIREN", f"{sensor['name']} at {level:.1f} m -> {len(targets)} clients")
            sms.send_many(sms_recipients_for_sensor(sensor),
                          f"RescuNet FLOOD ALERT: {sensor['name']} at {level:.1f} m (danger {sensor['danger_level']:.1f} m). "
                          f"Move to higher ground / nearest relief camp NOW.", "siren")
    elif (sensor["state"] == "warning" and prev_state == "normal") or (rising and t - meta["last_warn"] >= WARN_REPEAT_S):
        meta["last_warn"] = t
        if rising:
            msg = (f"Water is rising fast near {sensor['name']}: {level:.1f} m (+{sensor['trend']:.1f} m since the last "
                   f"reading). Be ready to move to higher ground.")
            severity = "rising"
            msg_hi = (f"{sensor['name']} के पास पानी तेज़ी से बढ़ रहा है: {level:.1f} मीटर (+{sensor['trend']:.1f} मीटर)। "
                      f"ऊँचे स्थान पर जाने के लिए तैयार रहें।")
        else:
            msg, severity = f"{sensor['name']} crossed the warning level ({level:.1f} m). Stay alert.", "warning"
            msg_hi = f"{sensor['name']} का जल स्तर चेतावनी स्तर ({level:.1f} मीटर) पार कर गया है। सतर्क रहें।"
        push_notification("warning", "Flood warning", msg)
        emit_warning(targets, severity, msg, msg_hi, sensor=sensor)
        sms.send_many(sms_recipients_for_sensor(sensor), f"RescuNet warning: {msg}", "warning")
    return jsonify(ok=True, sensor=sensor, emergency=emergency, clients_in_range=len(targets))


# --------------------------------------------------------------------------
# Admin-only
# --------------------------------------------------------------------------
@app.route("/api/admin/siren", methods=["POST"])
@api_roles("admin")
def master_siren():
    """MASTER SIREN TRIGGER - the only manual way to sound the global siren."""
    u = current_user()
    d = request.get_json(silent=True) or {}
    custom = (d.get("message") or "").strip()[:300]
    msg = custom or ("OFFICIAL FLOOD WARNING: Severe flooding expected within 30-60 minutes. "
                     "Evacuate to the nearest relief camp immediately.")
    msg_hi = None if custom else "सरकारी बाढ़ चेतावनी: अगले 30-60 मिनट में भीषण बाढ़ की आशंका है। तुरंत नज़दीकी राहत शिविर की ओर निकलें।"
    audit(u["username"], "MASTER_SIREN", msg)
    push_notification("danger", "Official evacuation order", msg)
    broadcast_emergency(msg, f"Authorized official ({u['name']})", message_hi=msg_hi)
    sms.send_many(sms_all_users(), f"RescuNet OFFICIAL ALERT: {msg}", "siren")
    return jsonify(ok=True)


@app.route("/api/admin/camps/<int:camp_id>", methods=["POST"])
@api_roles("admin", "camp_manager")
def update_camp(camp_id):
    camp = relief_camps.get(camp_id)
    if not camp:
        return jsonify(error="Camp not found"), 404
    d = request.get_json(silent=True) or {}
    try:
        occupied, food, med = int(d["occupied"]), int(d["food_stock"]), int(d["medical_stock"])
        assert 0 <= occupied <= camp["capacity"] and 0 <= food <= 100 and 0 <= med <= 100
    except (KeyError, TypeError, ValueError, AssertionError):
        return jsonify(error="occupied must be 0..capacity; food/medical stock 0..100"), 400
    camp.update(occupied=occupied, food_stock=food, medical_stock=med)
    audit(current_user()["username"], "camp_update", f"{camp['name']}: {occupied}/{camp['capacity']}, food {food}%, medical {med}%")
    socketio.emit("camps_update", [camp_view(c) for c in relief_camps.values()])
    return jsonify(ok=True, camp=camp_view(camp))


@app.route("/api/admin/alerts/<int:alert_id>/resolve", methods=["POST"])
@api_roles("admin")
def resolve_alert(alert_id):
    a = find_alert(alert_id)
    if not a:
        return jsonify(error="Alert not found"), 404
    a["status"] = "resolved"
    audit(current_user()["username"], "sos_resolved", f"SOS #{alert_id}")
    publish_sos_update(a)
    return jsonify(ok=True)


@app.route("/api/admin/alerts/<int:alert_id>/dispatch", methods=["POST"])
@api_roles("admin")
def dispatch_alert(alert_id):
    """Control room manually sends an SOS to one volunteer, or to ALL volunteers (ignores distance)."""
    a = find_alert(alert_id)
    if not a:
        return jsonify(error="Alert not found"), 404
    if a["status"] == "resolved":
        return jsonify(error="This SOS is already closed"), 409
    d = request.get_json(silent=True) or {}
    if d.get("all"):
        chosen = [v for v in users.values() if v["role"] == "volunteer"]
    else:
        try:
            v = users[int(d["volunteer_id"])]
            assert v["role"] == "volunteer"
        except (KeyError, TypeError, ValueError, AssertionError):
            return jsonify(error="Choose a volunteer"), 400
        chosen = [v]
    new = [v["id"] for v in chosen if v["id"] not in a["_notified"] and v["id"] != a["user_id"]]
    a["_notified"].update(new)
    a["volunteers_notified"] = len(a["_notified"])
    notify_volunteers(a, new)
    audit(current_user()["username"], "sos_dispatch", f"SOS #{alert_id} -> {len(new)} volunteer(s)")
    socketio.emit("sos_update", {"id": a["id"], "status": a["status"], "volunteer_id": a["volunteer_id"],
                                 "volunteer_name": a["volunteer_name"], "volunteers_notified": a["volunteers_notified"]}, to="admins")
    return jsonify(ok=True, dispatched=len(new))


@app.route("/api/admin/users", methods=["POST"])
@api_roles("admin")
def create_staff():
    """Only a control-room officer can create camp-manager accounts (they can edit camps, nothing else)."""
    d = request.get_json(silent=True) or {}
    name, username, password = (d.get("name") or "").strip(), (d.get("username") or "").strip().lower(), d.get("password") or ""
    if not name or len(username) < 3 or len(password) < 8:
        return jsonify(error="Name, username (3+) and password (8+ characters) are required"), 400
    if any(u["username"] == username for u in users.values()):
        return jsonify(error="Username already taken"), 409
    uid = next(_ids["user"])
    users[uid] = {"id": uid, "name": name[:60], "username": username, "password_hash": generate_password_hash(password),
                  "phone": "", "role": "camp_manager", "skill": "", "lat": None, "lng": None, "last_sos": 0}
    audit(current_user()["username"], "staff_created", f"camp_manager {username}")
    return jsonify(ok=True, user=public_user(users[uid])), 201


@app.route("/api/admin/sms/test", methods=["POST"])
@api_roles("admin")
def sms_test():
    """Send one real test SMS - the quickest way to check the provider settings."""
    phone = ((request.get_json(silent=True) or {}).get("phone") or "").strip()
    if len(re.sub(r"\D", "", phone)) < 10:
        return jsonify(error="Enter a valid 10-digit phone number"), 400
    rec = sms.send_sms(phone, "RescuNet test message: SMS alerts are working.", "test")
    audit(current_user()["username"], "sms_test", rec["to"])
    return jsonify(ok=True, status=rec["status"])


@app.route("/api/admin/audit")
@api_roles("admin")
def get_audit():
    return jsonify(audit_log[:50])


# --------------------------------------------------------------------------
# Sockets: server -> client only. Clients cannot emit anything that does something.
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# SMS gateway: messages from phones WITHOUT internet
# --------------------------------------------------------------------------
sms_sos_last = {}       # phone -> time of last SMS-SOS (anti-spam)


def handle_incoming_sms(sender, text):
    """Process one SMS and return the reply text.
       Anyone:      SOS [medical|food] <lat>,<lng>
       Volunteers:  ACCEPT <id> / ONWAY <id> / REACHED <id>"""
    phone = sms.normalize_phone(sender)
    text = (text or "").strip()[:300]
    user = next((x for x in users.values() if x["phone"] and sms.normalize_phone(x["phone"]) == phone), None)
    sms.log_inbound(phone, text, user["name"] if user else "")

    cmd = sms.parse_command(text)
    if cmd:
        action, aid = cmd
        if not user or user["role"] != "volunteer":
            return "RescuNet: only registered volunteers can reply with ACCEPT."
        a = find_alert(aid)
        if not a:
            return f"RescuNet: SOS #{aid} not found."
        err, _ = apply_response(user, a, action)
        if err:
            return f"RescuNet: {err}."
        return {"accept": f"You accepted SOS #{aid}. Reply ONWAY {aid} when you leave.",
                "on_the_way": f"Thanks. Reply REACHED {aid} when you arrive.",
                "reached": f"Thank you for helping. SOS #{aid} marked as reached."}[action]

    sos = sms.parse_sos(text)
    if sos:
        urgency, lat, lng = sos
        if lat is None and user and user["lat"] is not None:
            lat, lng = user["lat"], user["lng"]                    # last location shared in the app
        if lat is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return "RescuNet: send your location too, e.g. SOS medical 25.61,85.16 (decimal degrees)."
        if any(a["phone"] and sms.normalize_phone(a["phone"]) == phone and a["status"] != "resolved" for a in distress_alerts):
            return "RescuNet: you already have an active SOS. Help is being arranged."
        if time.time() - sms_sos_last.get(phone, 0) < 60:
            return "RescuNet: please wait a minute before sending another SOS."
        sms_sos_last[phone] = time.time()
        alert = create_sos(user, user["name"] if user else phone, user["phone"] if user else phone, lat, lng, urgency, ack=False)
        return f"RescuNet: SOS #{alert['id']} received. {alert['volunteers_notified']} volunteer(s) nearby alerted. Stay safe."
    return "RescuNet: to ask for help send: SOS medical|food <lat>,<lng>"


@app.route("/api/sms/incoming", methods=["POST"])
def sms_incoming():
    """Webhook for the SMS provider (Twilio: set 'A message comes in' to  https://YOUR-HOST/api/sms/incoming?key=SECRET)."""
    if not hmac.compare_digest(request.args.get("key", ""), SMS_WEBHOOK_SECRET):
        return jsonify(error="Forbidden"), 403
    data = request.form if request.form else (request.get_json(silent=True) or {})
    sender = data.get("From") or data.get("from") or ""
    if not sender:
        return jsonify(error="sender missing"), 400
    reply = handle_incoming_sms(sender, data.get("Body") or data.get("body") or data.get("text") or "")
    if request.form:         # Twilio style: the reply SMS is sent by the TwiML answer
        from xml.sax.saxutils import escape
        return Response(f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{escape(reply)}</Message></Response>",
                        mimetype="text/xml")
    sms.send_sms(sender, reply, "reply")
    return jsonify(reply=reply)


@app.route("/api/admin/sms")
@api_roles("admin")
def sms_outbox():
    return jsonify(mode=sms.MODE, provider=sms.status(), push=push.status(), messages=sms.outbox[:60])


@app.route("/api/admin/sms/simulate", methods=["POST"])
@api_roles("admin")
def sms_simulate():
    """Test the SMS flow without a provider: pretend a phone sent this text."""
    d = request.get_json(silent=True) or {}
    if not (d.get("from") or "").strip():
        return jsonify(error="Enter the sender phone number"), 400
    reply = handle_incoming_sms(d["from"], d.get("body", ""))
    sms.send_sms(d["from"], reply, "reply")
    return jsonify(ok=True, reply=reply)


@socketio.on("connect")
def on_connect():
    connected.add(request.sid)
    u = current_user()
    if u:
        join_room(f"user:{u['id']}")
        if u["role"] == "admin":
            join_room("admins")


@socketio.on("disconnect")
def on_disconnect(*_):
    connected.discard(request.sid)
    client_locations.pop(request.sid, None)


@socketio.on("set_location")
def on_set_location(data):
    """Browser shares its position (only after the user allowed it). Used ONLY to target alerts."""
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
        assert -90 <= lat <= 90 and -180 <= lng <= 180
    except (KeyError, TypeError, ValueError, AssertionError):
        return
    client_locations[request.sid] = (lat, lng)
    u = current_user()
    if u:
        u["lat"], u["lng"] = lat, lng           # volunteers: SOS routing; everyone: SMS alert targeting
    s = min(iot_water_sensors.values(), key=lambda x: haversine_km(lat, lng, x["lat"], x["lng"]))
    dist = haversine_km(lat, lng, s["lat"], s["lng"])
    socketio.emit("nearest_sensor", {"id": s["id"], "name": s["name"], "distance_km": round(dist, 1),
                                     "level": s["current_level"], "state": s["state"],
                                     "in_range": dist <= ALERT_RADIUS_KM}, to=request.sid)


if __name__ == "__main__":
    print("RescuNet running at http://127.0.0.1:5000")
    print(f"Admin login -> username: {ADMIN_USER}   password: {ADMIN_PASSWORD}   (set ADMIN_PASSWORD env var to change)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, allow_unsafe_werkzeug=True)
