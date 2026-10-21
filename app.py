# Сервер подписок SugarClick: ключи с тарифами, пробный час, подписанные ответы (Ed25519) и Telegram-бот для выдачи ключей.
#
# Переменные окружения (на Render: Environment):
#   SIGN_KEY        секретный ключ подписи (base64, 32 байта), создаётся командой: python app.py genkey
#   DATABASE_URL    строка подключения к PostgreSQL (Neon / Supabase). Без неё - локальный licenses.db (только для тестов!)
#   TG_TOKEN        токен бота от @BotFather
#   ADMIN_IDS       числовые Telegram ID админов через запятую (узнать: команда /id в боте)
#   ADMIN_USERNAMES ники админов через запятую, без @ (по умолчанию danilsugar)
#   RENDER_EXTERNAL_URL  Render задаёт сам; по нему бот ставит webhook
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request

import ed25519

DB_FILE = 'licenses.db'
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
TG_TOKEN = os.environ.get('TG_TOKEN', '').strip()
ADMIN_IDS = {int(x) for x in os.environ.get('ADMIN_IDS', '').replace(' ', '').split(',') if x.isdigit()}
ADMIN_USERNAMES = {x.strip().lstrip('@').lower() for x in os.environ.get('ADMIN_USERNAMES', 'danilsugar').split(',') if x.strip()}
TG_SECRET = hashlib.sha256(('sugarclick-webhook:' + TG_TOKEN).encode()).hexdigest()[:48]
CLIENT_VERSION = 2  # клиенты старше этой версии не могут активировать ключи (они не проверяют срок)

# тариф: (часы, название для пользователя)
PLANS = {
    'hour': (1, 'Пробный час'),
    'day': (24, '1 день'),
    '3days': (72, '3 дня'),
    'week': (168, 'Неделя'),
    'month': (720, 'Месяц'),
}
PLAN_ALIASES = {
    '1h': 'hour', 'час': 'hour', 'trial': 'hour',
    '1d': 'day', '1': 'day', 'день': 'day',
    '3d': '3days', '3': '3days', '3дня': '3days',
    '7d': 'week', '7': 'week', 'неделя': 'week',
    '30d': 'month', '30': 'month', 'месяц': 'month',
}
KEY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # без 0/O и 1/I, чтобы не путали при вводе


def _sign_key():
    raw = os.environ.get('SIGN_KEY', '').strip()
    if raw:
        return base64.b64decode(raw)
    print('ВНИМАНИЕ: SIGN_KEY не задан, используется временный ключ (только для тестов)')
    return hashlib.sha256(b'sugarclick-local-test-key').digest()


SIGN_SECRET = _sign_key()
SIGN_PUBLIC = ed25519.public_key(SIGN_SECRET)


# ===== База данных: PostgreSQL на Render, SQLite локально =====
def db():
    if DATABASE_URL:
        import psycopg
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect(DB_FILE)


def q(conn, sql, args=()):
    if DATABASE_URL:
        sql = sql.replace('?', '%s')
    cur = conn.cursor()
    cur.execute(sql, args)
    return cur


def init_db():
    with db() as conn:
        q(conn, '''CREATE TABLE IF NOT EXISTS sub_keys (
            key TEXT PRIMARY KEY,
            plan TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            hwid TEXT,
            activated_at BIGINT,
            banned INTEGER DEFAULT 0
        )''')
        q(conn, '''CREATE TABLE IF NOT EXISTS devices (
            hwid TEXT PRIMARY KEY,
            expires_at BIGINT DEFAULT 0,
            plan TEXT,
            trial_at BIGINT,
            banned INTEGER DEFAULT 0,
            last_seen BIGINT
        )''')


def get_device(conn, hwid):
    row = q(conn, 'SELECT expires_at, plan, trial_at, banned FROM devices WHERE hwid = ?', (hwid,)).fetchone()
    if not row:
        return None
    return {'expires_at': row[0] or 0, 'plan': row[1], 'trial_at': row[2], 'banned': row[3] or 0}


