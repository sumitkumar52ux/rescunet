# RescuNet - Flood Relief & Micro-Rescue Network (RBAC prototype)

## Run
    pip install -r requirements.txt
    python app.py
Open http://127.0.0.1:5000  (internet needed for Tailwind CDN + map tiles)

## Roles
| Role      | Can do |
|-----------|--------|
| Visitor   | View sensors, camps, map, alerts (read-only) |
| Citizen   | + private SOS (DB + nearby volunteers + control room). NEVER the siren |
| Volunteer | + receives nearby SOS alerts |
| Admin     | /admin: MASTER SIREN TRIGGER, sensors, camps, SOS queue, audit log |

Admin login (change it!):  username `admin`  password `Admin@123`
    set ADMIN_PASSWORD=YourStrongPassword   (Windows: set ADMIN_PASSWORD=...  before python app.py)
Also optional env vars: SECRET_KEY, ADMIN_USER, IOT_API_KEY.

## Test
1. Register a citizen in one browser, log in as admin in another (or a private window).
2. Admin: click "Arm siren", then "MASTER SIREN TRIGGER" -> every open tab gets the alarm.
3. Citizen: try SOS -> only volunteers/admin see it, no siren. Visiting /admin gives 403.
4. Admin panel sensor: set 15.5 m -> automatic emergency siren.
5. Real IoT device: POST /api/sensors/update with header X-IoT-Key: iot-demo-key.

## Local alerts + volunteer response (v3)
- Click "📍 Enable local alerts" (browser asks permission). Your position is used ONLY to target alerts.
- Each river sensor has a location and a 25 km radius (ALERT_RADIUS_KM in app.py).
  - Level >= 12 m and rising >= 0.5 m between readings -> "water rising fast" message (nearby users only)
  - Level >= 14 m -> warning message;  level >= 15 m -> siren (nearby users + users whose location is unknown)
  - While above 15 m the siren repeats at most every 2 minutes. Admin MASTER SIREN still reaches everyone.
- Volunteers who share location only receive SOS from within 10 km (NEARBY_KM).
- SOS flow: victim sends SOS -> volunteers + admin alerted -> volunteer taps Accept -> "I'm on the way" -> "I've reached"
  -> victim sees every step live and can tap "I'm safe" to close it. Volunteer's phone/victim phone is shown only after Accept.

## Quick demo (3 windows)
1. Admin window: /admin.  2. Normal window: register a volunteer, click Enable local alerts.
3. Incognito window: register a citizen, send SOS.  Then in Admin set Ganges sensor to 13, then 13.8, then 15.5.

## SMS alerts - works on phones with NO internet (v4)
Default is DEMO mode: no SMS is really sent, every message appears in Admin panel -> "SMS gateway" (Outbox).
- Register with a 10-digit phone number (needed for SMS). Admin number: set ADMIN_PHONE=98xxxxxxxx before python app.py.
- Outgoing SMS: flood siren / warning (users near the sensor + admin), MASTER SIREN (everyone),
  new SOS (nearby volunteers + admin), "volunteer accepted / on the way / reached" (to the person who sent the SOS).
- Incoming SMS (to your gateway number):
    anyone:     SOS medical 25.61,85.16      (also: SOS food ... / SOS ...)
    volunteer:  ACCEPT 12   ONWAY 12   REACHED 12
- Test without a provider: Admin panel -> "Simulate an incoming SMS".

### Going live (needs a provider account + paid SMS)
    set SMS_MODE=twilio    set TWILIO_SID=...  set TWILIO_TOKEN=...  set TWILIO_FROM=+1...
    or
    set SMS_MODE=fast2sms  set FAST2SMS_KEY=...        (India bulk SMS also needs DLT registration)
Point the provider's "incoming message" webhook to:  https://YOUR-PUBLIC-HOST/api/sms/incoming?key=SMS_WEBHOOK_SECRET
(set SMS_WEBHOOK_SECRET to your own secret). The server needs internet + a public address for this; the phones do not.

## v5 - review improvements
- Map: OpenStreetMap tiles (no API key). Dark mode darkens them with CSS.
- Header cleaned up; alert sound unlocks silently on the first tap (no "Arm siren" button). EN / हिं language toggle.
- Sensors: trend arrow, last-updated time, colour bar (green <12 m, yellow 12, orange 14, red 15).
- Camps: map pin + Directions link, FULL / High occupancy / Low food / Low medicine warnings.
- Helplines page (verify numbers for your state) + offline banner that shows the SMS-SOS text and number (set SMS_GATEWAY_NUMBER).
- SOS: GPS accuracy shown, priority (high/medium/low), status tracker hidden until an SOS is active.
- Control room: typed "SIREN" confirmation, live status bar, SOS dispatch (one volunteer or all), map preview,
  SMS provider status, audit log. Test tools moved to /admin/tools.
