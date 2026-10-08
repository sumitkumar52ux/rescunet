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