def extend_device(conn, hwid, hours, plan_name, now, trial=False):
    """Добавляет время к подписке: продление идёт от текущего конца, если подписка ещё активна."""
    dev = get_device(conn, hwid)
    if dev is None:
        q(conn, 'INSERT INTO devices (hwid, expires_at, plan, last_seen) VALUES (?, 0, NULL, ?)', (hwid, now))
        dev = {'expires_at': 0, 'plan': None, 'trial_at': None, 'banned': 0}
    active = dev['expires_at'] > now
    expires = max(now, dev['expires_at']) + hours * 3600
    # при продлении активной подписки в названии остаётся самый длинный тариф (пробный час не перебивает купленный)
    hours_of = {name: h for h, name in PLANS.values()}
    plan = plan_name
    if active and dev['plan'] and hours_of.get(dev['plan'], 0) > hours:
        plan = dev['plan']
    if trial:
        q(conn, 'UPDATE devices SET expires_at = ?, plan = ?, trial_at = ?, last_seen = ? WHERE hwid = ?', (expires, plan, now, now, hwid))
    else:
        q(conn, 'UPDATE devices SET expires_at = ?, plan = ?, last_seen = ? WHERE hwid = ?', (expires, plan, now, hwid))
    return expires, plan


def new_key():
    return 'SC-' + '-'.join(''.join(secrets.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(3))


def normalize_key(k):
    return k.strip().upper().replace(' ', '')


# ===== Подписанная лицензия =====
def license_payload(hwid, now, dev):
    exp = 0 if (dev is None or dev['banned']) else dev['expires_at']
    data = {'v': 1, 'hwid': hwid, 'exp': exp, 'plan': (dev or {}).get('plan') or '', 'now': now,
            'trial_used': bool(dev and dev['trial_at'])}
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'license': base64.b64encode(raw).decode(), 'sig': base64.b64encode(ed25519.sign(SIGN_SECRET, raw)).decode(),
            'exp': exp, 'plan': data['plan'], 'now': now}


# ===== API для программы =====
def api_activate(data):
    key, hwid = normalize_key(data.get('key', '')), data.get('hwid', '').strip()
    if not key or not hwid:
        return {'status': 'error', 'message': 'Нет ключа или HWID'}
    if int(data.get('v') or 0) < CLIENT_VERSION:
        return {'status': 'error', 'message': 'Обновите SugarClick до новой версии'}
    now = int(time.time())
    with db() as conn:
        row = q(conn, 'SELECT plan, hwid, banned FROM sub_keys WHERE key = ?', (key,)).fetchone()
        if not row:
            return {'status': 'error', 'message': 'Неверный ключ'}
        plan, key_hwid, banned = row
        if banned:
            return {'status': 'error', 'message': 'Ключ заблокирован'}
        dev = get_device(conn, hwid)
        if dev and dev['banned']:
            return {'status': 'error', 'message': 'Компьютер заблокирован'}
        if key_hwid and key_hwid != hwid:
            return {'status': 'error', 'message': 'Ключ уже активирован на другом компьютере'}
        if not key_hwid:
            # атомарно занимаем ключ: если его успели активировать параллельно, rowcount будет 0
            cur = q(conn, 'UPDATE sub_keys SET hwid = ?, activated_at = ? WHERE key = ? AND hwid IS NULL', (hwid, now, key))
            if cur.rowcount != 1:
                return {'status': 'error', 'message': 'Ключ уже активирован на другом компьютере'}
            hours, name = PLANS[plan]
            extend_device(conn, hwid, hours, name, now)
            message = 'Ключ активирован'
        else:
            message = 'Ключ уже активирован на этом компьютере'
        dev = get_device(conn, hwid)
        q(conn, 'UPDATE devices SET last_seen = ? WHERE hwid = ?', (now, hwid))
    return dict(status='success', message=message, **license_payload(hwid, now, dev))


def api_trial(data):
    hwid = data.get('hwid', '').strip()
    if not hwid:
        return {'status': 'error', 'message': 'Нет HWID'}
    if int(data.get('v') or 0) < CLIENT_VERSION:
        return {'status': 'error', 'message': 'Обновите SugarClick до новой версии'}
    now = int(time.time())
    with db() as conn:
        dev = get_device(conn, hwid)
        if dev and dev['banned']:
            return {'status': 'error', 'message': 'Компьютер заблокирован'}
        if dev and dev['trial_at']:
            return {'status': 'error', 'message': 'Пробный час уже использован'}
        extend_device(conn, hwid, PLANS['hour'][0], PLANS['hour'][1], now, trial=True)
        dev = get_device(conn, hwid)
    return dict(status='success', message='Пробный час активирован', **license_payload(hwid, now, dev))


