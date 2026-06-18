=#from flask import Flask, render_template, request, redirect, url_for, make_response
from datetime import datetime
import json
import os
import hashlib
import uuid
# Cloudinaryのライブラリを読み込み
import cloudinary
import cloudinary.uploader

app = Flask(__name__)

# Cloudinaryの設定（Renderの環境変数から自動で読み込みます）
# Renderの環境変数（CLOUDINARY_URL）を使って一発で安全に接続します
# ★ secure=True を足すことで、通信の暗号化エラーによるフリーズを防ぎます
cloudinary.config(
    cloudinary_url = os.environ.get('cloudinary://413154997929334:1MWGTCiDlVZawKJWIm1aNpq_dhM@dpqh2ssnh'),
    secure = True
)

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
        return {"error": "タイトルが必要です"}, 400
    data = load_data()
    new_thread = {
        'id': len(data['threads']) + 1,
        'title': title,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'replies': []
    }
    #一番上に追加されるようにする
    data['threads'].insert(0, new_thread)
    save_data(data)
    
    # 画面をロビーに勝手に戻さず、成功したデータだけを返します
    return {"success": True, "thread": new_thread}



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
        content = request.form.get('content') or ""
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

        # 【追加】画像ファイルの取得処理
        image_url = ""
        if 'image' in request.files:
            file = request.files['image']
            # ファイルが存在し、ファイル名が空でない場合のみCloudinaryへアップロード
            if file and file.filename != '':
                try:
                    upload_result = cloudinary.uploader.upload(file)
                    image_url = upload_result.get('secure_url') # 画像のURLを取得
                except Exception as e:
                    print(f"Cloudinary Upload Error: {e}")

        # 本文が空ではない、または画像がアップロードされている場合に投稿を許可
        if content.strip() or image_url:
            new_reply = {
                'id': len(thread['replies']) + 1,
                'author': author_input,
                'content': content,
                'user_id': user_id,
                'is_admin': is_admin,
                'image_url': image_url, # 【追加】画像のURLを保存
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            thread['replies'].append(new_reply)
            save_data(data)
            
        response = redirect(url_for('thread_view', thread_id=thread_id))
        if is_admin:
            response.set_cookie('is_bbs_admin', 'true', max_age=60*60*24)
        return response
        
    response = make_response(render_template('thread.html', thread=thread, is_admin_user=is_admin_user))
    if not request.cookies.get('user_bbs_token'):
        user_token = str(uuid.uuid4())
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/thread/<int:thread_id>/delete/<int:reply_id>', methods=['POST'])
def delete_reply(thread_id, reply_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    if thread:
        reply = next((r for r in thread['replies'] if r['id'] == reply_id), None)
        if reply:
            reply['author'] = "あぼーん"
            reply['content'] = "この書き込みは管理員によって削除されました。"
            reply['user_id'] = "???"
            reply['is_admin'] = False
            reply['image_url'] = "" # 【追加】画像も削除
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
    is_admin_user = check_is_admin_cookie(request)
    return {"replies": new_replies, "is_admin_user": is_admin_user}

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
