from flask import Flask, render_template, request, redirect, url_for, make_response, jsonify
from datetime import datetime
import json
import os
import hashlib
import uuid

app = Flask(__name__)

DATA_FILE = 'bbs_data.json'
ADMIN_PASSWORD = "kenji1228s00460962"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"threads": [], "admin_message": "ここに管理者の一言が表示されます。"}

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_daily_user_id(user_session_token):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{user_session_token}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

def check_is_admin_cookie(request):
    """現在のアクセス者が管理者かどうかをCookieで判定する"""
    user_token = request.cookies.get('user_bbs_token') or "guest"
    # 管理者のID（????）の元になるトークンであるか、または管理者用フラグのCookieがあるか
    admin_cookie_flag = request.cookies.get('is_bbs_admin')
    return admin_cookie_flag == "true"

@app.route('/')
def index():
    data = load_data()
    if "admin_message" not in data:
        data["admin_message"] = "ここに管理者の一言が表示されます。"
        save_data(data)

    user_token = request.cookies.get('user_bbs_token')
    response = make_response(render_template('index.html', threads=data['threads'], admin_message=data['admin_message']))
    if not user_token:
        user_token = str(uuid.uuid4())
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/update_admin_message', methods=['POST'])
def update_admin_message():
    password = request.form.get('admin_password')
    message = request.form.get('message')
    if password == ADMIN_PASSWORD and message:
        data = load_data()
        data['admin_message'] = message
        save_data(data)
    return redirect(url_for('index'))

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

    user_token = request.cookies.get('user_bbs_token') or "guest"
    is_admin_user = check_is_admin_cookie(request)

    if request.method == 'POST':
        author_input = request.form.get('author') or "名無しさん"
        content = request.form.get('content')
        user_id = get_daily_user_id(user_token)
        
        is_admin = False
        if "#" in author_input:
            name_part, pass_part = author_input.split("#", 1)
            if pass_part == ADMIN_PASSWORD:
                author_input = (name_part or "管理人") + " ★"
                is_admin = True
                user_id = "????"
            else:
                author_input = name_part or "名無しさん"

        if content:
            new_reply = {
                'id': len(thread['replies']) + 1,
                'author': author_input,
                'content': content,
                'user_id': user_id,
                'is_admin': is_admin,
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            thread['replies'].append(new_reply)
            save_data(data)
            
        response = redirect(url_for('thread_view', thread_id=thread_id))
        if is_admin:
            # 管理者として書き込みに成功したら、ブラウザに管理者クッキーを付与
            response.set_cookie('is_bbs_admin', 'true', max_age=60*60*24)
        return response
        
    # 現在の閲覧者が管理者かどうかも一緒にテンプレートに送る
    response = make_response(render_template('thread.html', thread=thread, is_admin_user=is_admin_user))
    if not request.cookies.get('user_bbs_token'):
        user_token = str(uuid.uuid4())
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/thread/<int:thread_id>/delete/<int:reply_id>', methods=['POST'])
def delete_reply(thread_id, reply_id):
    """管理者専用：レスを削除（あぼーん化）する"""
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
        
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    if thread:
        reply = next((r for r in thread['replies'] if r['id'] == reply_id), None)
        if reply:
            # 完全にデータを消すと番号がズレるため、2chでおなじみの「あぼーん」に書き換える
            reply['author'] = "あぼーん"
            reply['content'] = "この書き込みは管理員によって削除されました。"
            reply['user_id'] = "???"
            reply['is_admin'] = False
            save_data(data)
            
    return redirect(url_for('thread_view', thread_id=thread_id))

@app.route('/api/thread/<int:thread_id>/updates')
def thread_updates(thread_id):
    last_id = request.args.get('last_id', type=int, default=0)
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    if not thread:
        return {"replies": []}, 404
    new_replies = [r for r in thread['replies'] if r['id'] > last_id]
    
    # 自動更新用APIにも現在の閲覧者が管理者かどうかの情報を混ぜる
    is_admin_user = check_is_admin_cookie(request)
    return {"replies": new_replies, "is_admin_user": is_admin_user}

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False') == 'True'
    app.run(debug=debug_mode)
