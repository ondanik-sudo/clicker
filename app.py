# Сервер подписок SugarClick: ключи с тарифами, пробный час, подписанные ответы (Ed25519) и Telegram-бот для выдачи ключей.
#
# Переменные окружения (на Render: Environment):
#   SIGN_KEY        секретный ключ подписи (base64, 32 байта), создаётся командой: python app.py genkey
#   DATABASE_URL    строка подключения к PostgreSQL (Neon / Supabase). Без неё - локальный licenses.db (только для тестов!)
#   TG_TOKEN        токен бота от @BotFather
#   ADMIN_IDS       числовые Telegram ID админов через запятую (узнать: команда /id в боте)
#   ADMIN_USERNAMES ники админов через запятую, без @ (по умолчанию danilsugar)
#   RENDER_EXTERNAL_URL  Render задаёт сам; по нему бот ставит webhook
#   PAY_CARD        номер карты для оплаты переводом (временно, до ЮKassa)
#   PAY_NAME        получатель, как он виден при переводе (например «Данил С.»)
#   PAY_BANK        банк карты (необязательно)
#   SUPPORT_USERNAME  ник для кнопки «Поддержка», без @ (по умолчанию danilsugar)
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import base64
import hashlib
import html
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import cards
import ed25519

DB_FILE = 'licenses.db'
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
TG_TOKEN = os.environ.get('TG_TOKEN', '').strip()
ADMIN_IDS = {int(x) for x in os.environ.get('ADMIN_IDS', '').replace(' ', '').split(',') if x.isdigit()}
ADMIN_USERNAMES = {x.strip().lstrip('@').lower() for x in os.environ.get('ADMIN_USERNAMES', 'danilsugar').split(',') if x.strip()}
TG_SECRET = hashlib.sha256(('sugarclick-webhook:' + TG_TOKEN).encode()).hexdigest()[:48]
CLIENT_VERSION = 2  # клиенты старше этой версии не могут активировать ключи (они не проверяют срок)
PAY_CARD = os.environ.get('PAY_CARD', '').strip()
PAY_NAME = os.environ.get('PAY_NAME', '').strip()
PAY_BANK = os.environ.get('PAY_BANK', '').strip()
SUPPORT_USERNAME = os.environ.get('SUPPORT_USERNAME', 'danilsugar').strip().lstrip('@')

# цены в рублях для продажи через бота
PRICES = {'day': 50, '3days': 100, 'week': 300, 'month': 550}
PLAN_LENGTH = {'day': '24 часа', '3days': '3 дня', 'week': '7 дней', 'month': '30 дней'}
REF_BONUS_PLAN = 'day'      # что получает пригласивший за каждую оплату друга
REF_DISCOUNT = 10           # скидка другу на первую покупку, %
REMIND_BEFORE = 3 * 3600    # за сколько до конца подписки бот напоминает

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
        # заказы из бота: new (показаны реквизиты) -> proof (ждём чек) -> review (чек у админов) -> paid / rejected / cancelled
        serial = 'SERIAL PRIMARY KEY' if DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        q(conn, f'''CREATE TABLE IF NOT EXISTS orders (
            id {serial},
            user_id BIGINT NOT NULL,
            username TEXT,
            first_name TEXT,
            plan TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            paid_at BIGINT,
            key TEXT,
            admin_msgs TEXT,
            decided_by TEXT
        )''')
        # чаты админов, которым слать чеки (ADMIN_USERNAMES не дают chat_id, пока админ сам не напишет боту)
        q(conn, 'CREATE TABLE IF NOT EXISTS bot_admins (uid BIGINT PRIMARY KEY)')
        # покупатели бота: кто пригласил, промокод на следующий заказ, напоминания, ожидание ввода (промокод / пожелание)
        q(conn, '''CREATE TABLE IF NOT EXISTS users (
            uid BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            ref_by BIGINT,
            created_at BIGINT,
            promo TEXT,
            remind INTEGER DEFAULT 1,
            reminded_for BIGINT DEFAULT 0,
            state TEXT
        )''')
        q(conn, '''CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY,
            percent INTEGER NOT NULL,
            uses_left INTEGER NOT NULL,
            used INTEGER DEFAULT 0,
            created_at BIGINT
        )''')
        # ключи, выданные покупателю для себя (покупка или бонус за друга): по ним бот находит его подписку
        q(conn, 'CREATE TABLE IF NOT EXISTS user_keys (key TEXT PRIMARY KEY, uid BIGINT NOT NULL, kind TEXT)')
    # колонки, добавленные после первого запуска: в Postgres есть IF NOT EXISTS, в SQLite просто ловим ошибку дубля
    for col, decl in (('gift', 'INTEGER DEFAULT 0'), ('wish', 'TEXT'), ('promo', 'TEXT'), ('base_amount', 'INTEGER'),
                      ('discount', 'TEXT'), ('ref_key', 'TEXT')):
        try:
            with db() as conn:
                q(conn, f'ALTER TABLE orders ADD COLUMN {"IF NOT EXISTS " if DATABASE_URL else ""}{col} {decl}')
        except Exception:
            pass
    with db() as conn:
        q(conn, "INSERT INTO user_keys (key, uid, kind) SELECT key, user_id, 'buy' FROM orders "
                "WHERE status = 'paid' AND key IS NOT NULL AND COALESCE(gift, 0) = 0 ON CONFLICT (key) DO NOTHING")


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


