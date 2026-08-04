from flask import Flask, render_template, request, redirect, url_for, make_response, session
from datetime import datetime
import json
import html
import os
import hashlib
import uuid
import time
import re

# Supabaseを使うためのライブラリを読み込み
from supabase import create_client, Client

import boto3  
import httpx  # supabaseの依存として既にインストールされているのでそのまま使う
from werkzeug.utils import secure_filename

app = Flask(__name__)
# static配下(CSS・画像・favicon等)のキャッシュ期間を7日に設定。ファイル名を変えない限りブラウザに残るので再訪問時が速くなる
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 7
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345') # 必須: セッション暗号化用キー

# スリープ防止
@app.before_request
def response_to_uptimerobot():
    if request.method == 'HEAD':
        return make_response('', 200)

# Cloudflare R2 (S3互換) の設定
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    region_name='auto'
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'bbs-images')
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL')  

SUPABASE_URL = os.environ.get('SUPABASE_URL')

SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY が設定されていません。Railwayの環境変数を確認してください。")

# Supabaseに接続するロボットを起動(service_roleキー: RLSを無視してアクセスできる。サーバー側でのみ使用しブラウザには絶対渡さない)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

DATA_FILE = 'bbs_data.json'
ADMIN_PASSWORD = "setokoji114514810072"

# セキュリティ強化 ユーザーごとの最後の書き込み時間を記録する場所
LAST_POST_TIMES = {}
LAST_THREAD_TIMES = {}
LAST_REPLY_TIMES = {}

def auto_migrate_from_json():
    pass

# IPアドレスを元に毎日変わるIDを生成
def get_daily_user_id(ip_address):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

CF_SHARED_SECRET = os.environ.get('CF_SHARED_SECRET')

def get_client_ip():
    """
    1. Cloudflareの合言葉ヘッダーが一致する場合のみ CF-Connecting-IP を信頼する
    2. 一致しない場合はヘッダーを信用せず remote_addr のみを使う
    """
    if CF_SHARED_SECRET and request.headers.get('X-Origin-Verify') == CF_SHARED_SECRET:
        cf_ip = request.headers.get('CF-Connecting-IP', '').strip()
        if cf_ip:
            return cf_ip
    return request.remote_addr or ""


PROXYCHECK_API_KEY = os.environ.get('PROXYCHECK_API_KEY', '')
_PROXY_CHECK_CACHE = {}
_PROXY_CACHE_TTL = 60 * 60 * 24  # 24時間キャッシュ(同じIPへの無駄な問い合わせを減らしAPI消費を抑える)

def is_proxy_or_vpn(ip):
    if not ip:
        return False
    cached = _PROXY_CHECK_CACHE.get(ip)
    now = time.time()
    if cached and (now - cached["checked_at"] < _PROXY_CACHE_TTL):
        return cached["is_proxy"]

    is_proxy = False
    try:
        params = {"vpn": "1", "asn": "0", "risk": "1"}
        if PROXYCHECK_API_KEY:
            params["key"] = PROXYCHECK_API_KEY
        resp = httpx.get(f"https://proxycheck.io/v2/{ip}", params=params, timeout=2.5)
        data = resp.json()
        info = data.get(ip, {})
        if info.get("proxy") == "yes":
            is_proxy = True
        elif info.get("risk") is not None and int(info.get("risk", 0)) >= 66:
            is_proxy = True
    except Exception as e:
        print(f"プロキシ判定APIエラー: {e}")
        is_proxy = False

    _PROXY_CHECK_CACHE[ip] = {"is_proxy": is_proxy, "checked_at": now}
    return is_proxy

def is_banned_ip(ip):
    if not ip:
        return False
    try:
        res = supabase.table('banned_ips').select('*').eq('ip_address', ip).execute()
        return len(res.data) > 0
    except Exception as e:
        print(f"BANチェックエラー: {e}")
        return False

def get_staff_role():
    return session.get('staff_role')

def can_manage_board():
    return session.get('staff_role') in ['admin', 'sub_admin']

