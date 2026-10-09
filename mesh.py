"""
RescuNet mesh network - SIMULATION of a LoRa-style multi-hop network, plus the bridge for a real gateway.

Idea: when mobile networks and internet are down, small radios (e.g. Meshtastic/LoRa nodes) pass a message from one
to the next until it reaches a GATEWAY that still has internet. This module simulates that: nodes have positions and a
radio range, an SOS hops node-to-node along the shortest path, waits (store-and-forward) when no path exists, and
re-routes when a relay or gateway fails. Flood alerts travel the other way: from internet gateways out to every node.

This is a teaching/demo model of the routing idea. It does NOT use real radios - a browser cannot be a mesh node.
Real hardware connects through  gateway/meshtastic_gateway.py  and the /api/mesh/ingest endpoint (see README).
"""
import math
import os
import threading
import time
from datetime import datetime

RANGE_KM = 2.6                 # radio range of every node in the demo
MAX_HOPS = 12
TICK_S = float(os.environ.get("MESH_TICK_S", "1.5"))
NO_TICKER = os.environ.get("MESH_NO_TICKER") == "1"      # tests step the network by hand


def _n(nid, name, kind, lat, lng):
    return {"id": nid, "name": name, "kind": kind, "lat": lat, "lng": lng, "online": True, "internet": kind == "gateway"}


NODES = {n["id"]: n for n in [
    _n("G1", "Gandhi Maidan Camp (gateway)", "gateway", 25.6180, 85.1415),
    _n("G2", "Kosi School Shelter (gateway)", "gateway", 25.5850, 85.2420),
    _n("R1", "Relay 1", "relay", 25.6200, 85.1620),
    _n("R2", "Relay 2", "relay", 25.6150, 85.1830),
    _n("R2b", "Relay 2b", "relay", 25.6190, 85.1830),
    _n("R3", "Relay 3", "relay", 25.6080, 85.2040),
    _n("R4", "Relay 4", "relay", 25.6000, 85.2240),
    _n("U1", "Village A (boat team)", "user", 25.6130, 85.2300),
    _n("U2", "Village B (Kosi basin)", "user", 25.5900, 85.2300),
    _n("U3", "Village C (Kankarbagh)", "user", 25.5980, 85.1600),
]}
messages = []          # newest first
outbox = []            # texts a real gateway should broadcast into the radio mesh
_lock = threading.RLock()
_seq = [0]
_cfg = {"on_deliver": None, "on_update": None}
_ticker = {"running": False}


def configure(on_deliver=None, on_update=None):
    _cfg["on_deliver"], _cfg["on_update"] = on_deliver, on_update


def _now():
    return datetime.now().isoformat(timespec="seconds")


def dist_km(a, b):
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b["lng"] - a["lng"]) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def neighbors(nid):
    return sorted(m for m, n in NODES.items() if m != nid and n["online"] and dist_km(NODES[nid], n) <= RANGE_KM)


def is_exit(nid):
    n = NODES[nid]
    return n["kind"] == "gateway" and n["online"] and n["internet"]


def route(start):
    """Shortest path (list of node ids) from `start` to the nearest gateway that has internet, or None."""
    if not NODES[start]["online"]:
        return None
    seen, queue = {start: None}, [start]
    while queue:
        cur = queue.pop(0)
        if is_exit(cur):
            path = []
            while cur is not None:
                path.append(cur)
                cur = seen[cur]
            return path[::-1]
        for nb in neighbors(cur):
            if nb not in seen:
                seen[nb] = cur
                queue.append(nb)
    return None


def send_sos(node_id, name, urgency):
    """A person at a village node sends an SOS into the mesh. Returns (message, error)."""
    with _lock:
        n = NODES.get(node_id)
        if not n or n["kind"] != "user":
            return None, "Choose one of the village nodes"
        if not n["online"]:
            return None, "That node is switched off"
        _seq[0] += 1
        m = {"id": _seq[0], "kind": "sos", "node": node_id, "at": node_id, "path": [node_id], "hops": 0,
             "status": "moving", "urgency": urgency, "name": name, "lat": n["lat"], "lng": n["lng"],
             "created": _now(), "alert_id": None}
        messages.insert(0, m)
        del messages[40:]
    _ensure_ticker()
    _update()
    return m, None


