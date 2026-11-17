
if not result.get('ok'):
        raise RuntimeError('Telegram rejected the message')


@app.get('/health')
def health():
    return jsonify(status='ok', live_feed='not_connected')


@app.post('/ingest')
def ingest():
    supplied_key = request.headers.get('X-Ingest-Key', '')
    if not INGEST_KEY or not hmac.compare_digest(supplied_key, INGEST_KEY):
        return jsonify(error='unauthorized'), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='JSON object required'), 400

    kind = clean(data.get('type', 'spawn'), 20).lower()
    if kind not in ('spawn', 'event'):
        return jsonify(error='type must be spawn or event'), 400

    name = clean(data.get('name'))
    if not name:
        return jsonify(error='name is required'), 400

    rarity = clean(data.get('rarity'), 20).title()
    if kind == 'spawn' and rarity not in ('Secret', 'Eternal', 'Divine'):
        return jsonify(error='rarity must be Secret, Eternal or Divine'), 400

    event_id = clean(data.get('event_id'), 150)
    if not event_id:
        return jsonify(error='unique event_id is required'), 400

    now = time.time()
    for key, expiry in list(SEEN.items()):
        if expiry < now:
            del SEEN[key]
    if event_id in SEEN:
        return jsonify(status='duplicate', sent=False)

    local_time = datetime.now(ZoneInfo('Asia/Phnom_Penh')).strftime('%Y-%m-%d %H:%M:%S')
    location = clean(data.get('location')) or 'Not provided'
    income = clean(data.get('income')) or 'Not provided'
    speed = clean(data.get('speed')) or 'Not provided'
    if kind == 'spawn':
        emoji = {'Secret': '⚫', 'Eternal': '🟣', 'Divine': '🟡'}[rarity]
        message = (f'🥚 YOUKO HUB — EGG SPAWN ALERT\n\n'
                   f'{emoji} {rarity.upper()} SPAWNED!\n'
                   f'🥚 Egg: {name}\n'
                   f'🌍 Location: {location}\n'
                   f'💰 Income: {income}\n'
                   f'👟 Recommended Speed: {speed}\n'
                   f'🕒 Cambodia: {local_time}')
    else:
        message = (f'🌊 YOUKO HUB — GAME EVENT\n\n'
                   f'📣 Event: {name}\n'
                   f'🌍 Location: {location}\n'
                   f'🕒 Cambodia: {local_time}')
    try:
        send_telegram(message)
    except (requests.RequestException, RuntimeError):
        app.logger.exception('Telegram delivery failed')
        return jsonify(error='telegram_delivery_failed'), 502

    SEEN[event_id] = now + 86400
    return jsonify(status='sent', sent=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