@app.route('/login_secret_8823', methods=['GET', 'POST'])
def staff_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        try:
            res = supabase.table('staff_users').select('*').eq('username', username).execute()
            if res.data:
                user = res.data[0]
                if user['password'] == password: 
                    session['staff_id'] = user['id']
                    session['staff_role'] = user['role']
                    session['staff_name'] = user['display_name']
                    return redirect(url_for('index'))
        except Exception as e:
            print(f"Login error: {e}")
        return "ログイン失敗", 401
    return '''
        <form method="post">
            ID: <input type="text" name="username"><br>
            PW: <input type="password" name="password"><br>
            <input type="submit" value="Enter">
        </form>
    '''

@app.route('/staff_logout')
def staff_logout():
    session.clear()
    return redirect(url_for('index'))

NG_WORDS = {
    '死ね': '〇ね',
    'しね': '〇ね',
    'エロ': 'エ〇',
    'えろ': 'え〇',
    'まんこ': 'ま〇こ',
    'ちんこ': 'ち〇こ',
    'マンコ': 'マ〇こ',
    'チンコ': 'チ〇こ',
    'セックス': 'セ。〇ス',
    'せっくす': 'せ。〇す',
    'おっぱい': 'お。〇い',
    'オッパイ': 'オ。〇イ',
    'レイプ': 'レ〇プ',
    'れいぷ': 'れ〇ぷ',
    'バカ': 'バ*',
    'アホ': 'ア*',
    'シコシコ':'4545',
    'オナニー':'0721',
    '射精':'身寸米青',
}

def filter_ng_words(text):
    if not text:
        return text
    for ng_word, replaced_word in NG_WORDS.items():
        if ng_word in text:
            text = text.replace(ng_word, replaced_word)
    return text

import random
from datetime import timedelta

def update_and_get_user_counts(current_token, location):
    """
    アクセス中人数の集計。以前はプロセス内のdict(ACTIVE_USERS)で管理していたが、
    本番は複数ワーカープロセスで動いており、プロセスごとにメモリが分かれてしまうため
    (ワーカーAが記録した在室情報をワーカーBが見られない)、Supabaseの共有テーブルに変更。
    """
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=5)).isoformat()

    if current_token:
        try:
            supabase.table('active_users').upsert({
                'token': current_token,
                'location': location,
                'last_seen': now.isoformat()
            }).execute()
        except Exception as e:
            print(f"アクティブユーザー更新エラー: {e}")

    count = 0
    try:
        res = supabase.table('active_users').select('token', count='exact').eq('location', location).gte('last_seen', cutoff).execute()
        count = res.count if res.count is not None else len(res.data)
    except Exception as e:
        print(f"アクティブユーザー数取得エラー: {e}")

    # 期限切れレコードの掃除は毎回やるとDB負荷が増えるので、確率的に(だいたい20回に1回)実行する
    if random.random() < 0.05:
        try:
            supabase.table('active_users').delete().lt('last_seen', cutoff).execute()
        except Exception as e:
            print(f"アクティブユーザー掃除エラー: {e}")

    return count

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')
    
@app.route('/roles')
def roles():
    return render_template('roles.html')

