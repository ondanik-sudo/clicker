from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)
DB_FILE = 'licenses.db'

def init_db():
    """Создает базу данных и список наших 15 ключей, если их еще нет"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value TEXT UNIQUE,
            hwid TEXT,
            is_used INTEGER DEFAULT 0
        )
    ''')
    
    # Наши 15 уникальных ключей
    valid_keys = [
        "SUGAR-7771-AAAA", "SUGAR-7772-BBBB", "SUGAR-7773-CCCC", 
        "SUGAR-7774-DDDD", "SUGAR-7775-EEEE", "SUGAR-7776-FFFF", 
        "SUGAR-7777-GGGG", "SUGAR-7778-HHHH", "SUGAR-7779-IIII", 
        "SUGAR-7770-JJJJ", "SUGAR-8881-KKKK", "SUGAR-8882-LLLL", 
        "SUGAR-8883-MMMM", "SUGAR-8884-NNNN", "SUGAR-8885-OOOO"
    ]
    
    for k in valid_keys:
        cursor.execute('INSERT OR IGNORE INTO keys (key_value) VALUES (?)', (k,))
        
    conn.commit()
    conn.close()

init_db()

@app.route('/activate', methods=['POST'])
def activate():
    data = request.json
    key = data.get('key', '').strip()
    hwid = data.get('hwid', '').strip()

    if not key or not hwid:
        return jsonify({"status": "error", "message": "Missing key or hwid"}), 400

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Проверяем, существует ли вообще такой ключ
    cursor.execute('SELECT hwid, is_used FROM keys WHERE key_value = ?', (key,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return jsonify({"status": "error", "message": "Неверный ключ активации!"})

    db_hwid, is_used = row

    # Если ключ уже использован
    if is_used == 1:
        if db_hwid == hwid:
            # Если тот же самый ПК пробует войти повторно — разрешаем
            conn.close()
            return jsonify({"status": "success", "message": "Активировано повторно"})
        else:
            # Если чужой ПК пытается ввести этот же ключ
            conn.close()
            return jsonify({"status": "error", "message": "Этот ключ уже активирован на другом устройстве!"})

    # Если ключ свободен, привязываем его к текущему HWID
    cursor.execute('UPDATE keys SET hwid = ?, is_used = 1 WHERE key_value = ?', (hwid, key))
    conn.commit()
    conn.close()

    return jsonify({"status": "success", "message": "Успешная активация!"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
