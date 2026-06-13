from flask import Flask, render_template, request, redirect, url_for, make_response
from datetime import datetime, timedelta
import json
import os
import hashlib
import uuid  # ユーザー識別用のランダムな値を生成するライブラリ

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

def get_daily_user_id(user_session_token):
    """ユーザー固有のトークンと日付から、毎日変わる8文字のIDを生成"""
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{user_session_token}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

@app.route('/')
def index():
    data = load_data()
    
    # ユーザーを識別するためのCookie（トークン）がなければ発行する
    user_token = request.cookies.get('user_bbs_token')
    response = make_response(render_template('index.html', threads=data['threads']))
    
    if not user_token:
        # ランダムな一意の文字列を生成
        user_token = str(uuid.uuid4())
        # Cookieに保存（有効期限はとりあえず1年など長く設定し、IDの計算側で毎日変える）
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
        
    return response

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

    # ユーザーのトークンを取得（なければ暫定の値を割り当て）
    user_token = request.cookies.get('user_bbs_token') or "guest"

    if request.method == 'POST':
        author = request.form.get('author') or "名無しさん"
        content = request.form.get('content')
        
        # クッキーのトークンを元に、今日のIDを計算（これで2秒連投しても固定されます）
        user_id = get_daily_user_id(user_token)
        
        if content:
            new_reply = {
                'id': len(thread['replies']) + 1,
                'author': author,
                'content': content,
                'user_id': user_id,
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            thread['replies'].append(new_reply)
            save_data(data)
            
        return redirect(url_for('thread_view', thread_id=thread_id))
        
    # GET時は通常どおり画面を表示
    response = make_response(render_template('thread.html', thread=thread))
    # 万が一トップページを経由せずに直リンクで来てもCookieを付与できるようにする
    if not request.cookies.get('user_bbs_token'):
        user_token = str(uuid.uuid4())
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
        
    return response

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

