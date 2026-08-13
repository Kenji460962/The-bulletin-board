from flask import Flask, render_template, request, redirect, url_for, make_response, session
from datetime import datetime, timedelta
import json
import html
import os
import hashlib
import uuid
import time
import re
import string
import random
import httpx
import boto3
import psutil
from werkzeug.utils import secure_filename

app = Flask(__name__)
# static配下(CSS・画像・favicon等)のキャッシュ期間を7日に設定。ファイル名を変えない限りブラウザに残るので再訪問時が速くなる
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 7

# psutilのCPU使用率計測を起動時に一度呼んで初期化
psutil.cpu_percent(interval=None)

# --- Cloudflare D1 接続設定 ---
CF_D1_ACCOUNT_ID = os.environ.get('CF_D1_ACCOUNT_ID')
CF_D1_DATABASE_ID = os.environ.get('CF_D1_DATABASE_ID')
CF_D1_API_TOKEN = os.environ.get('CF_D1_API_TOKEN')

def query_d1(sql, params=None):
    """Cloudflare D1 REST APIを使ってSQLを実行する共通関数"""
    if not CF_D1_ACCOUNT_ID or not CF_D1_DATABASE_ID or not CF_D1_API_TOKEN:
        return []
        
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_D1_ACCOUNT_ID}/d1/database/{CF_D1_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {CF_D1_API_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        resp = httpx.post(url, json={"sql": sql, "params": params or []}, headers=headers, timeout=10.0)
        data = resp.json()
        if data.get('success'):
            res = data.get('result', [])
            if res and 'results' in res[0]:
                return res[0]['results']
        else:
            print(f"D1 Query Error: {data.get('errors')}")
    except Exception as e:
        print(f"D1 API通信エラー: {e}")
    return []

# ---- cgroup(コンテナに実際に割り当てられた上限・使用量)を直接読む ----
_last_cpu_usage_usec = None
_last_cpu_check_time = None

def read_cgroup_memory():
    try:
        with open('/sys/fs/cgroup/memory.current') as f:
            used = int(f.read().strip())
        with open('/sys/fs/cgroup/memory.max') as f:
            limit_raw = f.read().strip()
            limit = None if limit_raw == 'max' else int(limit_raw)
        return used, limit
    except Exception:
        pass
    try:
        with open('/sys/fs/cgroup/memory/memory.usage_in_bytes') as f:
            used = int(f.read().strip())
        with open('/sys/fs/cgroup/memory/memory.limit_in_bytes') as f:
            limit = int(f.read().strip())
            if limit > 10**15:
                limit = None
        return used, limit
    except Exception:
        return None, None

def read_cgroup_cpu_percent():
    global _last_cpu_usage_usec, _last_cpu_check_time
    try:
        usage_usec = None
        with open('/sys/fs/cgroup/cpu.stat') as f:
            for line in f:
                k, v = line.strip().split()
                if k == 'usage_usec':
                    usage_usec = int(v)
                    break
        if usage_usec is None:
            return None

        quota_cores = None
        try:
            with open('/sys/fs/cgroup/cpu.max') as f:
                parts = f.read().strip().split()
                if parts[0] != 'max':
                    quota_cores = int(parts[0]) / int(parts[1])
        except Exception:
            pass

        now = time.time()
        percent = None
        if _last_cpu_usage_usec is not None and _last_cpu_check_time is not None:
            usec_delta = usage_usec - _last_cpu_usage_usec
            time_delta = now - _last_cpu_check_time
            if time_delta > 0 and usec_delta >= 0:
                cores_used = (usec_delta / 1_000_000) / time_delta
                denom = quota_cores or (os.cpu_count() or 1)
                percent = round((cores_used / denom) * 100, 1)

        _last_cpu_usage_usec = usage_usec
        _last_cpu_check_time = now
        return percent
    except Exception:
        return None

_last_net_rx_bytes = None
_last_net_tx_bytes = None
_last_net_check_time = None

def read_network_speed():
    global _last_net_rx_bytes, _last_net_tx_bytes, _last_net_check_time
    try:
        rx_total = 0
        tx_total = 0
        with open('/proc/net/dev') as f:
            lines = f.readlines()[2:]
        for line in lines:
            if ':' not in line:
                continue
            iface, rest = line.split(':', 1)
            iface = iface.strip()
            if iface == 'lo':
                continue
            fields = rest.split()
            rx_total += int(fields[0])
            tx_total += int(fields[8])

        now = time.time()
        rx_speed = tx_speed = None
        if _last_net_rx_bytes is not None and _last_net_check_time is not None:
            time_delta = now - _last_net_check_time
            if time_delta > 0:
                rx_speed = max(0, (rx_total - _last_net_rx_bytes) / time_delta)
                tx_speed = max(0, (tx_total - _last_net_tx_bytes) / time_delta)

        _last_net_rx_bytes = rx_total
        _last_net_tx_bytes = tx_total
        _last_net_check_time = now
        return rx_speed, tx_speed
    except Exception:
        return None, None

app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345')

CF_SHARED_SECRET = os.environ.get('CF_SHARED_SECRET')

# スリープ防止
@app.before_request
def response_to_uptimerobot():
    if request.method == 'HEAD':
        return make_response('', 200)

@app.before_request
def enforce_cloudflare_only():
    if request.method == 'HEAD':
        return
    if CF_SHARED_SECRET and request.headers.get('X-Origin-Verify') != CF_SHARED_SECRET:
        return "Access denied", 403

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

ADMIN_PASSWORD = "setokoji114514810072"

# セキュリティ強化 ユーザーごとの最後の書き込み時間を記録する場所
LAST_THREAD_TIMES = {}
LAST_REPLY_TIMES = {}

# IPアドレスを元に毎日変わるIDを生成
def get_daily_user_id(ip_address):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

def get_client_ip():
    if CF_SHARED_SECRET and request.headers.get('X-Origin-Verify') == CF_SHARED_SECRET:
        cf_ip = request.headers.get('CF-Connecting-IP', '').strip()
        if cf_ip:
            return cf_ip
    return request.remote_addr or ""

PROXYCHECK_API_KEY = os.environ.get('PROXYCHECK_API_KEY', '')
_PROXY_CHECK_CACHE = {}
_PROXY_CACHE_TTL = 60 * 60 * 24

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
        res = query_d1("SELECT * FROM banned_ips WHERE ip_address = ?", [ip])
        return len(res) > 0
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
            res = query_d1("SELECT * FROM staff_users WHERE username = ?", [username])
            if res:
                user = res[0]
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

def update_and_get_user_counts(current_token, location):
    """D1を使ってアクティブユーザーを記録・集計する"""
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=2)).isoformat()

    if current_token:
        sql_upsert = """
        INSERT INTO active_users (token, location, last_seen) 
        VALUES (?, ?, ?) 
        ON CONFLICT(token) DO UPDATE SET location=excluded.location, last_seen=excluded.last_seen
        """
        query_d1(sql_upsert, [current_token, location, now.isoformat()])

    sql_count = "SELECT COUNT(*) as cnt FROM active_users WHERE location = ? AND last_seen >= ?"
    res = query_d1(sql_count, [location, cutoff])
    count = res[0]['cnt'] if res and len(res) > 0 else 0

    if random.random() < 0.05:
        query_d1("DELETE FROM active_users WHERE last_seen < ?", [cutoff])

    return count

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')
    
