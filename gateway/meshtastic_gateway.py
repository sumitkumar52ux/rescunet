"""
Bridge between a REAL Meshtastic (LoRa) node and RescuNet.      *** UNTESTED EXAMPLE - needs hardware ***

Hardware: one Meshtastic-compatible LoRa board connected by USB to a computer with internet (this is the "gateway").
People carry other Meshtastic nodes (paired by Bluetooth with the Meshtastic phone app).

  pip install meshtastic requests pypubsub
  set RESCUNET_URL=https://your-app.up.railway.app
  set MESH_GATEWAY_KEY=<same value as the MESH_GATEWAY_KEY variable on the server>
  python meshtastic_gateway.py

What it does
  mesh -> server : a text such as   SOS medical 25.61,85.16   (or just  SOS  if the node shares its GPS position)
                   is sent to /api/mesh/ingest, which creates a normal RescuNet SOS (volunteer popup + SMS + push).
  server -> mesh : flood alerts waiting in /api/mesh/outbox are broadcast into the radio mesh.
"""
import os
import time

import meshtastic.serial_interface
import requests
from pubsub import pub

SERVER = os.environ.get("RESCUNET_URL", "http://127.0.0.1:5000").rstrip("/")
HEADERS = {"X-Mesh-Key": os.environ.get("MESH_GATEWAY_KEY", "mesh-demo-key")}
iface = meshtastic.serial_interface.SerialInterface()


def on_text(packet, interface):
    text = (packet.get("decoded") or {}).get("text", "")
    if not text.strip().upper().startswith("SOS"):
        return
    sender = packet.get("fromId") or str(packet.get("from"))
    node = (interface.nodes or {}).get(sender, {})
    pos = node.get("position") or {}
    body = {"from": (node.get("user") or {}).get("longName", sender), "text": text,
            "lat": pos.get("latitude"), "lng": pos.get("longitude"),
            "hops": (packet.get("hopStart") or 0) - (packet.get("hopLimit") or 0)}
    try:
        reply = requests.post(f"{SERVER}/api/mesh/ingest", json=body, headers=HEADERS, timeout=10).json().get("reply")
        if reply:
            interface.sendText(reply, destinationId=sender)       # confirmation back to the sender
    except Exception as e:
        print("could not reach RescuNet:", e)


pub.subscribe(on_text, "meshtastic.receive.text")

last_id = 0
while True:                                                       # server -> mesh broadcast
    try:
        items = requests.get(f"{SERVER}/api/mesh/outbox", params={"since": last_id}, headers=HEADERS, timeout=10).json()
        for item in items.get("items", []):
            iface.sendText(item["text"])
            last_id = max(last_id, item["id"])
    except Exception as e:
        print("outbox poll failed:", e)
    time.sleep(10)
