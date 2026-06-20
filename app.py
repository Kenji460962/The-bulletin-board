from flask import Flask, render_template, request, redirect, url_for, make_response
from datetime import datetime
import json
import os
import hashlib
import uuid
# Cloudinaryのライブラリを読み込み
import cloudinary
import cloudinary.uploader
import time

app = Flask(__name__)

# 🟢 無料プランのスリープを防ぎつつ、エラーログだけを完全に消し去る設定
@app.before_request
def response_to_uptimerobot():
    if request.method == 'HEAD':
        return make_response('', 200) # ←「生きてるよ！」と最速で返事をする


# Cloudinaryの設定
# Renderの環境変数
cloudinary.config(
    cloudinary_url = os.environ.get('cloudinary://413154997929334:1MWGTCiDlVZawKJWIm1aNpq_dhM@dpqh2ssnh'),
    secure = True
)

DATA_FILE = 'bbs_data.json'
ADMIN_PASSWORD = "setokoji114514"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"threads": [], "admin_message": "ここに管理者の一言が表示されます。", "banned_ips": []}

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

#IPアドレスを元に毎日変わるIDを生成
def get_daily_user_id(ip_address):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

#アクセスしてきたユーザーの実際のIPアドレスを取得する
def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

#BANされたIPかどうかを判定する
def is_banned_ip(ip):
    data = load_data()
    if "banned_ips" not in data:
        data["banned_ips"] = []
        save_data(data)
    return ip in data["banned_ips"]

def check_is_admin_cookie(request):
    admin_cookie_flag = request.cookies.get('is_bbs_admin')
    return admin_cookie_flag == "true"

ACTIVE_USERS = {}

def update_and_get_user_counts(current_token, location):
    now = time.time()
    
    # 1. アクセスしてきたユーザーの位置と時間を更新
    if current_token:
        ACTIVE_USERS[current_token] = {
            "location": location,
            "last_time": now
        }
        
    # 2. 5分（300秒）以上アクセスのない古いデータを削除
    expired_tokens = [token for token, info in ACTIVE_USERS.items() if now - info["last_time"] > 300]
    for token in expired_tokens:
        del ACTIVE_USERS[token]
        
    # 3. 指定された場所（ロビーまたは各スレッド）にいる人数をカウント
    count = sum(1 for info in ACTIVE_USERS.values() if info["location"] == location)
    return count
    


# 🟢 【修正】methodsに 'GET' と 'HEAD' の両方を公式に許可するよう指定します
@app.route('/', methods=['GET', 'HEAD'])
def index():
    # ロビー閲覧時もBANされているIPからのアクセスを拒否
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    # 🟢 【追加】ロボットからのHEADリクエストは、ここで最優先で安全に200（正常終了）を返して追い返します
    if request.method == 'HEAD':
        return make_response('', 200)

    data = load_data()
    if "admin_message" not in data:
        data["admin_message"] = "ここに管理者の一言が表示されます。"
        save_data(data)

    user_token = request.cookies.get('user_bbs_token')
    
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    # ロビーの閲覧者数を計算
    active_count = update_and_get_user_counts(user_token, "lobby")

    is_admin_user = check_is_admin_cookie(request)
    
    # 通常の人間用（GET）の画面作成
    response = make_response(render_template(
        'index.html', 
        threads=data['threads'],
        admin_message=data['admin_message'], 
        is_admin_user=is_admin_user,
        active_count=active_count
    ))
    
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
        
    return response