- Roles: admin (control room officer) and camp_manager (camps only). Officers create camp managers in /admin/tools.

## Hosting on Railway (WebSockets supported)
- Start command (already in `Procfile`):  gunicorn -w 1 --threads 100 -b 0.0.0.0:$PORT app:app
  Keep **one** worker (-w 1): all data lives in this process's memory.
- Variables to set in Railway -> Variables (change every default!):
    ADMIN_PASSWORD, SECRET_KEY (long random text), IOT_API_KEY, SMS_WEBHOOK_SECRET, COOKIE_SECURE=1
    optional: ADMIN_PHONE, SMS_GATEWAY_NUMBER, SMS_MODE (+ provider keys), ALLOWED_ORIGINS
- Data is still in memory: every deploy / restart clears accounts and alerts (a database is the next step).
- SMS webhook:  https://YOUR-APP.up.railway.app/api/sms/incoming?key=SMS_WEBHOOK_SECRET
- IoT sensors:  POST https://YOUR-APP.up.railway.app/api/sensors/update   header  X-IoT-Key: <IOT_API_KEY>

## Alerts when the website is CLOSED (Web Push, v6)
1. pip install -r requirements.txt            (adds pywebpush)
2. python make_vapid_keys.py                  -> copy the 3 lines it prints into your environment variables
   (Railway: Variables tab.  Windows:  set VAPID_PUBLIC_KEY=...  set VAPID_PRIVATE_KEY=...  set VAPID_SUBJECT=mailto:you@example.com)
   Never change the keys later - every device would have to subscribe again.
3. Open the site (HTTPS, or localhost) -> blue banner "Turn on" (or Settings -> Background alerts) -> Allow notifications.
4. Close the website. Admin presses MASTER SIREN (or a sensor passes 15 m) -> a notification with vibration appears;
   tapping it opens RescuNet and the full-screen siren starts. Test button: Admin -> Test tools -> "Send test notification".
Honest limits: the notification uses the phone's normal sound (it cannot loop a siren) and is muted by Do-Not-Disturb;
iPhone needs "Add to Home Screen" first; the device needs internet (SMS covers phones without it); subscriptions are kept in
memory, so they reset when the server restarts (users are asked again on their next visit).

## AI forecast + mesh network (v7)
### AI forecast (forecast.py)
- A least-squares trend is fitted to each sensor's latest readings (<= 12, last 6 h). It gives the rise rate (m/h), the
  ETA to the 14 m / 15 m levels (with a range), the level in 1 h / 3 h, and a confidence (readings + fit quality).
  Shown on the public sensor cards and, with a sparkline, in the control room.
- AI early warning: if danger level is forecast within 60 minutes (confidence medium/high) everyone near that sensor gets a
  "Flood forecast" message (app + push + SMS), at most once per 10 minutes. It NEVER sounds the siren by itself.
- Honest: this is classic statistics, not deep learning. Rivers can speed up or slow down.
- FORECAST_DEMO=1 (default) treats readings closer than 10 minutes as 10 minutes apart so demos work. Set FORECAST_DEMO=0 in real use.
- Flood simulator: Admin -> Test tools -> "Flood simulator" (forecast -> early warning -> siren). SIM_TICK_S sets the speed.
### Mesh network (mesh.py) - public site -> "Mesh network"
- Simulation of a LoRa-style multi-hop network: SOS hops node to node to a gateway with internet, waits when no path exists
  (store-and-forward), re-routes when a relay/gateway fails; siren alerts spread from the gateways to every node.
  Control room can switch nodes / gateway internet off to show self-healing. Delivered SOS = normal SOS marked "via mesh".
- Real hardware: gateway/meshtastic_gateway.py (UNTESTED example, needs Meshtastic LoRa boards) posts radio texts such as
  "SOS medical 25.61,85.16" to /api/mesh/ingest and broadcasts /api/mesh/outbox alerts into the radio mesh.
  Set MESH_GATEWAY_KEY (same value on server and gateway). A browser cannot be a mesh node.

## Live demo for the control room (v8)
Admin panel -> "Live demo": drag the water-level slider (or press presets / "Auto-rise 10 -> 16 m"). Each release sends a real
sensor reading; the panel explains what happened (watch -> rising fast -> warning at 14 m -> AUTOMATIC SIREN at 15 m) and how many
screens / push devices / SMS numbers it reached. "Reset rivers to normal" lets you repeat the demo.
- Demo tip: in the public-site window do NOT press "Enable local alerts" (a device whose location is far from the sensor, e.g. >25 km,
  correctly gets no automatic siren). Tap once on the page so the browser allows sound.
- Drill mode (button on the same card, 30 min, switches itself off): the siren/warnings show on connected screens marked [DEMO],
  but NO SMS / push is sent. It never applies to real sensor hardware (X-IoT-Key readings).
- The siren repeats at most every 2 minutes for a river that stays above 15 m; going below 15 m and rising again triggers a fresh one.