def make_keys(conn, plan, n, now):
    keys = []
    while len(keys) < n:
        k = new_key()
        if not q(conn, 'SELECT 1 FROM sub_keys WHERE key = ?', (k,)).fetchone():
            q(conn, 'INSERT INTO sub_keys (key, plan, created_at) VALUES (?, ?, ?)', (k, plan, now))
            keys.append(k)
    return keys


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
        # атомарно занимаем пробный час: при пачке одновременных запросов проверка выше пропускала несколько,
        # и каждый добавлял по часу. UPDATE ... WHERE trial_at IS NULL пройдёт ровно у одного (rowcount = 1)
        q(conn, 'INSERT INTO devices (hwid, expires_at, plan, last_seen) VALUES (?, 0, NULL, ?) ON CONFLICT (hwid) DO NOTHING', (hwid, now))
        cur = q(conn, 'UPDATE devices SET trial_at = ? WHERE hwid = ? AND trial_at IS NULL', (now, hwid))
        if cur.rowcount != 1:
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
def _tg_request(method, body, content_type):
    if not TG_TOKEN:
        return None
    req = urllib.request.Request(f'https://api.telegram.org/bot{TG_TOKEN}/{method}', data=body, headers={'Content-Type': content_type})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        # Telegram объясняет ошибку в теле ответа (например «message is not modified») - возвращаем его
        try:
            res = json.loads(e.read().decode('utf-8'))
        except Exception:
            res = {'ok': False, 'description': str(e)}
        if 'not modified' not in res.get('description', ''):
            print('Telegram error:', method, res.get('description'))
        return res
    except Exception as e:
        print('Telegram error:', method, e)
        return None


def tg(method, **params):
    return _tg_request(method, json.dumps(params).encode('utf-8'), 'application/json')


def tg_upload(method, fields, files):
    """Запрос с загрузкой картинок (multipart/form-data): files = {имя поля: PNG-байты}."""
    boundary = secrets.token_hex(16)
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        if not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode('utf-8'))
    for k, data in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{k}.png"\r\n'
                     f'Content-Type: image/png\r\n\r\n'.encode('utf-8') + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    return _tg_request(method, b''.join(parts), f'multipart/form-data; boundary={boundary}')


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
    '/stats — статистика и выручка\n'
    '/orders — заказы, ждущие проверки\n'
    '/promo &lt;код&gt; &lt;скидка %&gt; [использований] — создать промокод\n'
    '    пример: <code>/promo SUGAR20 20 50</code>; <code>/promo</code> — список, <code>/promo SUGAR20 off</code> — выключить\n'
    '/shop — меню покупателя (как его видят клиенты)\n'
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
        with db() as conn:
            keys = make_keys(conn, plan, n, now)
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
            sold, revenue = q(conn, "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid'").fetchone()
            month_rev = q(conn, "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid' AND paid_at > ?", (now - 30 * 86400,)).fetchone()[0]
            waiting = q(conn, "SELECT COUNT(*) FROM orders WHERE status = 'review'").fetchone()[0]
            invited = q(conn, 'SELECT COUNT(*) FROM users WHERE ref_by IS NOT NULL').fetchone()[0]
            bonuses = q(conn, 'SELECT COUNT(*) FROM orders WHERE ref_key IS NOT NULL').fetchone()[0]
            gifts = q(conn, "SELECT COUNT(*) FROM orders WHERE status = 'paid' AND gift = 1").fetchone()[0]
        lines = ['<b>Статистика</b>', 'База: ' + ('PostgreSQL' if DATABASE_URL else '⚠️ SQLite (временная, стирается при перезапуске!)'),
                 f'Ключей: {total}, активировано: {used}', f'Активных подписок: {active}', f'Пробных часов: {trials}', '',
                 f'Продано через бота: {sold} на {revenue} ₽', f'За 30 дней: {month_rev} ₽', f'Ждут проверки: {waiting}',
                 f'Подарков: {gifts}', f'Пришло по приглашениям: {invited}, бонусов выдано: {bonuses}', '']
        for plan, cnt, u in by_plan:
            lines.append(f'{PLANS.get(plan, (0, plan))[1]}: {u or 0} из {cnt}')
        return '\n'.join(lines)
    if cmd == '/promo':
        with db() as conn:
            if not args:
                rows = q(conn, 'SELECT code, percent, uses_left, used FROM promos ORDER BY created_at DESC').fetchall()
                if not rows:
                    return 'Промокодов нет. Создать: <code>/promo SUGAR20 20 50</code>'
                return '<b>Промокоды</b>\n\n' + '\n'.join(
                    f'<code>{c}</code> · −{p}% · осталось {left} · использован {u or 0}' + (' · выключен' if left <= 0 else '')
                    for c, p, left, u in rows)
            code = args[0].upper()
            if len(args) > 1 and args[1].lower() == 'off':
                cur = q(conn, 'UPDATE promos SET uses_left = 0 WHERE code = ?', (code,))
                return f'Промокод <code>{html.escape(code)}</code> выключен' if cur.rowcount else 'Промокод не найден'
            if len(args) < 2 or not args[1].isdigit() or not 1 <= int(args[1]) <= 90:
                return 'Формат: <code>/promo КОД скидка% [использований]</code>, скидка от 1 до 90'
            if not code.replace('_', '').replace('-', '').isalnum() or len(code) > 24:
                return 'Код: латиница и цифры, до 24 символов'
            percent = int(args[1])
            uses = int(args[2]) if len(args) > 2 and args[2].isdigit() else 100
            if q(conn, 'SELECT 1 FROM promos WHERE code = ?', (code,)).fetchone():
                q(conn, 'UPDATE promos SET percent = ?, uses_left = ? WHERE code = ?', (percent, uses, code))
            else:
                q(conn, 'INSERT INTO promos (code, percent, uses_left, used, created_at) VALUES (?, ?, ?, 0, ?)', (code, percent, uses, now))
        return f'Промокод <code>{html.escape(code)}</code>: −{percent}%, {uses} использований'
    if cmd == '/orders':
        with db() as conn:
            rows = q(conn, "SELECT id, plan, amount, username, user_id, created_at FROM orders WHERE status = 'review' ORDER BY id").fetchall()
        if not rows:
            return 'Заказов на проверке нет'
        return '<b>Ждут проверки</b>\n\n' + '\n'.join(
            f'#{oid} · {PLANS[plan][1]} · {amount} ₽ · {buyer_label(uname, uid)} · {fmt_time(ts)}' for oid, plan, amount, uname, uid, ts in rows)
    return 'Неизвестная команда. /help — список команд'


