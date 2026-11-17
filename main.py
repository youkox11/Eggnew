"""YOUKO HUB Egg Spawn Notifier: authenticated event ingest -> Telegram.
No live spawn source is bundled. Configure a permitted event producer separately.
"""
import hmac
import os
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
INGEST_KEY = os.environ.get("INGEST_API_KEY", "").strip()
SEEN = {}
SEEN_LOCK = threading.Lock()
LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")


def clean(value, limit=120):
    if value is None:
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()[:limit]


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram environment variables missing")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message},
        timeout=15,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected the message")


@app.get("/health")
def health():
    return jsonify(status="ok", live_feed="not_connected")


@app.post("/ingest")
def ingest():
    supplied = request.headers.get("X-Ingest-Key", "")
    if not INGEST_KEY or not hmac.compare_digest(supplied, INGEST_KEY):
        return jsonify(error="unauthorized"), 401
    if request.content_length is not None and request.content_length > 8192:
        return jsonify(error="payload_too_large"), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON object required"), 400

    kind = clean(data.get("type", "spawn"), 20).lower()
    if kind not in ("spawn", "event"):
        return jsonify(error="type must be spawn or event"), 400
    name = clean(data.get("name"))
    event_id = clean(data.get("event_id"), 150)
    if not name or not event_id:
        return jsonify(error="name and unique event_id are required"), 400
    rarity = clean(data.get("rarity"), 20).title()
    if kind == "spawn" and rarity not in ("Secret", "Eternal", "Divine"):
        return jsonify(error="rarity must be Secret, Eternal or Divine"), 400

    now = time.time()
    with SEEN_LOCK:
        for key, expiry in list(SEEN.items()):
            if expiry <= now:
                del SEEN[key]
        if event_id in SEEN:
            return jsonify(status="duplicate", sent=False), 200
        # Temporary reservation prevents simultaneous requests sending duplicates.
        SEEN[event_id] = now + 60

    local_time = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    location = clean(data.get("location")) or "Not provided"
    if kind == "spawn":
        emoji = {"Secret": "⚫", "Eternal": "🟣", "Divine": "🟡"}[rarity]
        income = clean(data.get("income")) or "Not provided"
        speed = clean(data.get("speed")) or "Not provided"
        message = (f"🥚 YOUKO HUB — EGG SPAWN ALERT\n\n"
                   f"{emoji} {rarity.upper()} SPAWNED!\n"
                   f"🥚 Egg: {name}\n🌍 Location: {location}\n"
                   f"💰 Income: {income}\n👟 Recommended Speed: {speed}\n"
                   f"🕒 Cambodia: {local_time}")
    else:
        message = (f"🌊 YOUKO HUB — GAME EVENT\n\n"
                   f"📣 Event: {name}\n🌍 Location: {location}\n"
                   f"🕒 Cambodia: {local_time}")

    try:
        send_telegram(message)
    except (requests.RequestException, RuntimeError, ValueError):
        with SEEN_LOCK:
            SEEN.pop(event_id, None)
        app.logger.exception("Telegram delivery failed")
        return jsonify(error="telegram_delivery_failed"), 502

    with SEEN_LOCK:
        SEEN[event_id] = time.time() + 86400
    return jsonify(status="sent", sent=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
