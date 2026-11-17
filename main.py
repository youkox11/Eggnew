"""YOUKO HUB: Telegram ingest and optional eggPing feed monitor.

The eggPing poller is opt-in. Enable only when automated access is permitted.
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
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
INGEST_KEY = os.getenv('INGEST_API_KEY', '').strip()
MONITOR_ENABLED = os.getenv('EGGPING_MONITOR_ENABLED', 'false').lower() == 'true'
MONITOR_URL = 'https://eggping.com/api/live'
POLL_SECONDS = max(60, int(os.getenv('EGGPING_POLL_SECONDS', '90')))
TZ = ZoneInfo('Asia/Phnom_Penh')
SEEN = {}
LOCK = threading.Lock()
STATUS = {'running': False, 'last_check': None, 'last_error': None, 'baseline': False}


def clean(value, limit=120):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or '')).strip()[:limit]


def local_time(at_ms=None):
    when = datetime.fromtimestamp(at_ms / 1000, TZ) if at_ms else datetime.now(TZ)
    return when.strftime('%Y-%m-%d %H:%M:%S')


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError('Missing Telegram environment variables')
    response = requests.post(
        f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
        json={'chat_id': CHAT_ID, 'text': message, 'disable_web_page_preview': True},
        timeout=15,
    )
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('Telegram rejected the message')


def spawn_message(item):
    rarity = clean(item.get('rarity'), 20).title()
    symbol = {'Secret': '⚫', 'Eternal': '🟣', 'Divine': '🟡'}.get(rarity, '🥚')
    facts = item.get('facts') if isinstance(item.get('facts'), dict) else {}
    at = item.get('at') if isinstance(item.get('at'), (int, float)) else None
    return (f'🥚 YOUKO HUB — EGG SPAWN ALERT\n\n'
            f'{symbol} {rarity.upper()} SPAWNED!\n'
            f'🥚 Egg: {clean(item.get("name"))}\n'
            f'🌍 Biome: {clean(item.get("biome") or facts.get("place") or "Not provided")}\n'
            f'💰 Income: {clean(facts.get("income") or "Not provided")}\n'
            f'👟 Recommended Speed: {clean(facts.get("speed") or "Not provided")}\n'
            f'🕒 Cambodia: {local_time(at)}\n'
            f'📡 Source: eggPing (unofficial report)')


def clean_seen():
    now = time.time()
    for key, expiry in list(SEEN.items()):
        if expiry < now:
            del SEEN[key]


def monitor_loop():
    STATUS['running'] = True
    while True:
        try:
            result = requests.get(MONITOR_URL, timeout=15, headers={'User-Agent': 'YoukoEggNotifier/1.0'})
            result.raise_for_status()
            payload = result.json()
            entries = payload.get('spawns', []) if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                raise ValueError('spawns is not a list')
            now_ms = int(time.time() * 1000)
            with LOCK:
                clean_seen()
                if not STATUS['baseline']:
                    for obj in entries:
                        if isinstance(obj, dict) and obj.get('id'):
                            SEEN['eggping:' + str(obj['id'])] = time.time() + 86400
                    STATUS['baseline'] = True
                else:
                    # Old entries should never be sent as new alerts.
                    fresh = [e for e in entries if isinstance(e, dict) and
                             e.get('rarity', '').lower() in ('secret', 'eternal', 'divine') and
                             isinstance(e.get('at'), (int, float)) and
                             0 <= now_ms - e['at'] <= 180000 and e.get('id')]
                    for item in sorted(fresh, key=lambda e: e['at']):
                        key = 'eggping:' + str(item['id'])
                        if key not in SEEN:
                            send_telegram(spawn_message(item))
                            SEEN[key] = time.time() + 86400
            STATUS['last_check'] = local_time()
            STATUS['last_error'] = None
        except Exception as exc:
            STATUS['last_error'] = clean(exc, 180)
            app.logger.exception('eggPing monitor error')
        time.sleep(POLL_SECONDS)


@app.get('/health')
def health():
    return jsonify(status='ok', live_feed='eggping_enabled' if MONITOR_ENABLED else 'not_connected', monitor=STATUS)


@app.post('/ingest')
def ingest():
    supplied = request.headers.get('X-Ingest-Key', '')
    if not INGEST_KEY or not hmac.compare_digest(supplied, INGEST_KEY):
        return jsonify(error='unauthorized'), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='JSON object required'), 400
    kind = clean(data.get('type', 'spawn'), 20).lower()
    if kind not in ('spawn', 'event'):
        return jsonify(error='type must be spawn or event'), 400
    name = clean(data.get('name'))
    event_id = clean(data.get('event_id'), 150)
    if not name or not event_id:
