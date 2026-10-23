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
import urllib.request

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
    '/stats — статистика и выручка\n'
    '/orders — заказы, ждущие проверки\n'
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
        lines = ['<b>Статистика</b>', 'База: ' + ('PostgreSQL' if DATABASE_URL else '⚠️ SQLite (временная, стирается при перезапуске!)'),
                 f'Ключей: {total}, активировано: {used}', f'Активных подписок: {active}', f'Пробных часов: {trials}', '',
                 f'Продано через бота: {sold} на {revenue} ₽', f'За 30 дней: {month_rev} ₽', f'Ждут проверки: {waiting}', '']
        for plan, cnt, u in by_plan:
            lines.append(f'{PLANS.get(plan, (0, plan))[1]}: {u or 0} из {cnt}')
        return '\n'.join(lines)
    if cmd == '/orders':
        with db() as conn:
            rows = q(conn, "SELECT id, plan, amount, username, user_id, created_at FROM orders WHERE status = 'review' ORDER BY id").fetchall()
        if not rows:
            return 'Заказов на проверке нет'
        return '<b>Ждут проверки</b>\n\n' + '\n'.join(
            f'#{oid} · {PLANS[plan][1]} · {amount} ₽ · {buyer_label(uname, uid)} · {fmt_time(ts)}' for oid, plan, amount, uname, uid, ts in rows)
    return 'Неизвестная команда. /help — список команд'


# ===== Магазин: оплата переводом на карту с ручным подтверждением (временно, до ЮKassa) =====
def is_admin(user):
    uname = (user.get('username') or '').lower()
    return user.get('id') in ADMIN_IDS or bool(uname and uname in ADMIN_USERNAMES)


def buyer_label(username, user_id):
    return f'@{html.escape(username)}' if username else f'id {user_id}'


def admin_chats():
    with db() as conn:
        seen = {r[0] for r in q(conn, 'SELECT uid FROM bot_admins').fetchall()}
    return ADMIN_IDS | seen


def send(chat_id, text, buttons=None):
    params = dict(chat_id=chat_id, text=text, parse_mode='HTML', disable_web_page_preview=True)
    if buttons:
        params['reply_markup'] = {'inline_keyboard': buttons}
    return tg('sendMessage', **params)


def show(chat_id, message_id, text, buttons=None):
    """Меняет экран в том же сообщении (кнопки меню), а если не вышло - присылает новое."""
    if message_id:
        params = dict(chat_id=chat_id, message_id=message_id, text=text, parse_mode='HTML', disable_web_page_preview=True,
                      reply_markup={'inline_keyboard': buttons or []})
        res = tg('editMessageText', **params)
        if res and res.get('ok'):
            return
    send(chat_id, text, buttons)


def menu_screen():
    plans = list(PRICES)
    rows = [[{'text': f'{PLANS[p][1]} · {PRICES[p]} ₽', 'callback_data': f'buy:{p}'} for p in plans[i:i + 2]]
            for i in range(0, len(plans), 2)]
    rows.append([{'text': '🧾 Мои покупки', 'callback_data': 'my'}, {'text': '💬 Поддержка', 'callback_data': 'support'}])
    text = ('<b>SugarClick</b> — подписка на автокликер для «Номерка»\n\n'
            'Выберите тариф. После оплаты бот пришлёт ключ <code>SC-XXXX-XXXX-XXXX</code>, '
            'его нужно ввести в программе в окне активации.')
    return text, rows


def order_screen(oid, plan, amount):
    lines = [f'<b>Заказ #{oid} · {PLANS[plan][1]}</b>', '', f'Переведите <b>{amount} ₽</b> на карту:',
             f'<code>{html.escape(PAY_CARD)}</code>']
    if PAY_NAME:
        lines.append(f'Получатель: {html.escape(PAY_NAME)}')
    if PAY_BANK:
        lines.append(f'Банк: {html.escape(PAY_BANK)}')
    lines += ['', f'Переведите ровно {amount} ₽ одним платежом. После перевода нажмите «Я оплатил» и пришлите скриншот чека.']
    buttons = [[{'text': '✅ Я оплатил', 'callback_data': f'paid:{oid}'}],
               [{'text': '← Отменить заказ', 'callback_data': f'cancel:{oid}'}]]
    return '\n'.join(lines), buttons


def proof_screen(oid):
    text = (f'<b>Заказ #{oid}</b>\n\nПришлите сюда скриншот чека о переводе (картинкой или файлом). '
            'Администратор проверит оплату, и ключ придёт в этот чат.')
    return text, [[{'text': '← Отменить заказ', 'callback_data': f'cancel:{oid}'}]]


def support_screen():
    text = f'По вопросам оплаты и работы программы пишите @{html.escape(SUPPORT_USERNAME)}'
    return text, [[{'text': '← Назад', 'callback_data': 'menu'}]]


def purchases_screen(uid):
    with db() as conn:
        rows = q(conn, "SELECT id, plan, amount, status, paid_at, created_at, key FROM orders WHERE user_id = ? "
                       "AND status IN ('paid', 'review') ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    if not rows:
        text = 'Покупок пока нет'
    else:
        items = []
        for oid, plan, amount, status, paid_at, created_at, key in rows:
            if status == 'paid':
                items.append(f'#{oid} · {PLANS[plan][1]} · {amount} ₽ · {fmt_time(paid_at)}\n<code>{key}</code>')
            else:
                items.append(f'#{oid} · {PLANS[plan][1]} · {amount} ₽ · ⏳ на проверке')
        text = '<b>Мои покупки</b>\n\n' + '\n\n'.join(items)
    return text, [[{'text': '← Назад', 'callback_data': 'menu'}]]


def notify_admins(text):
    for cid in admin_chats():
        send(cid, text)


def update_admin_msgs(admin_msgs, caption):
    for cid, mid in json.loads(admin_msgs or '[]'):
        tg('editMessageCaption', chat_id=cid, message_id=mid, caption=caption, parse_mode='HTML')


def review_caption(oid, plan, amount, username, user_id, first_name):
    who = buyer_label(username, user_id)
    if first_name:
        who += f' ({html.escape(first_name)})'
    return (f'💳 <b>Заказ #{oid}</b> · {PLANS[plan][1]} · <b>{amount} ₽</b>\n'
            f'Покупатель: {who}, id <code>{user_id}</code>\n'
            f'Проверьте, что перевод пришёл, и подтвердите.')


def handle_proof(msg, user, chat_id):
    """Покупатель прислал чек: отправляем его всем админам с кнопками подтверждения."""
    uid = user.get('id')
    with db() as conn:
        row = q(conn, "SELECT id, plan, amount FROM orders WHERE user_id = ? AND status IN ('new', 'proof') ORDER BY id DESC LIMIT 1",
                (uid,)).fetchone()
        if not row:
            return False
        oid, plan, amount = row
        q(conn, "UPDATE orders SET status = 'review' WHERE id = ?", (oid,))
    caption = review_caption(oid, plan, amount, user.get('username'), uid, user.get('first_name'))
    buttons = {'inline_keyboard': [[{'text': '✅ Подтвердить', 'callback_data': f'ok:{oid}'},
                                    {'text': '❌ Отклонить', 'callback_data': f'no:{oid}'}]]}
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


def decide_order(oid, approve, admin_user):
    """Админ подтвердил или отклонил оплату. Возвращает текст всплывающего ответа на кнопку."""
    now = int(time.time())
    admin_name = '@' + admin_user['username'] if admin_user.get('username') else str(admin_user.get('id'))
    with db() as conn:
        row = q(conn, 'SELECT plan, amount, user_id, username, first_name, status, admin_msgs, key, decided_by FROM orders WHERE id = ?',
                (oid,)).fetchone()
        if not row:
            return 'Заказ не найден'
        plan, amount, user_id, username, first_name, status, admin_msgs, key, decided_by = row
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
    caption = review_caption(oid, plan, amount, username, user_id, first_name).rsplit('\n', 1)[0]
    if approve:
        send(user_id, f'✅ <b>Оплата подтверждена</b>\n\nЗаказ #{oid} · {PLANS[plan][1]}\nВаш ключ:\n<code>{key}</code>\n\n'
                      'Откройте SugarClick и введите ключ в окне активации. Время подписки пойдёт с момента активации.',
             [[{'text': '🧾 Мои покупки', 'callback_data': 'my'}]])
        update_admin_msgs(admin_msgs, caption + f'\n\n✅ Подтвердил {html.escape(admin_name)}, ключ <code>{key}</code>')
        return 'Ключ отправлен покупателю'
    send(user_id, f'❌ Оплата по заказу #{oid} не подтверждена.\n\n'
                  f'Если вы перевели деньги, напишите @{html.escape(SUPPORT_USERNAME)} и приложите чек.',
         [[{'text': '← В меню', 'callback_data': 'menu'}]])
    update_admin_msgs(admin_msgs, caption + f'\n\n❌ Отклонил {html.escape(admin_name)}')
    return 'Заказ отклонён'


def handle_callback(cb):
    user = cb.get('from') or {}
    uid = user.get('id')
    msg = cb.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id') or uid
    mid = msg.get('message_id')
    data = cb.get('data') or ''
    action, _, arg = data.partition(':')
    toast = None
    if action in ('ok', 'no'):
        toast = decide_order(int(arg), action == 'ok', user) if is_admin(user) else 'Только для админов'
    elif action == 'menu':
        show(chat_id, mid, *menu_screen())
    elif action == 'my':
        show(chat_id, mid, *purchases_screen(uid))
    elif action == 'support':
        show(chat_id, mid, *support_screen())
    elif action == 'buy' and arg in PRICES:
        if not PAY_CARD:
            toast = 'Оплата временно недоступна, напишите в поддержку'
        else:
            now = int(time.time())
            with db() as conn:
                # у покупателя один открытый заказ: старые неоплаченные закрываем
                q(conn, "UPDATE orders SET status = 'cancelled' WHERE user_id = ? AND status IN ('new', 'proof')", (uid,))
                oid = q(conn, "INSERT INTO orders (user_id, username, first_name, plan, amount, status, created_at) "
                              "VALUES (?, ?, ?, ?, ?, 'new', ?) RETURNING id",
                        (uid, user.get('username'), user.get('first_name'), arg, PRICES[arg], now)).fetchone()[0]
            show(chat_id, mid, *order_screen(oid, arg, PRICES[arg]))
    elif action in ('paid', 'cancel'):
        oid = int(arg)
        new_status = 'proof' if action == 'paid' else 'cancelled'
        with db() as conn:
            cur = q(conn, "UPDATE orders SET status = ? WHERE id = ? AND user_id = ? AND status IN ('new', 'proof')",
                    (new_status, oid, uid))
            ok = cur.rowcount == 1
        if not ok:
            toast = 'Этот заказ уже закрыт'
        elif action == 'paid':
            show(chat_id, mid, *proof_screen(oid))
        else:
            show(chat_id, mid, *menu_screen())
            toast = 'Заказ отменён'
    params = {'callback_query_id': cb.get('id')}
    if toast:
        params['text'] = toast
    tg('answerCallbackQuery', **params)


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
    cmd = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
    if cmd == '/id':
        return send(chat_id, f'Ваш Telegram ID: <code>{uid}</code>')
    if chat.get('type') != 'private':
        return
    if msg.get('photo') or msg.get('document'):
        if not handle_proof(msg, user, chat_id) and not admin:
            send(chat_id, 'Сначала выберите тариф, затем пришлите чек.', menu_screen()[1])
        return
    if admin and cmd and cmd != '/shop':
        try:
            reply = bot_command(text, int(time.time()))
        except Exception as e:
            reply = f'Ошибка: {html.escape(str(e))}'
        return send(chat_id, reply)
    with db() as conn:
        waiting = q(conn, "SELECT id FROM orders WHERE user_id = ? AND status = 'proof' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if waiting and not cmd:
        return send(chat_id, *proof_screen(waiting[0]))
    if admin and not cmd:
        return
    send(chat_id, *menu_screen())


def setup_webhook():
    base = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
    if not (TG_TOKEN and base):
        return
    res = tg('setWebhook', url=base + '/tg', secret_token=TG_SECRET, allowed_updates=['message', 'callback_query'])
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
