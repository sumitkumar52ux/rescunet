"""
RescuNet AI forecast - a small, transparent trend model for river levels.

What it really is: a least-squares line fitted to the latest readings of a sensor (up to 12 readings from the last
6 hours). From the slope it estimates how fast the river is rising, when it will reach the warning (14 m) and danger
(15 m) levels, and how sure it is (number of readings + how well a straight line fits). The ETA is shown as a range
using the standard error of the slope. It is classic statistics, NOT a deep-learning model, and rivers can speed up or
slow down, so a forecast is a warning aid for the control room - it never sounds the siren by itself.

Timing: real sensors report every few minutes. For demos where readings are typed in seconds apart,
FORECAST_DEMO=1 (default) treats readings closer than 10 minutes as 10 minutes apart. Set FORECAST_DEMO=0 on a real deployment.
"""
import math
import os
import time

DEMO = os.environ.get("FORECAST_DEMO", "1") == "1"
DEMO_STEP_S = 600
MAX_POINTS = 12
WINDOW_S = 6 * 3600
MIN_POINTS = 3
RISING_M_PER_H = 0.15        # slower than this counts as "steady"

history = {}                 # sensor id -> [(timestamp, level), ...]


def reset(sid):
    history[sid] = []


def record(sid, level, ts=None):
    ts = time.time() if ts is None else ts
    h = history.setdefault(sid, [])
    if h and ts < h[-1][0]:          # clock went backwards (e.g. after a simulation): start a new series
        h.clear()
    h.append((ts, float(level)))
    del h[:-60]


def _effective_times(points):
    t = [0.0]
    for i in range(1, len(points)):
        dt = max(points[i][0] - points[i - 1][0], 0.0)
        t.append(t[-1] + (max(dt, DEMO_STEP_S) if DEMO else dt))
    return t


def _eta_min(cur, thr, rate_per_s):
    if cur >= thr:
        return 0.0
    if rate_per_s <= 1e-7:
        return None
    return (thr - cur) / rate_per_s / 60.0


def predict(sid, warning=14.0, danger=15.0):
    h = history.get(sid, [])
    pts = [p for p in h if h and h[-1][0] - p[0] <= WINDOW_S][-MAX_POINTS:]
    n = len(pts)
    out = {"points": n, "demo_timing": DEMO, "status": "insufficient_data", "confidence": "none",
           "rate_m_per_h": None, "eta_warning_min": None, "eta_danger_min": None,
           "eta_danger_range": None, "level_in_1h": None, "level_in_3h": None,
           "history": [round(p[1], 2) for p in pts], "method": "least-squares trend on the latest readings"}
    if n < MIN_POINTS:
        out["note"] = f"Need at least {MIN_POINTS} readings (have {n})"
        return out
    t, y = _effective_times(pts), [p[1] for p in pts]
    if t[-1] < 300:
        out["note"] = "Readings are too close together in time"
        return out
    tm, ym = sum(t) / n, sum(y) / n
    sxx = sum((x - tm) ** 2 for x in t)
    slope = sum((x - tm) * (v - ym) for x, v in zip(t, y)) / sxx            # metres per second
    icpt = ym - slope * tm
    sse = sum((v - (icpt + slope * x)) ** 2 for x, v in zip(t, y))
    sst = sum((v - ym) ** 2 for v in y)
    r2 = 1.0 if sst == 0 else max(0.0, 1 - sse / sst)
    se = math.sqrt(sse / (n - 2)) / math.sqrt(sxx) if n > 2 else 0.0         # standard error of the slope
    cur, rate_h = y[-1], slope * 3600
    confidence = "high" if n >= 6 and r2 >= 0.85 else "medium" if n >= 4 and r2 >= 0.6 else "low"
    status = ("above_danger" if cur >= danger else "rising" if rate_h >= RISING_M_PER_H
              else "falling" if rate_h <= -RISING_M_PER_H else "steady")
    eta_d = _eta_min(cur, danger, slope)
    fast, slow = _eta_min(cur, danger, slope + se), _eta_min(cur, danger, slope - se)
    out.update(status=status, confidence=confidence, r2=round(r2, 2), rate_m_per_h=round(rate_h, 2),
               eta_warning_min=None if _eta_min(cur, warning, slope) is None else round(_eta_min(cur, warning, slope)),
               eta_danger_min=None if eta_d is None else round(eta_d),
               eta_danger_range=[None if fast is None else round(fast), None if slow is None else round(slow)] if eta_d else None,
               level_in_1h=round(cur + rate_h, 2), level_in_3h=round(cur + 3 * rate_h, 2))
    out["note"] = {"rising": "River is rising", "falling": "River is falling", "steady": "River is steady",
                   "above_danger": "Already above danger level"}[status]
    return out