def flood_alert(text):
    """Server -> mesh: the alert spreads from every internet gateway outwards, one hop per tick."""
    with _lock:
        gws = [g for g in NODES if is_exit(g)]
        outbox.append({"id": (outbox[-1]["id"] + 1) if outbox else 1, "text": text[:180], "time": _now()})
        del outbox[:-30]
        if not gws:
            return None
        _seq[0] += 1
        m = {"id": _seq[0], "kind": "alert", "text": text[:120], "status": "moving", "reached": {g: 0 for g in gws},
             "frontier": list(gws), "wave": 0, "created": _now()}
        messages.insert(0, m)
        del messages[40:]
    _ensure_ticker()
    _update()
    return m


def set_node(node_id, online=None, internet=None):
    with _lock:
        n = NODES.get(node_id)
        if not n:
            return False
        if online is not None:
            n["online"] = bool(online)
        if internet is not None and n["kind"] == "gateway":
            n["internet"] = bool(internet)
    _ensure_ticker()
    _update()
    return True


def reset():
    with _lock:
        messages.clear()
        for n in NODES.values():
            n["online"], n["internet"] = True, n["kind"] == "gateway"
    _update()


def step():
    """Advance every message by one hop. Returns True if anything changed."""
    changed = False
    with _lock:
        for m in list(messages):
            if m["status"] in ("delivered", "dropped"):
                continue
            if m["kind"] == "sos":
                if is_exit(m["at"]):
                    _deliver(m); changed = True
                    continue
                path = route(m["at"])
                if path is None:                     # store-and-forward: keep the message and try again next tick
                    if m["status"] != "waiting":
                        m["status"], changed = "waiting", True
                    continue
                if m["hops"] >= MAX_HOPS:
                    m["status"], changed = "dropped", True
                    continue
                m["status"], m["at"] = "moving", path[1]
                m["path"].append(path[1]); m["hops"] += 1; changed = True
                if is_exit(m["at"]):
                    _deliver(m)
            else:                                    # alert wave
                new = []
                for f in m["frontier"]:
                    for nb in neighbors(f):
                        if nb not in m["reached"]:
                            m["reached"][nb] = m["wave"] + 1
                            new.append(nb)
                if new:
                    m["wave"] += 1; m["frontier"] = new
                else:
                    m["status"] = "delivered"
                changed = True
    return changed


def _deliver(m):
    m["status"], m["delivered_via"] = "delivered", m["at"]
    cb = _cfg["on_deliver"]
    if cb:
        try:
            cb(m)
        except Exception:                            # a problem in the app must not stop the network
            pass


def _pub(m):
    out = {k: v for k, v in m.items() if k != "frontier"}
    if m["kind"] == "sos":
        out["path_names"] = [NODES[p]["name"] for p in m["path"]]
    return out


def state():
    with _lock:
        links = [[a, b] for a in sorted(NODES) if NODES[a]["online"] for b in neighbors(a) if a < b]
        return {"range_km": RANGE_KM, "nodes": [dict(n) for n in NODES.values()], "links": links,
                "messages": [_pub(m) for m in messages[:10]], "updated": _now()}


def _update():
    cb = _cfg["on_update"]
    if cb:
        try:
            cb()
        except Exception:
            pass


def _ensure_ticker():
    if NO_TICKER:
        return
    with _lock:
        if _ticker["running"]:
            return
        _ticker["running"] = True
    threading.Thread(target=_loop, daemon=True).start()


def _loop():
    idle = 0
    while True:
        time.sleep(TICK_S)
        if step():
            _update()
        with _lock:
            busy = any(m["status"] in ("moving", "waiting") for m in messages)
            idle = 0 if busy else idle + 1
            if idle >= 20:
                _ticker["running"] = False
                return