# ===== Магазин: оплата переводом на карту с ручным подтверждением (временно, до ЮKassa) =====
# Экран бота = (подпись, кнопки, картинка). Картинка = (ключ кэша или None, функция, рисующая PNG).
# Картинку с ключом рисуем и загружаем один раз, дальше шлём её file_id из Telegram.
_photo_ids = {}
_bot_username = None


def is_admin(user):
    uname = (user.get('username') or '').lower()
    return user.get('id') in ADMIN_IDS or bool(uname and uname in ADMIN_USERNAMES)


def buyer_label(username, user_id):
    return f'@{html.escape(username)}' if username else f'id {user_id}'


def bot_username():
    global _bot_username
    if not _bot_username:
        res = tg('getMe') or {}
        _bot_username = (res.get('result') or {}).get('username')
    return _bot_username or 'SugarClickBot'


def admin_chats():
    with db() as conn:
        seen = {r[0] for r in q(conn, 'SELECT uid FROM bot_admins').fetchall()}
    return ADMIN_IDS | seen


def btn(text, data):
    return {'text': text, 'callback_data': data}


def send(chat_id, text, buttons=None):
    params = dict(chat_id=chat_id, text=text, parse_mode='HTML', disable_web_page_preview=True)
    if buttons:
        params['reply_markup'] = {'inline_keyboard': buttons}
    return tg('sendMessage', **params)


def _remember_photo(cache_key, res):
    if cache_key and res and res.get('ok'):
        photos = (res.get('result') or {}).get('photo') or []
        if photos:
            _photo_ids[cache_key] = photos[-1]['file_id']