@app.route('/', methods=['GET', 'HEAD'])
def index():
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    if request.method == 'HEAD':
        return make_response('', 200)

    page = request.args.get('page', default=1, type=int)
    per_page = 20
    start_index = (page - 1) * per_page
    end_index = start_index + per_page - 1

    search_query = request.args.get('q', default='', type=str).strip()

    try:
        threads_query = supabase.table('threads').select('*').order('id', desc=True)
        if search_query:
            threads_query = threads_query.ilike('title', f'%{search_query}%')
        threads_response = threads_query.range(start_index, end_index).execute()
        threads = threads_response.data
        has_next = len(threads) == per_page

        pinned_ids = [4, 3, 2, 1]
        pinned_threads = []

        if not search_query:
            for pid in pinned_ids:
                for i, t in enumerate(threads):
                    if int(t['id']) == pid:
                        pinned_threads.append(threads.pop(i))
                        break

            for pid in pinned_ids:
                if any(int(pt['id']) == pid for pt in pinned_threads):
                    continue
                try:
                    pinned_res = supabase.table('threads').select('*').eq('id', pid).execute()
                    if pinned_res.data:
                        pinned_threads.append(pinned_res.data[0])
                except Exception as pe:
                    print(f"固定スレッド取得エラー: {pe}")

            for pt in pinned_threads:
                pt['is_pinned'] = True  
                pt['replies_count'] = None  
                threads.insert(0, pt)

        for t in threads:
            if t.get('is_pinned') or int(t['id']) in [1, 2, 3, 4]:
                t['is_pinned'] = True
            try:
                count_res = supabase.table('replies').select('id', count='exact').eq('thread_id', int(t['id'])).execute()
                t['replies_count'] = count_res.count if count_res.count is not None else 0
            except Exception as ce:
                print(f"レス数取得エラー(thread_id={t['id']}): {ce}")
                t['replies_count'] = 0

        try:
            active_cutoff = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
            active_res = supabase.table('active_users').select('location').gte('last_seen', active_cutoff).execute()
            thread_active_counts = {}
            for row in (active_res.data or []):
                loc = row.get('location', '')
                thread_active_counts[loc] = thread_active_counts.get(loc, 0) + 1
        except Exception as ace:
            print(f"スレ別アクセス数取得エラー: {ace}")
            thread_active_counts = {}

        for t in threads:
            t['thread_active_count'] = thread_active_counts.get(f"thread_{t['id']}", 0)

        try:
            admin_res = supabase.table('admin_messages').select('message').eq('id', 1).execute()
            admin_message = admin_res.data[0]['message'] if admin_res.data else "ここに管理者の一言が表示されます。"
        except Exception as ae:
            admin_message = "管理者の一言の取得に失敗しました。"

    except Exception as e:
        print(f"スレッド一覧取得エラー: {e}")
        threads = []
        has_next = False

    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    active_count = update_and_get_user_counts(user_token, "lobby")
    is_admin_user = can_manage_board()

    response = make_response(render_template(
        'index.html', 
        threads=threads, 
        admin_message=admin_message, 
        is_admin_user=is_admin_user, 
        active_count=active_count,
        current_page=page,      
        has_next=has_next,
        search_query=search_query
    ))
    
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
        
    return response

@app.route('/update_admin_message', methods=['POST'])
def update_admin_message():
    if not can_manage_board():
        return "権限がありません", 403
    message = request.form.get('message')
    if message:
        try:
            supabase.table('admin_messages').update({'message': message}).eq('id', 1).execute()
        except Exception as e:
            print(f"メッセージ更新エラー: {e}")
    return redirect(url_for('index'))

@app.route('/create_thread', methods=['POST'])
def create_thread():
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return {"error": "あなたはアクセス禁止（BAN）されています。"}, 403

    title = request.form.get('title')
    if not title:
        return {"error": "タイトルが必要です"}, 400
        
    title = filter_ng_words(title)
    title = html.escape(title)    
    
    if len(title) > 30:
        return {"error": "スレッド名は30文字以内で入力してください"}, 400
    
    is_admin = can_manage_board()
    now = time.time()

    # VPN/プロキシ経由の場合はスレ立て間隔を長めにする(通常180秒 → 600秒)
    thread_cooldown = 180
    if not is_admin and is_proxy_or_vpn(client_ip):
        thread_cooldown = 600

    if not is_admin:
        if client_ip in LAST_THREAD_TIMES and now - LAST_THREAD_TIMES[client_ip] < thread_cooldown:
            remaining_time = int(thread_cooldown - (now - LAST_THREAD_TIMES[client_ip]))
            minutes = remaining_time // 60
            seconds = remaining_time % 60
            return {"error": f"スレッド作成は3分に1回までです。あと {minutes}分 {seconds}秒 お待ちください。"}, 429
            
    LAST_THREAD_TIMES[client_ip] = now 
    
    try:
        response = supabase.table('threads').insert({
            'title': title,
            'ip_address': client_ip
        }).execute()
        new_thread = response.data[0] if response.data else None
    except Exception as e:
        print(f"スレッド作成エラー: {e}")
        return {"error": "データベースエラーが発生しました"}, 500
        
    return {"success": True, "thread": new_thread}

@app.route('/thread/<int:thread_id>', methods=['GET', 'POST'])
def thread_view(thread_id):
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    if request.method == 'POST':
        content = request.form.get('content') or ""
        
        if len(content) > 500:
            return {"success": False, "error": "500文字以内で入力してください。"}, 400
        
        author_input = request.form.get('author') or "名無しさん"

        # 🛠️ 安全にトリップ（#）を分割する処理に修正
        if "#" in author_input:
            parts = author_input.split("#", 1)
            name_part = parts[0][:20]
            pass_part = parts[1]
            author_input = f"{name_part}#{pass_part}"
        else:
            author_input = author_input[:20]

        if author_input.strip() == "あぼーん":
            author_input = "名無しさん"

        content = filter_ng_words(content)
        author_input = filter_ng_words(author_input)
        
        staff_role = get_staff_role()
        
        if staff_role:
            author_input = session.get('staff_name')
            is_admin = can_manage_board()
            user_id = "STAFF"
            role_to_save = staff_role
        else:
            is_admin = False
            role_to_save = None
            if "#" in author_input:
                name_part, _ = author_input.split("#", 1)
                author_input = html.escape(name_part) or "名無しさん"
            else:
                author_input = html.escape(author_input)
            user_id = get_daily_user_id(client_ip)

        content = html.escape(content)
        content = re.sub(r'&gt;&gt;(\d+)', r'>>\1', content)

        now = time.time()
        if not staff_role:
            # レス投稿は頻度が高いため、プロキシ判定API(外部への問い合わせが発生しうる)は呼ばず固定クールダウンにする
            reply_cooldown = 3
            if client_ip in LAST_REPLY_TIMES and now - LAST_REPLY_TIMES[client_ip] < reply_cooldown:
                return {"success": False, "error": f"連続投稿はできません。{reply_cooldown}秒お待ちください。"}, 429
            LAST_REPLY_TIMES[client_ip] = now

        image_url = ""
        if 'image' in request.files:
            file = request.files['image']
            if file and file.filename != '':
                try:
                    orig_filename = secure_filename(file.filename)
                    ext = os.path.splitext(orig_filename)[1]
                    unique_filename = f"{uuid.uuid4()}{ext}"
                    s3_client.upload_fileobj(file, R2_BUCKET_NAME, unique_filename, ExtraArgs={'ContentType': file.content_type})
                    image_url = f"{R2_PUBLIC_URL.rstrip('/')}/{unique_filename}"
                except Exception as e:
                    print(f"R2 Upload Error: {e}")

        if content.strip() or image_url:
            try:
                insert_res = supabase.table('replies').insert({
                    'thread_id': thread_id,
                    'author': author_input,
                    'content': content,
                    'user_id': user_id,
                    'is_admin': is_admin,
                    'role': role_to_save,
                    'image_url': image_url,
                    'ip_address': client_ip
                }).execute()

                new_reply = insert_res.data[0] if insert_res.data else None
                if new_reply:
                    if new_reply.get('date'):
                        dt_utc = datetime.fromisoformat(new_reply['date'].replace('Z', '+00:00'))
                        from datetime import timedelta
                        dt_jst = dt_utc + timedelta(hours=9)
                        new_reply['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
                    if new_reply.get('content'):
                        new_reply['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', new_reply['content'])

                    try:
                        thread_res = supabase.table('threads').select('ip_address').eq('id', thread_id).execute()
                        op_ip = thread_res.data[0]['ip_address'] if thread_res.data else None
                        op_user_id = get_daily_user_id(op_ip) if op_ip else None
                        new_reply['is_op'] = bool(op_user_id) and new_reply.get('user_id') == op_user_id
                    except Exception as ope:
                        new_reply['is_op'] = False

                return {"success": True, "reply": new_reply}
            except Exception as e:
                print(f"レス保存エラー: {e}")
                return {"success": False, "error": "データベースエラーが発生しました。"}, 500

        return {"success": False, "error": "書き込み内容が空です。"}, 400

    try:
        thread_res = supabase.table('threads').select('*').eq('id', thread_id).execute()
        if not thread_res.data:
            return "スレッドが見つかりません", 404
        thread = thread_res.data[0]

        replies_res = supabase.table('replies').select('*').eq('thread_id', thread_id).order('id', desc=False).execute()

        thread['replies'] = replies_res.data
        for r in thread['replies']:
            if r.get('date'):
                dt_utc = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                from datetime import timedelta
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
            
            if r.get('content'):
                r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', r['content'])

        # スレ主(OP)判定: スレ立て時のIPと同じ日次IDを持つレスに目印を付ける
        op_user_id = get_daily_user_id(thread.get('ip_address', '')) if thread.get('ip_address') else None
        for r in thread['replies']:
            r['is_op'] = bool(op_user_id) and r.get('user_id') == op_user_id

    except Exception as e:
        print(f"スレッド読み込みエラー: {e}")
        return "データベースエラーが発生しました", 500

    is_admin_user = can_manage_board()
    
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)

    response = make_response(render_template(
        'thread.html', 
        thread=thread, 
        is_admin_user=is_admin_user, 
        active_count=active_count, 
        back_to_board="/?tab=threads",
        op_user_id=op_user_id
    ))
    
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/thread/<int:thread_id>/delete_thread', methods=['POST'])
def delete_thread(thread_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
        supabase.table('threads').delete().eq('id', thread_id).execute()
    except Exception as e:
        print(f"スレッド削除エラー: {e}")
    return redirect(url_for('index'))

@app.route('/thread/<int:thread_id>/delete/<int:reply_id>', methods=['POST'])
def delete_reply(thread_id, reply_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
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

@app.route('/thread/<int:thread_id>/ban/<int:reply_id>', methods=['POST'])
def ban_user(thread_id, reply_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
        reply_res = supabase.table('replies').select('ip_address').eq('id', reply_id).execute()
        if reply_res.data and reply_res.data[0].get('ip_address'):
            b_ip = reply_res.data[0]['ip_address']
            supabase.table('banned_ips').insert({'ip_address': b_ip}).execute()
            
        supabase.table('replies').update({
            'author': 'あぼーん',
            'content': 'この書き込みは管理員によってBANされました。',
            'user_id': '???',
            'is_admin': False,
            'image_url': ''
        }).eq('id', reply_id).execute()
    except Exception as e:
        return f"【BAN失敗】エラーの原因: {e}", 500
    return redirect(url_for('thread_view', thread_id=thread_id))

@app.route('/thread/<int:thread_id>/ban_owner', methods=['POST'])
def ban_thread_owner(thread_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
        thread_res = supabase.table('threads').select('ip_address').eq('id', thread_id).execute()
        if thread_res.data and thread_res.data[0].get('ip_address'):
            owner_ip = thread_res.data[0]['ip_address']
            supabase.table('banned_ips').insert({'ip_address': owner_ip}).execute()
                
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
        return f"【スレ主BAN失敗】エラーの原因: {e}", 500
    return redirect(url_for('index'))

@app.route('/api/thread/<int:thread_id>/updates')
def thread_updates(thread_id):
    last_id = request.args.get('last_id', type=int, default=0)
    user_token = request.cookies.get('user_bbs_token')
    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)

    try:
        thread_res = supabase.table('threads').select('ip_address').eq('id', thread_id).execute()
        op_ip = thread_res.data[0]['ip_address'] if thread_res.data else None
        op_user_id = get_daily_user_id(op_ip) if op_ip else None

        new_replies_res = supabase.table('replies').select('*').eq('thread_id', thread_id).gt('id', last_id).order('id', desc=False).execute()
        new_replies = new_replies_res.data
        for r in new_replies:
            if r.get('date'):
                dt_utc = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                from datetime import timedelta
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')

            if r.get('content'):
                r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', r['content'])

            r['is_op'] = bool(op_user_id) and r.get('user_id') == op_user_id

    except Exception as e:
        print(f"自動更新APIエラー: {e}")
        new_replies = []
        
    is_admin_user = can_manage_board()
    return {
        "replies": new_replies, 
        "is_admin_user": is_admin_user, 
        "active_count": active_count
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
