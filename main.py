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
        return jsonify(error='name and unique event_id required'), 400
    rarity = clean(data.get('rarity'), 20).title()
    if kind == 'spawn' and rarity not in ('Secret', 'Eternal', 'Divine'):
        return jsonify(error='invalid rarity'), 400
    with LOCK:
        clean_seen()
        if 'ingest:' + event_id in SEEN:
            return jsonify(status='duplicate', sent=False)
        if kind == 'spawn':
            message = spawn_message({
                'rarity': rarity, 'name': name,
                'biome': data.get('location'),
                'facts': {'income': data.get('income'), 'speed': data.get('speed')},
            }).replace('📡 Source: eggPing (unofficial report)', '📡 Source: authorized ingest')
        else:
            message = (f'🌊 YOUKO HUB — GAME EVENT\n\n📣 Event: {name}\n'
                       f'🌍 Location: {clean(data.get("location") or "Not provided")}\n'
                       f'🕒 Cambodia: {local_time()}')
        try:
            send_telegram(message)
        except (requests.RequestException, RuntimeError):
            app.logger.exception('Telegram delivery failed')
            return jsonify(error='telegram_delivery_failed'), 502
        SEEN['ingest:' + event_id] = time.time() + 86400
    return jsonify(status='sent', sent=True)


import html
import asyncio
import discord

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL = os.getenv("DISCORD_CHANNEL_ID", "").strip()

RARITY_ICONS = {
    "SECRET": "⚫",
    "ETERNAL": "🟣",
    "DIVINE": "🟡",
}

def send_discord_alert(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if not response.ok:
        raise RuntimeError("Telegram delivery failed")

def discord_worker():
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        app.logger.info("YOUKO HUB Discord connected")

    @client.event
    async def on_message(message):
        if str(message.channel.id) != DISCORD_CHANNEL:
            return

        if not (message.webhook_id or message.author.bot):
            return

        for index, embed in enumerate(message.embeds):
            title = embed.title or ""
            description = embed.description or ""

            rarity = next(
                (
                    r for r in RARITY_ICONS
                    if re.search(
                        rf"\b{r}\s+EGG\s+SPAWNED\b",
                        title + "\n" + description,
                        re.I,
                    )
                ),
                None,
            )

            if not rarity:
                continue

            fields = {
                re.sub(r"[^a-z ]", "", f.name.lower()).strip(): f.value
                for f in embed.fields
            }


            def get_field(*names):
                for name in names:
                    if fields.get(name):
                        return fields[name]

                text = re.sub(r"<a?:\w+:\d+>", "", description)
                text = text.replace("**", "").replace("__", "")

                for name in names:
                    match = re.search(
                        rf"\b{re.escape(name)}\s*:\s*([^\r\n]+)",
                        text,
                        re.IGNORECASE,
                    )
                    if match:
                        return match.group(1).strip()

                return "N/A"



            egg = get_field("egg", "egg name")
            location = get_field("location", "biome")
            money = get_field("money", "income")
            speed = get_field("recommended speed", "speed")

            if egg == "N/A":
                continue

            join = get_field("join game", "join")
            match = re.search(
                r"https://[^\s)>]+",
                join + "\n" + description,
            )

            game_link = "N/A"
            if match:
                url = html.escape(match.group(0), quote=True)
                game_link = f'<a href="{url}">Click Here</a>'

            esc = html.escape

            alert = (
                "<b>YOUKO HUB • Auto Notification</b>\n\n"
                f"<b>{RARITY_ICONS[rarity]} "
                f"{rarity} EGG SPAWNED!!</b>\n\n"
                f"🥚 ពង៖ {esc(egg)}\n"
                f"👽 ទីតាំង៖ {esc(location)}\n"
                f"💸 Money: {esc(money)}\n"
                f"🏃 ល្បឿនណែនាំ៖ {esc(speed)}\n"
                f"🎮 ចូលហ្គេម៖ {game_link}\n"
                "📡 Source: SenZ V2\n\n"
                "<b>YOUKO HUB | Steal An Egg Notifier</b>"
            )

            key = f"discord:{message.id}:{index}"

            def deliver():
                with LOCK:
                    clean_seen()
                    if key in SEEN:
                        return
                    send_discord_alert(alert)
                    SEEN[key] = time.time() + 86400

            try:
                await asyncio.to_thread(deliver)
            except Exception:
                app.logger.error("Discord alert delivery failed")

    try:
        client.run(DISCORD_TOKEN)
    except Exception:
        app.logger.error("Discord connection failed")

if DISCORD_TOKEN and DISCORD_CHANNEL:
    threading.Thread(
        target=discord_worker,
        name="youko-discord",
        daemon=True,
    ).start()


if MONITOR_ENABLED:
    threading.Thread(target=monitor_loop, name='eggping-poller', daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