def send_screen(chat_id, caption, buttons, image):
    cache_key, render = image
    markup = {'inline_keyboard': buttons or []}
    if cache_key in _photo_ids:
        res = tg('sendPhoto', chat_id=chat_id, photo=_photo_ids[cache_key], caption=caption, parse_mode='HTML', reply_markup=markup)
        if res and res.get('ok'):
            return res
    res = tg_upload('sendPhoto', {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML', 'reply_markup': markup}, {'photo': render()})
    _remember_photo(cache_key, res)
    if not (res and res.get('ok')):
        # картинка не ушла - пусть хотя бы текст дойдёт
        res = send(chat_id, caption, buttons)
    return res


def show(chat_id, message_id, screen):
    """Меняет экран в том же сообщении (картинку, подпись и кнопки), а если не вышло - присылает новое."""
    caption, buttons, (cache_key, render) = screen
    if message_id:
        markup = {'inline_keyboard': buttons or []}
        media = {'type': 'photo', 'caption': caption, 'parse_mode': 'HTML'}
        if cache_key in _photo_ids:
            media['media'] = _photo_ids[cache_key]
            res = tg('editMessageMedia', chat_id=chat_id, message_id=message_id, media=media, reply_markup=markup)
        else:
            media['media'] = 'attach://img'
            res = tg_upload('editMessageMedia', {'chat_id': chat_id, 'message_id': message_id, 'media': media, 'reply_markup': markup},
                            {'img': render()})
            _remember_photo(cache_key, res)
        if res and (res.get('ok') or 'not modified' in res.get('description', '')):
            return
        # старое текстовое сообщение (из прошлой версии бота) картинкой не заменить: убираем его и шлём новое
        tg('deleteMessage', chat_id=chat_id, message_id=message_id)
    send_screen(chat_id, caption, buttons, (cache_key, render))


# ----- данные покупателя -----
def ensure_user(user, ref=None):
    """Запоминает покупателя. ref - кто пригласил (только для новых, кто ещё ничего не заказывал)."""
    uid = user.get('id')
    now = int(time.time())
    with db() as conn:
        exists = q(conn, 'SELECT 1 FROM users WHERE uid = ?', (uid,)).fetchone()
        if exists:
            q(conn, 'UPDATE users SET username = ?, first_name = ? WHERE uid = ?', (user.get('username'), user.get('first_name'), uid))
            return False
        ref_ok = (ref and ref != uid and q(conn, 'SELECT 1 FROM users WHERE uid = ?', (ref,)).fetchone()
                  and not q(conn, 'SELECT 1 FROM orders WHERE user_id = ?', (uid,)).fetchone())
        q(conn, 'INSERT INTO users (uid, username, first_name, ref_by, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (uid) DO NOTHING',
          (uid, user.get('username'), user.get('first_name'), ref if ref_ok else None, now))
    return bool(ref_ok)


def get_user(conn, uid):
    row = q(conn, 'SELECT ref_by, promo, remind, state FROM users WHERE uid = ?', (uid,)).fetchone()
    return dict(zip(('ref_by', 'promo', 'remind', 'state'), row)) if row else {'ref_by': None, 'promo': None, 'remind': 1, 'state': None}


def set_state(uid, state):
    with db() as conn:
        q(conn, 'UPDATE users SET state = ? WHERE uid = ?', (state, uid))


def subscription(uid):
    """Подписка покупателя по его ключам: (конец, тариф, есть ли купленные ключи)."""
    with db() as conn:
        has_keys = bool(q(conn, 'SELECT 1 FROM user_keys WHERE uid = ?', (uid,)).fetchone())
        row = q(conn, 'SELECT d.expires_at, d.plan FROM user_keys uk JOIN sub_keys s ON s.key = uk.key JOIN devices d ON d.hwid = s.hwid '
                      'WHERE uk.uid = ? AND d.banned = 0 ORDER BY d.expires_at DESC LIMIT 1', (uid,)).fetchone()
    return (row[0] or 0, row[1] or '', has_keys) if row else (0, '', has_keys)


def sub_state(expires, now):
    if expires <= now:
        return 'off'
    return 'warn' if expires - now <= REMIND_BEFORE else 'on'


def price_for(conn, uid, plan):
    """Цена с лучшей из скидок (они не складываются): -10% другу на первую покупку или промокод."""
    base = PRICES[plan]
    user = get_user(conn, uid)
    best, label, promo = 0, None, None
    if user['ref_by'] and not q(conn, "SELECT 1 FROM orders WHERE user_id = ? AND status = 'paid'", (uid,)).fetchone():
        best, label = REF_DISCOUNT, f'−{REF_DISCOUNT}% по приглашению'
    if user['promo']:
        row = q(conn, 'SELECT percent FROM promos WHERE code = ? AND uses_left > 0', (user['promo'],)).fetchone()
        if row and row[0] > best:
            best, label, promo = row[0], f'−{row[0]}% промокод {user["promo"]}', user['promo']
    amount = max(1, round(base * (100 - best) / 100))
    return amount, base, label, promo


# ----- экраны -----
def menu_screen(uid):
    expires, plan, has_keys = subscription(uid)
    now = int(time.time())
    status = sub_state(expires, now) if has_keys else None
    plans = list(PRICES)
    rows = [[btn(f'{PLANS[p][1]} · {PRICES[p]} ₽', f'buy:{p}') for p in plans[i:i + 2]] for i in range(0, len(plans), 2)]
    rows += [[btn('⏳ Моя подписка', 'status'), btn('👥 Пригласить друга', 'invite')],
             [btn('🎁 Подарить подписку', 'giftmenu'), btn('🏷 Промокод', 'promo')],
             [btn('🧾 Мои покупки', 'my'), btn('💬 Поддержка', 'support')]]
    lines = ['Выберите тариф или откройте нужный раздел.']
    if status in ('on', 'warn'):
        lines.append(f'Подписка «{html.escape(plan)}»: осталось {fmt_left(expires - now)}.')
    with db() as conn:
        amount, base, label, promo = price_for(conn, uid, 'day')
    if label:
        lines.append(f'\n🏷 Ваша скидка на следующий заказ: <b>{html.escape(label)}</b>')
    return '\n'.join(lines), rows, (f'menu:{status}', lambda: cards.banner(status))


def plan_image(plan, amount, base, label, gift):
    sub = PLAN_LENGTH[plan] + (' · в подарок' if gift else ' · отсчёт с активации')
    badge = label or ('Подарок другу' if gift else None)
    return (f'plan:{plan}:{amount}:{base}:{label}:{gift}',
            lambda: cards.plan_card(PLANS[plan][1], sub, amount, base if amount != base else None, badge))


def order_screen(oid):
    with db() as conn:
        plan, amount, base, label, gift, wish = q(conn, 'SELECT plan, amount, base_amount, discount, gift, wish FROM orders WHERE id = ?',
                                                  (oid,)).fetchone()
    base = base or amount
    lines = [f'<b>Заказ #{oid} · {PLANS[plan][1]}' + (' · подарок' if gift else '') + '</b>']
    if label:
        lines.append(f'Скидка {html.escape(label)}')
    lines += ['', f'Переведите <b>{amount} ₽</b> на карту:', f'<code>{html.escape(PAY_CARD)}</code>']
    if PAY_NAME:
        lines.append(f'Получатель: {html.escape(PAY_NAME)}')
    if PAY_BANK:
        lines.append(f'Банк: {html.escape(PAY_BANK)}')
    if gift:
        lines += ['', f'Пожелание на открытке: «{html.escape(wish)}»' if wish else 'Можно добавить пожелание — оно будет на открытке с ключом.']
    lines += ['', f'Переведите ровно {amount} ₽ одним платежом. После перевода нажмите «Я оплатил» и пришлите скриншот чека.']
    buttons = [[btn('✅ Я оплатил', f'paid:{oid}')], [btn('🏷 Ввести промокод', f'opromo:{oid}')]]
    if gift:
        buttons.append([btn('✍️ Пожелание на открытке', f'wish:{oid}')])
    buttons.append([btn('← Отменить заказ', f'cancel:{oid}')])
    return '\n'.join(lines), buttons, plan_image(plan, amount, base, label, gift)


def proof_screen(oid):
    text = (f'<b>Заказ #{oid}</b>\n\nПришлите сюда скриншот чека о переводе (картинкой или файлом). '
            'Администратор проверит оплату, и ключ придёт в этот чат.')
    return text, [[btn('← Отменить заказ', f'cancel:{oid}')]], \
        ('title:wait', lambda: cards.title_card('Ждём чек', 'пришлите скриншот перевода', 'clock'))


def support_screen():
    text = f'По вопросам оплаты и работы программы пишите @{html.escape(SUPPORT_USERNAME)}'
    buttons = [[{'text': '💬 Написать', 'url': f'https://t.me/{SUPPORT_USERNAME}'}], [btn('← Назад', 'menu')]]
    return text, buttons, ('title:support', lambda: cards.title_card('Поддержка', 'ответим в Telegram', 'chat'))


def purchases_screen(uid):
    with db() as conn:
        rows = q(conn, "SELECT id, plan, amount, status, paid_at, key, gift FROM orders WHERE user_id = ? "
                       "AND status IN ('paid', 'review') ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        bonus = q(conn, "SELECT key FROM user_keys WHERE uid = ? AND kind = 'ref' ORDER BY key", (uid,)).fetchall()
    if not rows and not bonus:
        text = 'Покупок пока нет'
    else:
        items = []
        for oid, plan, amount, status, paid_at, key, gift in rows:
            head = f'#{oid} · {PLANS[plan][1]}' + (' · 🎁 подарок' if gift else '') + f' · {amount} ₽'
            items.append(f'{head} · {fmt_time(paid_at)}\n<code>{key}</code>' if status == 'paid' else f'{head} · ⏳ на проверке')
        if bonus:
            items.append('Бонусы за друзей (+1 день):\n' + '\n'.join(f'<code>{k}</code>' for (k,) in bonus))
        text = '<b>Мои покупки</b>\n\n' + '\n\n'.join(items)
    return text[:1000], [[btn('← Назад', 'menu')]], ('title:my', lambda: cards.title_card('Мои покупки', 'ключи и заказы', 'list'))


def status_screen(uid):
    expires, plan, has_keys = subscription(uid)
    now = int(time.time())
    state = sub_state(expires, now)
    with db() as conn:
        remind = get_user(conn, uid)['remind']
    hours_of = {name: h for h, name in PLANS.values()}
    if state == 'off':
        left, title, until = '—', plan or 'Нет подписки', 'истекла ' + fmt_time(expires) if expires else ''
        if has_keys and not expires:
            title, until = 'Ключ не активирован', 'введите ключ в программе'
        text = ('Ключ куплен, но ещё не активирован в программе.' if has_keys and not expires
                else 'Активной подписки нет. Выберите тариф — время пойдёт с момента активации ключа.')
    else:
        left, title, until = fmt_left(expires - now), plan, 'до ' + fmt_time(expires)
        text = 'Продление добавит время к текущему сроку.'
        text += '\nНапоминание придёт за 3 часа до конца.' if remind else '\nНапоминания выключены.'
    frac = (expires - now) / (hours_of.get(plan, 24) * 3600) if state != 'off' else 0
    label = 'ключ ждёт активации' if has_keys and not expires else None
    buttons = [[btn('🔄 Продлить' if state != 'off' else '🛒 Купить', 'menu'),
                btn('🔔 Напоминания: вкл' if remind else '🔕 Напоминания: выкл', 'remind')], [btn('← Назад', 'menu')]]
    return text, buttons, (None, lambda: cards.status_card(left, title, until, frac, state, label))


def invite_screen(uid):
    link = f'https://t.me/{bot_username()}?start=r{uid}'
    with db() as conn:
        invited = q(conn, 'SELECT COUNT(*) FROM users WHERE ref_by = ?', (uid,)).fetchone()[0]
        paid = q(conn, "SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN users u ON u.uid = o.user_id "
                       "WHERE u.ref_by = ? AND o.status = 'paid'", (uid,)).fetchone()[0]
        earned = q(conn, "SELECT COUNT(*) FROM user_keys WHERE uid = ? AND kind = 'ref'", (uid,)).fetchone()[0]
    text = (f'Приглашайте друзей: за каждую их оплату вы получаете <b>+1 день</b> подписки, '
            f'а друг — <b>−{REF_DISCOUNT}%</b> на первую покупку.\n\nВаша ссылка:\n<code>{link}</code>\n\n'
            f'Приглашено: <b>{invited}</b> · оплатили: <b>{paid}</b>\nПолучено: <b>+{earned} д</b>')
    share = 'https://t.me/share/url?' + urllib.parse.urlencode(
        {'url': link, 'text': f'SugarClick — автокликер для «Номерка». По моей ссылке −{REF_DISCOUNT}% на первую подписку'})
    buttons = [[{'text': '📨 Поделиться ссылкой', 'url': share}], [btn('← Назад', 'menu')]]
    return text, buttons, ('invite', lambda: cards.invite_card())


def giftmenu_screen():
    plans = list(PRICES)
    rows = [[btn(f'🎁 {PLANS[p][1]} · {PRICES[p]} ₽', f'gift:{p}') for p in plans[i:i + 2]] for i in range(0, len(plans), 2)]
    rows.append([btn('← Назад', 'menu')])
    text = ('Подарите подписку другу: после оплаты бот пришлёт открытку с ключом и вашим пожеланием. '
            'Её можно переслать — друг активирует ключ у себя.')
    return text, rows, ('title:gift', lambda: cards.title_card('Подарить подписку', 'ключ придёт открыткой', 'cube'))


def promo_screen(back):
    text = 'Отправьте промокод сообщением. Скидка применится к заказу.'
    return text, [[btn('← Назад', back)]], ('title:promo', lambda: cards.title_card('Промокод', 'отправьте код сообщением', 'tag'))


def wish_screen(oid):
    text = 'Напишите пожелание для открытки одним сообщением (до 60 символов). Например: «С днём рождения!»'
    return text, [[btn('← Назад', f'order:{oid}')]], ('title:wish', lambda: cards.title_card('Пожелание', 'будет на открытке с ключом', 'chat'))


# ----- заказы -----
def create_order(user, plan, gift):
    uid = user.get('id')
    now = int(time.time())
    with db() as conn:
        # у покупателя один открытый заказ: старые неоплаченные закрываем
        q(conn, "UPDATE orders SET status = 'cancelled' WHERE user_id = ? AND status IN ('new', 'proof')", (uid,))
        amount, base, label, promo = price_for(conn, uid, plan)
        return q(conn, "INSERT INTO orders (user_id, username, first_name, plan, amount, base_amount, discount, promo, gift, status, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?) RETURNING id",
                 (uid, user.get('username'), user.get('first_name'), plan, amount, base, label, promo, 1 if gift else 0, now)).fetchone()[0]


def apply_promo(uid, code, oid):
    """Проверяет промокод и запоминает его покупателю; если есть открытый заказ - пересчитывает цену."""
    code = code.strip().upper()
    with db() as conn:
        row = q(conn, 'SELECT percent FROM promos WHERE code = ? AND uses_left > 0', (code,)).fetchone()
        if not row:
            return None
        q(conn, 'UPDATE users SET promo = ?, state = NULL WHERE uid = ?', (code, uid))
        if oid:
            order = q(conn, "SELECT plan FROM orders WHERE id = ? AND user_id = ? AND status IN ('new', 'proof')", (oid, uid)).fetchone()
            if order:
                amount, base, label, promo = price_for(conn, uid, order[0])
                q(conn, 'UPDATE orders SET amount = ?, base_amount = ?, discount = ?, promo = ? WHERE id = ?', (amount, base, label, promo, oid))
    return row[0]


def review_caption(oid, plan, amount, username, user_id, first_name, gift=0, label=None):
    who = buyer_label(username, user_id)
    if first_name:
        who += f' ({html.escape(first_name)})'
    extra = (' · 🎁 подарок' if gift else '') + (f'\nСкидка: {html.escape(label)}' if label else '')
    return (f'💳 <b>Заказ #{oid}</b> · {PLANS[plan][1]} · <b>{amount} ₽</b>{extra}\n'
            f'Покупатель: {who}, id <code>{user_id}</code>\n'
            f'Проверьте, что перевод пришёл, и подтвердите.')


def handle_proof(msg, user, chat_id):
    """Покупатель прислал чек: отправляем его всем админам с кнопками подтверждения."""
    uid = user.get('id')
    with db() as conn:
        row = q(conn, "SELECT id, plan, amount, gift, discount FROM orders WHERE user_id = ? AND status IN ('new', 'proof') "
                      "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not row:
            return False
        oid, plan, amount, gift, label = row
        q(conn, "UPDATE orders SET status = 'review' WHERE id = ?", (oid,))
    caption = review_caption(oid, plan, amount, user.get('username'), uid, user.get('first_name'), gift, label)
    buttons = {'inline_keyboard': [[btn('✅ Подтвердить', f'ok:{oid}'), btn('❌ Отклонить', f'no:{oid}')]]}
    sent = []
    for cid in admin_chats():
        if msg.get('photo'):
            res = tg('sendPhoto', chat_id=cid, photo=msg['photo'][-1]['file_id'], caption=caption, parse_mode='HTML', reply_markup=buttons)
        else:
            res = tg('sendDocument', chat_id=cid, document=msg['document']['file_id'], caption=caption, parse_mode='HTML', reply_markup=buttons)
        if res and res.get('ok'):
            sent.append([cid, res['result']['message_id']])
    with db() as conn:
        q(conn, 'UPDATE orders SET admin_msgs = ? WHERE id = ?', (json.dumps(sent), oid))
    if not sent:
        print('Чек по заказу', oid, 'некому отправить: нет chat_id админов (ADMIN_IDS или /start от админа)')
    send(chat_id, f'Чек по заказу #{oid} получен ✅\nОжидайте подтверждения — ключ придёт сюда же.')
    return True


def update_admin_msgs(admin_msgs, caption):
    for cid, mid in json.loads(admin_msgs or '[]'):
        tg('editMessageCaption', chat_id=cid, message_id=mid, caption=caption, parse_mode='HTML')


def decide_order(oid, approve, admin_user):
    """Админ подтвердил или отклонил оплату. Возвращает текст всплывающего ответа на кнопку."""
    now = int(time.time())
    admin_name = '@' + admin_user['username'] if admin_user.get('username') else str(admin_user.get('id'))
    ref_by = ref_key = None
    with db() as conn:
        row = q(conn, 'SELECT plan, amount, user_id, username, first_name, status, admin_msgs, decided_by, gift, wish, promo, discount '
                      'FROM orders WHERE id = ?', (oid,)).fetchone()
        if not row:
            return 'Заказ не найден'
        plan, amount, user_id, username, first_name, status, admin_msgs, decided_by, gift, wish, promo, label = row
        if status != 'review':
            return f'Заказ уже обработан ({decided_by or status})'
        # атомарно: если два админа нажали одновременно, пройдёт только один
        new_status = 'paid' if approve else 'rejected'
        cur = q(conn, "UPDATE orders SET status = ?, decided_by = ?, paid_at = ? WHERE id = ? AND status = 'review'",
                (new_status, admin_name, now if approve else None, oid))
        if cur.rowcount != 1:
            return 'Заказ уже обработан'
        if approve:
            key = make_keys(conn, plan, 1, now)[0]
            q(conn, 'UPDATE orders SET key = ? WHERE id = ?', (key, oid))
            if not gift:
                q(conn, "INSERT INTO user_keys (key, uid, kind) VALUES (?, ?, 'buy')", (key, user_id))
            if promo:
                q(conn, 'UPDATE promos SET uses_left = uses_left - 1, used = used + 1 WHERE code = ? AND uses_left > 0', (promo,))
                q(conn, 'UPDATE users SET promo = NULL WHERE uid = ? AND promo = ?', (user_id, promo))
            ref_by = get_user(conn, user_id)['ref_by']
            if ref_by:
                ref_key = make_keys(conn, REF_BONUS_PLAN, 1, now)[0]
                q(conn, "INSERT INTO user_keys (key, uid, kind) VALUES (?, ?, 'ref')", (ref_key, ref_by))
                q(conn, 'UPDATE orders SET ref_key = ? WHERE id = ?', (ref_key, oid))
    caption = review_caption(oid, plan, amount, username, user_id, first_name, gift, label).rsplit('\n', 1)[0]
    if not approve:
        send(user_id, f'❌ Оплата по заказу #{oid} не подтверждена.\n\n'
                      f'Если вы перевели деньги, напишите @{html.escape(SUPPORT_USERNAME)} и приложите чек.',
             [[btn('← В меню', 'menu!')]])
        update_admin_msgs(admin_msgs, caption + f'\n\n❌ Отклонил {html.escape(admin_name)}')
        return 'Заказ отклонён'
    if gift:
        sender = '@' + username if username else (first_name or 'друга')
        send_screen(user_id, f'🎁 <b>Подарок готов</b>\n\nПерешлите эту открытку другу. Ключ:\n<code>{key}</code>\n\n'
                             'Друг вводит ключ в окне активации SugarClick, время пойдёт с момента активации.',
                    [[btn('← В меню', 'menu!')]], (None, lambda: cards.gift_card(sender, PLANS[plan][1], key, wish or '')))
    else:
        send_screen(user_id, f'✅ <b>Оплата подтверждена</b>\n\nЗаказ #{oid} · {PLANS[plan][1]}\nВаш ключ:\n<code>{key}</code>\n\n'
                             'Откройте SugarClick и введите ключ в окне активации. Время подписки пойдёт с момента активации.',
                    [[btn('⏳ Моя подписка', 'status!'), btn('🧾 Мои покупки', 'my!')]],
                    ('title:paid', lambda: cards.title_card('Оплата подтверждена', 'ключ в сообщении', 'check', 'green')))
    if ref_by:
        friend = '@' + html.escape(username) if username else html.escape(first_name or 'Друг')
        send_screen(ref_by, f'👥 <b>Друг оплатил подписку</b>\n\n{friend} купил «{PLANS[plan][1]}» по вашей ссылке. '
                            f'Ваш бонус +1 день:\n<code>{ref_key}</code>\n\nАктивируйте ключ в программе — время добавится к подписке.',
                    [[btn('👥 Пригласить ещё', 'invite!')]], ('invite', lambda: cards.invite_card()))
    update_admin_msgs(admin_msgs, caption + f'\n\n✅ Подтвердил {html.escape(admin_name)}, ключ <code>{key}</code>')
    return 'Ключ отправлен покупателю'


# ----- обработка Telegram -----
def handle_callback(cb):
    user = cb.get('from') or {}
    uid = user.get('id')
    msg = cb.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id') or uid
    mid = msg.get('message_id')
    data = cb.get('data') or ''
    if data.endswith('!'):
        # кнопки под ключом или напоминанием открывают экран новым сообщением, чтобы ключ не пропал из чата
        data, mid = data[:-1], None
    action, _, arg = data.partition(':')
    toast = None
    if action in ('ok', 'no'):
        toast = decide_order(int(arg), action == 'ok', user) if is_admin(user) else 'Только для админов'
        return tg('answerCallbackQuery', callback_query_id=cb.get('id'), text=toast)
    ensure_user(user)
    if action not in ('promo', 'opromo', 'wish'):
        set_state(uid, None)
    if action == 'menu':
        show(chat_id, mid, menu_screen(uid))
    elif action == 'my':
        show(chat_id, mid, purchases_screen(uid))
    elif action == 'support':
        show(chat_id, mid, support_screen())
    elif action == 'status':
        show(chat_id, mid, status_screen(uid))
    elif action == 'invite':
        show(chat_id, mid, invite_screen(uid))
    elif action == 'giftmenu':
        show(chat_id, mid, giftmenu_screen())
    elif action == 'remind':
        with db() as conn:
            q(conn, 'UPDATE users SET remind = 1 - COALESCE(remind, 1) WHERE uid = ?', (uid,))
        show(chat_id, mid, status_screen(uid))
    elif action == 'remindoff':
        with db() as conn:
            q(conn, 'UPDATE users SET remind = 0 WHERE uid = ?', (uid,))
        toast = 'Напоминания выключены. Включить можно в «Моя подписка»'
    elif action == 'promo':
        set_state(uid, 'promo')
        show(chat_id, mid, promo_screen('menu'))
    elif action in ('buy', 'gift') and arg in PRICES:
        if not PAY_CARD:
            toast = 'Оплата временно недоступна, напишите в поддержку'
        else:
            show(chat_id, mid, order_screen(create_order(user, arg, action == 'gift')))
    elif action in ('order', 'opromo', 'wish', 'paid', 'cancel'):
        oid = int(arg)
        with db() as conn:
            row = q(conn, "SELECT status FROM orders WHERE id = ? AND user_id = ?", (oid, uid)).fetchone()
        if not row or row[0] not in ('new', 'proof'):
            toast = 'Этот заказ уже закрыт'
        elif action == 'order':
            show(chat_id, mid, order_screen(oid))
        elif action == 'opromo':
            set_state(uid, f'promo:{oid}')
            show(chat_id, mid, promo_screen(f'order:{oid}'))
        elif action == 'wish':
            set_state(uid, f'wish:{oid}')
            show(chat_id, mid, wish_screen(oid))
        elif action == 'paid':
            with db() as conn:
                q(conn, "UPDATE orders SET status = 'proof' WHERE id = ? AND status IN ('new', 'proof')", (oid,))
            show(chat_id, mid, proof_screen(oid))
        else:
            with db() as conn:
                q(conn, "UPDATE orders SET status = 'cancelled' WHERE id = ? AND status IN ('new', 'proof')", (oid,))
            show(chat_id, mid, menu_screen(uid))
            toast = 'Заказ отменён'
    params = {'callback_query_id': cb.get('id')}
    if toast:
        params['text'] = toast
    tg('answerCallbackQuery', **params)


def handle_text_input(uid, chat_id, text, state):
    """Покупатель ввёл промокод или пожелание после нажатия кнопки."""
    kind, _, arg = state.partition(':')
    oid = int(arg) if arg.isdigit() else None
    if kind == 'promo':
        percent = apply_promo(uid, text, oid)
        if percent is None:
            return send(chat_id, 'Такого промокода нет или он закончился. Попробуйте другой.',
                        [[btn('← Назад', f'order:{oid}' if oid else 'menu')]])
        if oid:
            send(chat_id, f'Промокод применён: −{percent}% ✅')
            return send_screen(chat_id, *order_screen(oid))
        return send_screen(chat_id, *menu_screen(uid))
    if kind == 'wish' and oid:
        with db() as conn:
            q(conn, "UPDATE orders SET wish = ? WHERE id = ? AND user_id = ? AND status IN ('new', 'proof')", (text[:60], oid, uid))
            q(conn, 'UPDATE users SET state = NULL WHERE uid = ?', (uid,))
        return send_screen(chat_id, *order_screen(oid))


def handle_update(update):
    if update.get('callback_query'):
        return handle_callback(update['callback_query'])
    msg = update.get('message') or {}
    chat = msg.get('chat') or {}
    chat_id = chat.get('id')
    user = msg.get('from') or {}
    if chat_id is None:
        return
    uid = user.get('id')
    admin = is_admin(user)
    if admin:
        with db() as conn:
            q(conn, 'INSERT INTO bot_admins (uid) VALUES (?) ON CONFLICT (uid) DO NOTHING', (uid,))
    text = (msg.get('text') or '').strip()
    parts = text.split()
    cmd = parts[0].split('@')[0].lower() if text.startswith('/') else ''
    if cmd == '/id':
        return send(chat_id, f'Ваш Telegram ID: <code>{uid}</code>')
    if chat.get('type') != 'private':
        return
    # /start r123 - пришёл по ссылке приглашения
    ref = None
    if cmd == '/start' and len(parts) > 1 and parts[1].startswith('r') and parts[1][1:].isdigit():
        ref = int(parts[1][1:])
    if ensure_user(user, ref):
        name = '@' + html.escape(user['username']) if user.get('username') else html.escape(user.get('first_name') or 'Новый пользователь')
        send(ref, f'👥 По вашей ссылке пришёл {name}. Когда он оплатит подписку, вы получите +1 день.')
    if msg.get('photo') or msg.get('document'):
        if not handle_proof(msg, user, chat_id) and not admin:
            send(chat_id, 'Сначала выберите тариф, затем пришлите чек.', [[btn('🛒 К тарифам', 'menu!')]])
        return
    if admin and cmd and cmd not in ('/shop', '/start'):
        try:
            reply = bot_command(text, int(time.time()))
        except Exception as e:
            reply = f'Ошибка: {html.escape(str(e))}'
        return send(chat_id, reply)
    if admin and cmd == '/start' and not ref:
        send(chat_id, BOT_HELP)
        return
    with db() as conn:
        state = get_user(conn, uid)['state']
        waiting = q(conn, "SELECT id FROM orders WHERE user_id = ? AND status = 'proof' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if cmd:
        set_state(uid, None)
    elif state and text:
        return handle_text_input(uid, chat_id, text, state)
    elif waiting:
        return send_screen(chat_id, *proof_screen(waiting[0]))
    elif admin:
        return
    send_screen(chat_id, *menu_screen(uid))


def reminder_loop():
    """Раз в 10 минут: кому до конца подписки осталось меньше 3 часов - напоминаем (один раз на каждый срок)."""
    while True:
        time.sleep(600)
        try:
            now = int(time.time())
            with db() as conn:
                rows = q(conn, 'SELECT uk.uid, MAX(d.expires_at) FROM user_keys uk JOIN sub_keys s ON s.key = uk.key '
                               'JOIN devices d ON d.hwid = s.hwid WHERE d.banned = 0 GROUP BY uk.uid').fetchall()
                due = []
                for uid, expires in rows:
                    if expires and 0 < expires - now <= REMIND_BEFORE:
                        u = q(conn, 'SELECT remind, reminded_for FROM users WHERE uid = ?', (uid,)).fetchone()
                        if u and u[0] and u[1] != expires:
                            q(conn, 'UPDATE users SET reminded_for = ? WHERE uid = ?', (expires, uid))
                            due.append((uid, expires))
            for uid, expires in due:
                send_screen(uid, f'⏳ <b>Подписка заканчивается</b>\n\nОсталось {fmt_left(expires - now)}. '
                                 'Продлите, чтобы кликер не остановился — время добавится к текущему сроку.',
                            [[btn('🔄 Продлить', 'menu!'), btn('🔕 Не напоминать', 'remindoff')]],
                            ('title:remind', lambda: cards.title_card('Подписка заканчивается', 'продлите, чтобы кликер не остановился', 'clock')))
        except Exception as e:
            print('reminder error:', e)


def setup_webhook():
    base = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
    if not (TG_TOKEN and base):
        return
    res = tg('setWebhook', url=base + '/tg', secret_token=TG_SECRET, allowed_updates=['message', 'callback_query'])
    print('setWebhook:', res)
    tg('setMyCommands', commands=[{'command': 'start', 'description': 'Меню: тарифы, подписка, приглашения'}])


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
            self.send_json(200, {'status': 'ok', 'db': 'postgres' if DATABASE_URL else 'sqlite'})
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
    # Render (переменную RENDER он задаёт сам) стирает диск при каждом засыпании и перезапуске: с SQLite все подписки
    # пропадают примерно через сутки. Лучше не запуститься и показать ошибку в деплое, чем молча терять ключи покупателей.
    if os.environ.get('RENDER') and not DATABASE_URL:
        print('ОШИБКА: на Render не задан DATABASE_URL (строка подключения PostgreSQL из Neon). '
              'Без него база временная и подписки стираются при перезапуске. Добавьте DATABASE_URL в Environment.')
        sys.exit(1)
    init_db()
    threading.Thread(target=setup_webhook, daemon=True).start()
    if TG_TOKEN:
        threading.Thread(target=reminder_loop, daemon=True).start()
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