@app.route('/roles')
def roles():
    return render_template('roles.html')

# エラー解消のために追加した games_hub ルート
@app.route('/games')
def games_hub():
    return redirect(url_for('index'))

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
        if search_query:
            threads = query_d1(
                "SELECT * FROM threads WHERE title LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
                [f"%{search_query}%", per_page, start_index]
            )
        else:
            threads = query_d1(
                "SELECT * FROM threads ORDER BY id DESC LIMIT ? OFFSET ?",
                [per_page, start_index]
            )
        
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
                    pinned_res = query_d1("SELECT * FROM threads WHERE id = ?", [pid])
                    if pinned_res:
                        pinned_threads.append(pinned_res[0])
                except Exception as pe:
                    print(f"固定スレッド取得エラー: {pe}")

            for pt in pinned_threads:
                pt['is_pinned'] = True  
                pt['replies_count'] = None  
                threads.insert(0, pt)

        all_thread_ids = [int(t['id']) for t in threads]
        reply_counts = {}
        if all_thread_ids:
            try:
                placeholders = ','.join(['?'] * len(all_thread_ids))
                counts_res = query_d1(
                    f"SELECT thread_id, COUNT(*) as reply_count FROM replies WHERE thread_id IN ({placeholders}) GROUP BY thread_id",
                    all_thread_ids
                )
                for row in (counts_res or []):
                    reply_counts[row['thread_id']] = row['reply_count']
            except Exception as re:
                print(f"レス数取得エラー: {re}")

        for t in threads:
            if t.get('is_pinned') or int(t['id']) in [1, 2, 3, 4]:
                t['is_pinned'] = True
            t['replies_count'] = reply_counts.get(int(t['id']), 0)

        try:
            active_cutoff = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
            active_res = query_d1("SELECT location FROM active_users WHERE last_seen >= ?", [active_cutoff])
            thread_active_counts = {}
            for row in (active_res or []):
                loc = row.get('location', '')
                thread_active_counts[loc] = thread_active_counts.get(loc, 0) + 1
        except Exception as ace:
            print(f"スレ別アクセス数取得エラー: {ace}")
            thread_active_counts = {}

        for t in threads:
            t['thread_active_count'] = thread_active_counts.get(f"thread_{t['id']}", 0)

        try:
            admin_res = query_d1("SELECT message FROM admin_messages WHERE id = ?", [1])
            admin_message = admin_res[0]['message'] if admin_res else "ここに管理者の一言が表示されます。"
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
            query_d1("UPDATE admin_messages SET message = ? WHERE id = ?", [message, 1])
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
        query_d1(
            "INSERT INTO threads (title, ip_address) VALUES (?, ?)",
            [title, client_ip]
        )
        res = query_d1("SELECT * FROM threads ORDER BY id DESC LIMIT 1")
        new_thread = res[0] if res else None
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
                query_d1(
                    """INSERT INTO replies (thread_id, author, content, user_id, is_admin, role, image_url, ip_address) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [thread_id, author_input, content, user_id, 1 if is_admin else 0, role_to_save, image_url, client_ip]
                )
                res = query_d1("SELECT * FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT 1", [thread_id])
                new_reply = res[0] if res else None
                if new_reply:
                    if new_reply.get('date'):
                        dt_utc = datetime.fromisoformat(new_reply['date'].replace('Z', '+00:00'))
                        dt_jst = dt_utc + timedelta(hours=9)
                        new_reply['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
                    if new_reply.get('content'):
                        new_reply['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', new_reply['content'])
                    try:
                        thread_res = query_d1("SELECT ip_address FROM threads WHERE id = ?", [thread_id])
                        op_ip = thread_res[0]['ip_address'] if thread_res else None
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
        thread_res = query_d1("SELECT * FROM threads WHERE id = ?", [thread_id])
        if not thread_res:
            return "スレッドが見つかりません", 404
        thread = thread_res[0]

        RECENT_REPLIES_LIMIT = 300
        replies_res = query_d1(
            "SELECT * FROM (SELECT * FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            [thread_id, RECENT_REPLIES_LIMIT]
        )
        recent_replies = replies_res if replies_res else []
        thread['replies'] = recent_replies
        for r in thread['replies']:
            if r.get('date'):
                dt_utc = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
            if r.get('content'):
                r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', r['content'])

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
        query_d1("DELETE FROM threads WHERE id = ?", [thread_id])
    except Exception as e:
        print(f"スレッド削除エラー: {e}")
    return redirect(url_for('index'))

@app.route('/thread/<int:thread_id>/delete/<int:reply_id>', methods=['POST'])
def delete_reply(thread_id, reply_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
        query_d1("DELETE FROM replies WHERE id = ? AND thread_id = ?", [reply_id, thread_id])
    except Exception as e:
        print(f"レス削除エラー: {e}")
    return redirect(url_for('thread_view', thread_id=thread_id))

@app.route('/ban_user/<int:reply_id>', methods=['POST'])
def ban_user(reply_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
        reply_res = query_d1("SELECT ip_address FROM replies WHERE id = ?", [reply_id])
        if reply_res and reply_res[0].get('ip_address'):
            b_ip = reply_res[0]['ip_address']
            query_d1("INSERT OR IGNORE INTO banned_ips (ip_address) VALUES (?)", [b_ip])
            query_d1(
                """UPDATE replies SET author = ?, content = ?, user_id = ?, is_admin = ?, image_url = ? 
                   WHERE id = ?""",
                ['あぼーん', 'この書き込みは管理員によってBANされました。', '???', 0, '', reply_id]
            )
        return redirect(request.referrer or url_for('index'))
    except Exception as e:
        print(f"BANエラー: {e}")
        return f"エラーが発生しました: {e}", 500

@app.route('/ban_thread_owner/<int:thread_id>', methods=['POST'])
def ban_thread_owner(thread_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
        thread_res = query_d1("SELECT ip_address FROM threads WHERE id = ?", [thread_id])
        if thread_res and thread_res[0].get('ip_address'):
            owner_ip = thread_res[0]['ip_address']
            query_d1("INSERT OR IGNORE INTO banned_ips (ip_address) VALUES (?)", [owner_ip])
            query_d1("UPDATE threads SET title = ? WHERE id = ?", ['【このスレッドは管理員によってBANされました】', thread_id])
            query_d1("DELETE FROM replies WHERE thread_id = ?", [thread_id])
            query_d1(
                """INSERT INTO replies (thread_id, author, content, user_id, is_admin, role, image_url, ip_address) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [thread_id, 'あぼーん', 'このスレッドの作成者はBANされました。', '???', 0, None, '', owner_ip]
            )
        return redirect(url_for('index'))
    except Exception as e:
        print(f"スレッドオーナーBANエラー: {e}")
        return f"エラーが発生しました: {e}", 500

@app.route('/server_metrics')
def server_metrics():
    if not can_manage_board():
        return "Unauthorized", 403

    mem_used, mem_limit = read_cgroup_memory()
    if mem_used is not None:
        if mem_limit and mem_limit > 0:
            memory_percent = round((mem_used / mem_limit) * 100, 1)
            memory_used_mb = round(mem_used / (1024 * 1024), 1)
            memory_limit_mb = round(mem_limit / (1024 * 1024), 1)
        else:
            memory_percent = 0.0
            memory_used_mb = round(mem_used / (1024 * 1024), 1)
            memory_limit_mb = "Unlimited"
    else:
        vm = psutil.virtual_memory()
        memory_percent = vm.percent
        memory_used_mb = round(vm.used / (1024 * 1024), 1)
        memory_limit_mb = round(vm.total / (1024 * 1024), 1)

    cpu_percent = read_cgroup_cpu_percent()
    if cpu_percent is None:
        cpu_percent = psutil.cpu_percent(interval=None)

    rx_speed, tx_speed = read_network_speed()
    rx_kbps = round(rx_speed / 1024, 1) if rx_speed is not None else 0.0
    tx_kbps = round(tx_speed / 1024, 1) if tx_speed is not None else 0.0

    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "memory_used_mb": memory_used_mb,
        "memory_limit_mb": memory_limit_mb,
        "rx_kbps": rx_kbps,
        "tx_kbps": tx_kbps
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
