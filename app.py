from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import sqlite3
import os

DB_FILE = 'licenses.db'

def init_db():
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

class SimpleHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/activate':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                key = data.get('key', '').strip()
                hwid = data.get('hwid', '').strip()
            except:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Invalid JSON"}).encode('utf-8'))
                return

            if not key or not hwid:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Missing key or hwid"}).encode('utf-8'))
                return

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT hwid, is_used FROM keys WHERE key_value = ?', (key,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Неверный ключ активации!"}).encode('utf-8'))
                return

            db_hwid, is_used = row

            if is_used == 1:
                if db_hwid == hwid:
                    conn.close()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "message": "Активировано повторно"}).encode('utf-8'))
                    return
                else:
                    conn.close()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Этот ключ уже активирован на другом устройстве!"}).encode('utf-8'))
                    return

            cursor.execute('UPDATE keys SET hwid = ?, is_used = 1 WHERE key_value = ?', (hwid, key))
            conn.commit()
            conn.close()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "Успешная активация!"}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def run():
    port = int(os.environ.get('PORT', 5000))
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, SimpleHandler)
    print(f'Server running on port {port}...')
    httpd.serve_forever()

if __name__ == '__main__':
    run()
