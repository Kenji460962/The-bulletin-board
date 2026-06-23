from flask import Flask, render_template, request, redirect, url_for, make_response
from datetime import datetime
import json
import os
import hashlib
import uuid
import time
# Cloudinaryのライブラリを読み込み
import cloudinary
import cloudinary.uploader
# 🟢 【追加】Supabaseを使うためのライブラリを読み込み
from supabase import create_client, Client

app = Flask(__name__)

# 無料プランのスリープを防ぎつつ、エラーログだけを完全に消し去る設定
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

# 🟢 【重要】あなたのSupabase情報を直接ここに埋め込みました
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://mpzjidhuovorzvjhukmy.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1wemppZGh1b3Zvcnp2amh1a215Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIwMDYzMjIsImV4cCI6MjA5NzU4MjMyMn0.Q11dCsMYX0LakWydaVD6EIKKJD2Wbv7qHV0GuAyxEeo')

# Supabaseに接続するロボットを起動
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DATA_FILE = 'bbs_data.json'
ADMIN_PASSWORD = "setokoji114514"

# 🟢 【セキュリティ強化】ユーザーごとの最後の書き込み時間を記録する場所
LAST_POST_TIMES = {}


# 🟢 【全自動お引越し装置】
# Renderサーバー内にある古いbbs_data.jsonを見つけて、起動時に自動でSupabaseへ全移行します
# 🟢 不要になったお引越しシステムの中身を完全に消去しました
def auto_migrate_from_json():
    pass



# IPアドレスを元に毎日変わるIDを生成
def get_daily_user_id(ip_address):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

# アクセスしてきたユーザーの実際のIPアドレスを取得する
def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

# BANされたIPかどうかを判定する
def is_banned_ip(ip):
    try:
        response = supabase.table('banned_ips').select('ip').eq('ip', ip).execute()
        return len(response.data) > 0
    except:
        return False

def check_is_admin_cookie(request):
    admin_cookie_flag = request.cookies.get('is_bbs_admin')
    return admin_cookie_flag == "true"

ACTIVE_USERS = {}

def update_and_get_user_counts(current_token, location):
    now = time.time()
    if current_token:
        ACTIVE_USERS[current_token] = {
            "location": location,
            "last_time": now
        }
    expired_tokens = [token for token, info in ACTIVE_USERS.items() if now - info["last_time"] > 300]
    for token in expired_tokens:
        del ACTIVE_USERS[token]
    count = sum(1 for info in ACTIVE_USERS.values() if info["location"] == location)
    return count

# 🟢 methodsに 'GET' と 'HEAD' の両方を公式に許可するよう指定します
@app.route('/', methods=['GET', 'HEAD'])
def index():
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    if request.method == 'HEAD':
        return make_response('', 200)

    try:
        # Supabaseからスレッド一覧（最新順）を取得
        threads_response = supabase.table('threads').select('*').order('id', desc=True).execute()
        threads = threads_response.data

        # 各スレッドにレスが何件ついているかを数える（エラーが起きても無視して進むように個別に囲む）
        for t in threads:
            try:
                replies_res = supabase.table('replies').select('id').eq('thread_id', t['id']).execute()
                t['replies'] = replies_res.data if replies_res.data else []
            except Exception as re:
                print(f"レス件数取得エラー (スレID {t['id']}): {re}")
                t['replies'] = [] # エラーが起きても空配列を入れてスレ自体は残す

        # Supabaseから管理者の一言を取得
        try:
            admin_res = supabase.table('admin_messages').select('message').eq('id', 1).execute()
            # リストから最初の1件を取得する安全な書き方に修正
            admin_message = admin_res.data[0]['message'] if admin_res.data else "ここに管理者の一言が表示されます。"
        except Exception as ae:
            print(f"管理者メッセージ取得エラー: {ae}")
            admin_message = "管理者の一言の取得に失敗しました。"

    except Exception as e:
        print(f"スレッド一覧取得エラー: {e}")
        threads = []



    user_token = request.cookies.get('user_bbs_token')
    
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    active_count = update_and_get_user_counts(user_token, "lobby")
    is_admin_user = check_is_admin_cookie(request)
    
    response = make_response(render_template(
        'index.html', 
        threads=threads, 
        admin_message=admin_message, 
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
        try:
            supabase.table('admin_messages').update({'message': message}).eq('id', 1).execute()
        except Exception as e:
            print(f"メッセージ更新エラー: {e}")
    return redirect(url_for('index'))



@app.route('/create_thread', methods=['POST'])
def create_thread():
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    title = request.form.get('title')
    if not title:
        return {"error": "タイトルが必要です"}, 400
    if len(title) > 30:
        return {"error": "スレッド名は50文字以内で入力してください"}, 400
    
    # 🟢 【セキュリティ強化】10秒以内の連続スレ立てをブロック
    now = time.time()
    if client_ip in LAST_POST_TIMES and now - LAST_POST_TIMES[client_ip] < 3:
        return {"error": "連投は禁止されています。しばらく時間を置いてから投稿してください。"}, 429
    LAST_POST_TIMES[client_ip] = now # 書き込み時間を更新
    
    try:
        # Supabaseへ新しいスレッドを保存
        response = supabase.table('threads').insert({
            'title': title,
            'ip_address': client_ip
        }).execute()
        new_thread = response.data[0] if response.data else None
    except Exception as e:
        print(f"スレッド作成エラー: {e}")
        return {"error": "データベースエラー"}, 500
        
    return {"success": True, "thread": new_thread}



@app.route('/thread/<int:thread_id>', methods=['GET', 'POST'])
def thread_view(thread_id):
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    try:
        # 🟢 Supabaseから指定されたスレッドを取得
        thread_res = supabase.table('threads').select('*').eq('id', thread_id).execute()
        if not thread_res.data:
            return "スレッドが見つかりません", 404
        thread = thread_res.data[0]

        # 🟢 Supabaseからスレッド内のレス一覧（古い順）を取得
        replies_res = supabase.table('replies').select('*').eq('thread_id', thread_id).order('id', desc=False).execute()
        thread['replies'] = replies_res.data
        for r in thread['replies']:
            if r.get('date'):
                dt = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                r['date'] = dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        print(f"スレッド読み込みエラー: {e}")
        return "データベースエラーが発生しました", 500

    is_admin_user = check_is_admin_cookie(request)

    # 書き込み（POST）処理
    if request.method == 'POST':
        content = request.form.get('content') or ""
        
        # 🟢 コメントの文字数制限（500文字を超える場合は保存せずにリダイレクト）
        if len(content) > 500:
            return redirect(url_for('thread_view', thread_id=thread_id))
        
        now = time.time()
        if client_ip in LAST_POST_TIMES and now - LAST_POST_TIMES[client_ip] < 10:
            return redirect(url_for('thread_view', thread_id=thread_id))
        
        author_input = request.form.get('author') or "名無しさん"
        if not ("#" in author_input and author_input.split("#", 1)[1] == ADMIN_PASSWORD):
            LAST_POST_TIMES[client_ip] = now

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

        # 画像ファイルのアップロード
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
            try:
                # 🟢 Supabaseへレスを保存（壊れていた部分をしっかり修正！）
                supabase.table('replies').insert({
                    'thread_id': thread_id,
                    'author': author_input,
                    'content': content,
                    'user_id': user_id,
                    'is_admin': is_admin,
                    'image_url': image_url,
                    'ip_address': client_ip
                }).execute()
            except Exception as e:
                print(f"レス保存エラー: {e}")

        # 投稿完了後のリダイレクト処理
        response = redirect(url_for('thread_view', thread_id=thread_id))
        if is_admin:
            response.set_cookie('is_bbs_admin', 'true', max_age=60*60*24)
        return response

    # 画面表示（GET）の処理
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)

    # 🟢 カッコの最後に「, back_to_board="/?tab=threads"」を追加！
    response = make_response(render_template(
        'thread.html', 
        thread=thread, 
        is_admin_user=is_admin_user, 
        active_count=active_count, 
        back_to_board="/?tab=threads"
    ))
    
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response


# スレッド丸ごと削除（管理人用）
@app.route('/thread/<int:thread_id>/delete_thread', methods=['POST'])
def delete_thread(thread_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    try:
        # 🟢 Supabaseからスレッドを削除
        supabase.table('threads').delete().eq('id', thread_id).execute()
    except Exception as e:
        print(f"スレッド削除エラー: {e}")
    return redirect(url_for('index'))

# レス単体削除（あぼーん処理）
@app.route('/thread/<int:thread_id>/delete/<int:reply_id>', methods=['POST'])
def delete_reply(thread_id, reply_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    try:
        # 🟢 Supabaseの該当レスを「あぼーん」に更新
        supabase.table('replies').update({
            'author': 'あぼーん',
            'content': 'この書き込みは管理員によって削除されました。',
            'user_id': '???',
            'is_admin': False,
            'image_url': ''
        }).eq('id', reply_id).execute()
    except Exception as e:
        print(f"レス削除エラー: {e}")
    return redirect(url_for('thread_view', thread_id=thread_id))
    
# 荒らしユーザーをBAN
@app.route('/thread/<int:thread_id>/ban/<int:reply_id>', methods=['POST'])
def ban_user(thread_id, reply_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    
    try:
        reply_res = supabase.table('replies').select('ip_address').eq('id', reply_id).execute()
        if reply_res.data and reply_res.data[0].get('ip_address'):
            b_ip = reply_res.data[0]['ip_address']
            # BANリストにIPを登録
            supabase.table('banned_ips').insert({'ip': b_ip}).execute()
            
            # レスをあぼーん化
            supabase.table('replies').update({
                'author': 'あぼーん',
                'content': 'この書き込みは管理員によってBANされました。',
                'user_id': '???',
                'is_admin': False,
                'image_url': ''
            }).eq('id', reply_id).execute()
    except Exception as e:
        print(f"ユーザーBANエラー: {e}")
            
    return redirect(url_for('thread_view', thread_id=thread_id))

# 🟢 スレ主をBANする管理者用ルート
@app.route('/thread/<int:thread_id>/ban_owner', methods=['POST'])
def ban_thread_owner(thread_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    
    try:
        thread_res = supabase.table('threads').select('ip_address').eq('id', thread_id).execute()
        if thread_res.data and thread_res.data[0].get('ip_address'):
            owner_ip = thread_res.data[0]['ip_address']
            # スレ主のIPをBANテーブルへ登録
            supabase.table('banned_ips').insert({'ip': owner_ip}).execute()
                
            # スレタイをBAN表示に変え、中身のレスを解体
            supabase.table('threads').update({'title': '【このスレッドは管理員によってBANされました】'}).eq('id', thread_id).execute()
            supabase.table('replies').delete().eq('thread_id', thread_id).execute()
            supabase.table('replies').insert({
                'thread_id': thread_id,
                'author': 'あぼーん',
                'content': 'このスレッドの作成者はBANされました。',
                'user_id': '???',
                'is_admin': False,
                'image_url': ''
            }).execute()
    except Exception as e:
        print(f"スレ主BANエラー: {e}")
        
    return redirect(url_for('index'))

# 3秒おきの自動リアルタイム更新API
@app.route('/api/thread/<int:thread_id>/updates')
def thread_updates(thread_id):
    last_id = request.args.get('last_id', type=int, default=0)
    
    user_token = request.cookies.get('user_bbs_token')
    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)
    
    try:
        new_replies_res = supabase.table('replies').select('*').eq('thread_id', thread_id).gt('id', last_id).order('id', desc=False).execute()
        new_replies = new_replies_res.data
        for r in new_replies:
            if r.get('date'):
                dt = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                r['date'] = dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        print(f"自動更新APIエラー: {e}")
        new_replies = []
        
    is_admin_user = check_is_admin_cookie(request)
    return {
        "replies": new_replies, 
        "is_admin_user": is_admin_user, 
        "active_count": active_count
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