def api_check(data):
    hwid = data.get('hwid', '').strip()
    if not hwid:
        return {'status': 'error', 'message': 'Нет HWID'}
    now = int(time.time())
    with db() as conn:
        dev = get_device(conn, hwid)
        if dev:
            q(conn, 'UPDATE devices SET last_seen = ? WHERE hwid = ?', (now, hwid))
    return dict(status='success', **license_payload(hwid, now, dev))


# ===== Telegram-бот (webhook) =====
def tg(method, **params):
    if not TG_TOKEN:
        return None
    req = urllib.request.Request(f'https://api.telegram.org/bot{TG_TOKEN}/{method}',
                                 data=json.dumps(params).encode('utf-8'), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print('Telegram error:', method, e)
        return None


def fmt_left(seconds):
    if seconds <= 0:
        return 'истекла'
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f'{d} д {h} ч'
    if h:
        return f'{h} ч {m} мин'
    return f'{m} мин'


def fmt_time(ts):
    return time.strftime('%d.%m.%Y %H:%M', time.gmtime(ts + 3 * 3600)) + ' МСК' if ts else '—'


BOT_HELP = (
    '<b>SugarClick: выдача ключей</b>\n\n'
    '/key &lt;тариф&gt; [кол-во] — создать ключи\n'
    '    тарифы: <code>day</code>, <code>3days</code>, <code>week</code>, <code>month</code> (или 1d, 3d, 7d, 30d)\n'
    '    пример: <code>/key week 5</code>\n'
    '/info &lt;ключ&gt; — статус ключа и подписки\n'
    '/ban &lt;ключ&gt; — заблокировать ключ и компьютер\n'
    '/unban &lt;ключ&gt; — разблокировать\n'
    '/stats — статистика\n'
    '/id — ваш Telegram ID'
)


def bot_command(text, now):
    parts = text.split()
    cmd = parts[0].split('@')[0].lower()
    args = parts[1:]
    if cmd in ('/start', '/help'):
        return BOT_HELP
    if cmd == '/key':
        if not args:
            return 'Укажите тариф: <code>/key week 5</code>'
        plan = PLAN_ALIASES.get(args[0].lower(), args[0].lower())
        if plan not in PLANS or plan == 'hour':
            return 'Неизвестный тариф. Доступны: day, 3days, week, month'
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        n = max(1, min(n, 50))
        keys = []
        with db() as conn:
            while len(keys) < n:
                k = new_key()
                if not q(conn, 'SELECT 1 FROM sub_keys WHERE key = ?', (k,)).fetchone():
                    q(conn, 'INSERT INTO sub_keys (key, plan, created_at) VALUES (?, ?, ?)', (k, plan, now))
                    keys.append(k)
        return f'<b>{PLANS[plan][1]}</b> × {n}\n\n' + '\n'.join(f'<code>{k}</code>' for k in keys)
    if cmd in ('/info', '/ban', '/unban'):
        if not args:
            return f'Укажите ключ: <code>{cmd} SC-XXXX-XXXX-XXXX</code>'
        key = normalize_key(args[0])
        with db() as conn:
            row = q(conn, 'SELECT plan, hwid, activated_at, banned, created_at FROM sub_keys WHERE key = ?', (key,)).fetchone()
            if not row:
                return 'Ключ не найден'
            plan, hwid, activated_at, banned, created_at = row
            if cmd != '/info':
                flag = 1 if cmd == '/ban' else 0
                q(conn, 'UPDATE sub_keys SET banned = ? WHERE key = ?', (flag, key))
                if hwid:
                    q(conn, 'UPDATE devices SET banned = ? WHERE hwid = ?', (flag, hwid))
                return ('Заблокирован' if flag else 'Разблокирован') + f': <code>{key}</code>'
            dev = get_device(conn, hwid) if hwid else None
        lines = [f'<code>{key}</code>', f'Тариф: {PLANS[plan][1]}', f'Создан: {fmt_time(created_at)}']
        if banned:
            lines.append('⛔ Заблокирован')
        if not hwid:
            lines.append('Не активирован')
        else:
            lines.append(f'Активирован: {fmt_time(activated_at)}')
            lines.append(f'HWID: <code>{hwid}</code>')
            if dev:
                lines.append(f'Подписка до: {fmt_time(dev["expires_at"])} (осталось {fmt_left(dev["expires_at"] - now)})')
        return '\n'.join(lines)
    if cmd == '/stats':
        with db() as conn:
            total = q(conn, 'SELECT COUNT(*) FROM sub_keys').fetchone()[0]
            used = q(conn, 'SELECT COUNT(*) FROM sub_keys WHERE hwid IS NOT NULL').fetchone()[0]
            active = q(conn, 'SELECT COUNT(*) FROM devices WHERE expires_at > ? AND banned = 0', (now,)).fetchone()[0]
            trials = q(conn, 'SELECT COUNT(*) FROM devices WHERE trial_at IS NOT NULL').fetchone()[0]
            by_plan = q(conn, 'SELECT plan, COUNT(*), SUM(CASE WHEN hwid IS NULL THEN 0 ELSE 1 END) FROM sub_keys GROUP BY plan').fetchall()
        lines = ['<b>Статистика</b>', f'Ключей: {total}, активировано: {used}', f'Активных подписок: {active}', f'Пробных часов: {trials}', '']
        for plan, cnt, u in by_plan:
            lines.append(f'{PLANS.get(plan, (0, plan))[1]}: {u or 0} из {cnt}')
        return '\n'.join(lines)
    return 'Неизвестная команда. /help — список команд'


def handle_update(update):
    msg = update.get('message') or {}
    text = (msg.get('text') or '').strip()
    user = msg.get('from') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    if not text.startswith('/') or chat_id is None:
        return
    uid, uname = user.get('id'), (user.get('username') or '').lower()
    if text.split()[0].split('@')[0].lower() == '/id':
        reply = f'Ваш Telegram ID: <code>{uid}</code>'
    elif uid in ADMIN_IDS or (uname and uname in ADMIN_USERNAMES):
        try:
            reply = bot_command(text, int(time.time()))
        except Exception as e:
            reply = f'Ошибка: {e}'
    else:
        return  # чужим не отвечаем
    tg('sendMessage', chat_id=chat_id, text=reply, parse_mode='HTML', disable_web_page_preview=True)


def setup_webhook():
    base = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
    if not (TG_TOKEN and base):
        return
    res = tg('setWebhook', url=base + '/tg', secret_token=TG_SECRET, allowed_updates=['message'])
    print('setWebhook:', res)


# ===== HTTP =====
class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ('/', '/health'):
            self.send_json(200, {'status': 'ok'})
        elif self.path == '/pubkey':
            self.send_json(200, {'pubkey': base64.b64encode(SIGN_PUBLIC).decode()})
        else:
            self.send_json(404, {'status': 'error'})

    do_HEAD = do_GET

    def do_POST(self):
        length = min(int(self.headers.get('Content-Length', 0) or 0), 65536)
        raw = self.rfile.read(length)
        if self.path == '/tg':
            if self.headers.get('X-Telegram-Bot-Api-Secret-Token') != TG_SECRET:
                return self.send_json(403, {'status': 'error'})
            self.send_json(200, {'ok': True})
            try:
                handle_update(json.loads(raw.decode('utf-8')))
            except Exception as e:
                print('update error:', e)
            return
        routes = {'/activate': api_activate, '/trial': api_trial, '/check': api_check}
        fn = routes.get(self.path)
        if not fn:
            return self.send_json(404, {'status': 'error'})
        try:
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self.send_json(400, {'status': 'error', 'message': 'Invalid JSON'})
        try:
            self.send_json(200, fn(data))
        except Exception as e:
            print('api error:', self.path, e)
            self.send_json(500, {'status': 'error', 'message': 'Ошибка сервера'})


def run():
    init_db()
    threading.Thread(target=setup_webhook, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    httpd = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'Server running on port {port}, pubkey {base64.b64encode(SIGN_PUBLIC).decode()}')
    httpd.serve_forever()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'genkey':
        sk = secrets.token_bytes(32)
        print('SIGN_KEY (секрет, только в Environment на Render):', base64.b64encode(sk).decode())
        print('Публичный ключ (вставить в программу):', base64.b64encode(ed25519.public_key(sk)).decode())
    else:
        run()
