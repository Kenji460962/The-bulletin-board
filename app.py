from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import json
import os
import hashlib  # ID生成用のライブラリ

app = Flask(__name__)

DATA_FILE = 'bbs_data.json'

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"threads": []}

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_user_id(ip_address):
    """IPアドレスと今日の日付から、2ch風の毎日変わる8文字のIDを生成"""
    today_str = datetime.now().strftime('%Y-%m-%d')
    # IPアドレスと日付をガッチャンコして暗号化（ハッシュ化）
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    # 先頭の8文字を切り取ってIDにする
    return hashed[:8]

@app.route('/')
def index():
    data = load_data()
    return render_template('index.html', threads=data['threads'])

@app.route('/create_thread', methods=['POST'])
def create_thread():
    title = request.form.get('title')
    if not title:
        return redirect(url_for('index'))
        
    data = load_data()
    new_thread = {
        'id': len(data['threads']) + 1,
        'title': title,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'replies': []
    }
    data['threads'].append(new_thread)
    save_data(data)
    return redirect(url_for('index'))

@app.route('/thread/<int:thread_id>', methods=['GET', 'POST'])
def thread_view(thread_id):
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    
    if not thread:
        return "スレッドが見つかりません", 404

    if request.method == 'POST':
        author = request.form.get('author') or "名無しさん"
        content = request.form.get('content')
        
        # 投稿者のIPアドレスを取得（Render環境などでも考慮）
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_id = generate_user_id(ip_address)
        
        if content:
            new_reply = {
                'id': len(thread['replies']) + 1,
                'author': author,
                'content': content,
                'user_id': user_id,  # IDを保存
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            thread['replies'].append(new_reply)
            save_data(data)
            
        return redirect(url_for('thread_view', thread_id=thread_id))
        
    return render_template('thread.html', thread=thread)

@app.route('/api/thread/<int:thread_id>/updates')
def thread_updates(thread_id):
    last_id = request.args.get('last_id', type=int, default=0)
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    
    if not thread:
        return {"replies": []}, 404
        
    new_replies = [r for r in thread['replies'] if r['id'] > last_id]
    return {"replies": new_replies}

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False') == 'True'
    app.run(debug=debug_mode)