# 🟢 【追加】スレ主をBANする管理者用ルート
@app.route('/thread/<int:thread_id>/ban_owner', methods=['POST'])
def ban_thread_owner(thread_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    
    # スレッドが存在し、かつ作成者のIPアドレスが記録されている場合
    if thread and 'ip_address' in thread:
        owner_ip = thread['ip_address']
        
        if "banned_ips" not in data:
            data["banned_ips"] = []
        if owner_ip not in data['banned_ips']:
            data['banned_ips'].append(owner_ip)
            
        # 💡 荒らし対策として、該当スレッドのタイトルを「BAN済」に変更し、レスも全削除
        thread['title'] = "【このスレッドは管理員によってBANされました】"
        thread['replies'] = [{
            'id': 1,
            'author': "あぼーん",
            'content': "このスレッドの作成者はBANされました。",
            'user_id': "???",
            'is_admin': False,
            'image_url': "",
            'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }]
        save_data(data)
        
    return redirect(url_for('index')) # BAN後はロビーに戻す


    # 🟢 【ロビーの閲覧者数を計算】
    active_count = update_and_get_user_counts(user_token, "lobby")

    is_admin_user = check_is_admin_cookie(request)
    
    # 🟢 【HTMLに人数 active_count を送る】
    response = make_response(render_template(
        'index.html', 
        threads=data['threads'],
        admin_message=data['admin_message'], 
        is_admin_user=is_admin_user,
        active_count=active_count
    ))
    
    if is_new_user:
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
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    title = request.form.get('title')
    if not title:
        return {"error": "タイトルが必要です"}, 400
    data = load_data()
    new_thread = {
        'id': len(data['threads']) + 1,
        'title': title,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ip_address': client_ip,  # 🟢 【追加】スレ主のIPを保存
        'replies': []
    }
    data['threads'].insert(0, new_thread)
    save_data(data)
    return {"success": True, "thread": new_thread}


@app.route('/thread/<int:thread_id>', methods=['GET', 'POST'])
def thread_view(thread_id):
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    if not thread:
        return "スレッドが見つかりません", 404

    is_admin_user = check_is_admin_cookie(request)

    if request.method == 'POST':
        author_input = request.form.get('author') or "名無しさん"
        content = request.form.get('content') or ""
        user_id = get_daily_user_id(client_ip)
        
        is_admin = False
        if "#" in author_input:
            name_part, pass_part = author_input.split("#", 1)
            if pass_part == ADMIN_PASSWORD:
                author_input = (name_part or "管理人") + " ★"
                is_admin = True
                user_id = "????"
            else:
                author_input = name_part or "名無しさん"

        image_url = ""
        if 'image' in request.files:
            file = request.files['image']
            if file and file.filename != '':
                try:
                    upload_result = cloudinary.uploader.upload(file)
                    image_url = upload_result.get('secure_url')
                except Exception as e:
                    print(f"Cloudinary Upload Error: {e}")

        if content.strip() or image_url:
            new_reply = {
                'id': len(thread['replies']) + 1,
                'author': author_input,
                'content': content,
                'user_id': user_id,
                'is_admin': is_admin,
                'image_url': image_url, 
                'ip_address': client_ip, 
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            thread['replies'].append(new_reply)
            save_data(data)
            
        response = redirect(url_for('thread_view', thread_id=thread_id))
        if is_admin:
            response.set_cookie('is_bbs_admin', 'true', max_age=60*60*24)
        return response
        
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    # 🟢 【スレッド専用の人数をカウントしてHTMLに送る】
    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)

    response = make_response(render_template('thread.html', thread=thread, is_admin_user=is_admin_user, active_count=active_count))
    
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/thread/<int:thread_id>/delete_thread', methods=['POST'])
def delete_thread(thread_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    data = load_data()
    data['threads'] = [t for t in data['threads'] if t['id'] != thread_id]
    save_data(data)
    return redirect(url_for('index'))

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
            reply['image_url'] = ""
            save_data(data)
    return redirect(url_for('thread_view', thread_id=thread_id))
    
@app.route('/thread/<int:thread_id>/ban/<int:reply_id>', methods=['POST'])
def ban_user(thread_id, reply_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    if thread:
        reply = next((r for r in thread['replies'] if r['id'] == reply_id), None)
        if reply and 'ip_address' in reply:
            if "banned_ips" not in data:
                data["banned_ips"] = []
            if reply['ip_address'] not in data['banned_ips']:
                data['banned_ips'].append(reply['ip_address'])
            reply['author'] = "あぼーん"
            reply['content'] = "この書き込みは管理員によってBANされました。"
            reply['user_id'] = "???"
            reply['is_admin'] = False
            reply['image_url'] = ""
            save_data(data)
    return redirect(url_for('thread_view', thread_id=thread_id))

@app.route('/api/thread/<int:thread_id>/updates')
def thread_updates(thread_id):
    last_id = request.args.get('last_id', type=int, default=0)
    
    # 🟢 【3秒おきのJavaScriptアクセス時にも生存確認として人数を更新】
    user_token = request.cookies.get('user_bbs_token')
    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)
    
    data = load_data()
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    if not thread:
        return {"replies": []}, 404
    new_replies = [r for r in thread['replies'] if r['id'] > last_id]
    is_admin_user = check_is_admin_cookie(request)
    
    # 🟢 【JavaScript側に最新の人数 active_count を辞書に含めて返す】
    return {
        "replies": new_replies, 
        "is_admin_user": is_admin_user, 
        "active_count": active_count
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

