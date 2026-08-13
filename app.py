from flask import Flask, render_template, request, redirect, url_for, make_response, session
from datetime import datetime
import json
import html
import os
import hashlib
import uuid
import time
import re
import string




import httpx

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







# Supabaseを使うためのライブラリを読み込み
from supabase import create_client, Client

import boto3  
import httpx  # supabaseの依存として既にインストールされているのでそのまま使う
import psutil  # 実際のCPU・メモリ使用率を取得するため
from werkzeug.utils import secure_filename

app = Flask(__name__)
# static配下(CSS・画像・favicon等)のキャッシュ期間を7日に設定。ファイル名を変えない限りブラウザに残るので再訪問時が速くなる
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 7

# psutilのCPU使用率計測を起動時に一度呼んで初期化(cgroup読み取りに失敗した場合のフォールバック用)
psutil.cpu_percent(interval=None)

# ---- cgroup(コンテナに実際に割り当てられた上限・使用量)を直接読む ----
# psutilはホスト全体の数値を返すことがあり、Railway側の表示(コンテナへの割当量基準)とズレるため、
# こちらの方がRailwayのMetrics画面の数値に近くなる
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
            if limit > 10**15:  # 事実上無制限を意味する巨大値
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
    """直近の呼び出しからの差分でイン・アウトの速度(バイト/秒)を計算する"""
    global _last_net_rx_bytes, _last_net_tx_bytes, _last_net_check_time
    try:
        rx_total = 0
        tx_total = 0
        with open('/proc/net/dev') as f:
            lines = f.readlines()[2:]  # 先頭2行はヘッダーなのでスキップ
        for line in lines:
            if ':' not in line:
                continue
            iface, rest = line.split(':', 1)
            iface = iface.strip()
            if iface == 'lo':  # ループバックはコンテナ外との通信ではないので除外
                continue
            fields = rest.split()
            rx_total += int(fields[0])   # 受信バイト数(累計)
            tx_total += int(fields[8])   # 送信バイト数(累計)

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
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345') # 必須: セッション暗号化用キー

# スリープ防止
@app.before_request
def response_to_uptimerobot():
    if request.method == 'HEAD':
        return make_response('', 200)

@app.before_request
def enforce_cloudflare_only():
    # UptimeRobotなどのヘルスチェックはHEADリクエストなので上のresponse_to_uptimerobotで先に処理される
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
        res = query_d1("SELECT * FROM banned_ips WHERE ip_address = ?", [ip])
        return len(res) > 0
    except Exception as e:
        print(f"BANチェックエラー: {e}")
        return False

def get_staff_role():
    return session.get('staff_role')

def can_manage_board():
    return session.get('staff_role') in ['admin', 'sub_admin']


####################


@app.route('/admin_migrate_data_run_once')
def admin_migrate_data_run_once():
    """SupabaseからD1へ過去データを1回だけコピーする一時ルート"""
    try:
        # 1. Supabaseからスレッドを全件取得
        res_threads = supabase.table('threads').select('*').execute()
        threads = res_threads.data if res_threads and res_threads.data else []
        
        migrated_threads = 0
        for t in threads:
            sql = "INSERT OR IGNORE INTO threads (id, title, ip_address, created_at) VALUES (?, ?, ?, ?)"
            query_d1(sql, [t.get('id'), t.get('title'), t.get('ip_address'), t.get('created_at')])
            migrated_threads += 1

        # 2. Supabaseからレスを全件取得
        res_replies = supabase.table('replies').select('*').execute()
        replies = res_replies.data if res_replies and res_replies.data else []
        
        migrated_replies = 0
        for r in replies:
            sql = """
            INSERT OR IGNORE INTO replies (id, thread_id, author, content, user_id, is_admin, role, image_url, ip_address, date) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            query_d1(sql, [
                r.get('id'), r.get('thread_id'), r.get('author'), r.get('content'), 
                r.get('user_id'), 1 if r.get('is_admin') else 0, r.get('role'), 
                r.get('image_url'), r.get('ip_address'), r.get('date')
            ])
            migrated_replies += 1

        return f"データ移行完了！ スレッド: {migrated_threads}件, レス: {migrated_replies}件 をD1にコピーしました。"
    except Exception as e:
        return f"移行エラー: {str(e)}"


##########################
@app.route('/manual_archive', methods=['GET'])
def manual_archive():
    """Supabaseの全スレッド・全レスをJSON形式にまとめ、Cloudflare R2にアーカイブとして保存する"""
    if not can_manage_board():
        return "権限がありません（管理者ログインが必要です）", 403

    try:
        # 1. Supabaseからすべてのスレッドとレスを取得
        threads_res = supabase.table('threads').select('*').execute()
        replies_res = supabase.table('replies').select('*').execute()

        # 2. JSON構造にまとめる
        archive_data = {
            "version": 1,
            "archived_at": datetime.utcnow().isoformat(),
            "threads": threads_res.data,
            "replies": replies_res.data
        }

        # 3. JSON文字列にシリアライズ（日本語が文字化けしないように ensure_ascii=False）
        json_string = json.dumps(archive_data, ensure_ascii=False, indent=2)

        # 4. R2に保存するファイル名（例: archives/bbs_data_20260606.json）
        filename = f"archives/bbs_data_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"

        # 5. Cloudflare R2へアップロード
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=filename,
            Body=json_string.encode('utf-8'),
            ContentType='application/json'
        )

        return f"成功！全データをJSONにまとめてR2に保存しました。<br>保存先キー: <code>{filename}</code>"
    
    except Exception as e:
        print(f"JSONアーカイブエラー: {e}")
        return f"エラーが発生しました: {str(e)}", 500





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

import random
from datetime import timedelta







def update_and_get_user_counts(current_token, location):
    """D1を使ってアクティブユーザーを記録・集計する"""
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=2)).isoformat()

    # 1. ユーザーの現在位置をD1に記録（UPSERT）
    if current_token:
        sql_upsert = """
        INSERT INTO active_users (token, location, last_seen) 
        VALUES (?, ?, ?) 
        ON CONFLICT(token) DO UPDATE SET location=excluded.location, last_seen=excluded.last_seen
        """
        query_d1(sql_upsert, [current_token, location, now.isoformat()])

    # 2. 現在その場所にいる人数をD1からカウント
    sql_count = "SELECT COUNT(*) as cnt FROM active_users WHERE location = ? AND last_seen >= ?"
    res = query_d1(sql_count, [location, cutoff])
    count = res[0]['cnt'] if res and len(res) > 0 else 0

    # 3. 確率で古いデータをD1から削除（掃除）
    if random.random() < 0.05:
        query_d1("DELETE FROM active_users WHERE last_seen < ?", [cutoff])

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
                    pinned_res = supabase.table('threads').select('*').eq('id', pid).execute()
                    if pinned_res.data:
                        pinned_threads.append(pinned_res.data[0])
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
                counts_res = supabase.rpc('get_reply_counts', {'thread_ids': all_thread_ids}).execute()
                for row in (counts_res.data or []):
                    reply_counts[row['thread_id']] = row['reply_count']
            except Exception as re:
                print(f"レス数取得エラー: {re}")

        for t in threads:
            if t.get('is_pinned') or int(t['id']) in [1, 2, 3, 4]:
                t['is_pinned'] = True
            t['replies_count'] = reply_counts.get(int(t['id']), 0)

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
        query_d1(
            "INSERT INTO threads (title, ip_address) VALUES (?, ?)",
            [title, client_ip]
        )
        # 追加された最新のスレッドを取得
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

                query_d1(
                    """INSERT INTO replies (thread_id, author, content, user_id, is_admin, role, image_url, ip_address) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [thread_id, author_input, content, user_id, 1 if is_admin else 0, role_to_save, image_url, client_ip]
                )
                # 追加された最新のレスを取得
                res = query_d1("SELECT * FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT 1", [thread_id])
                new_reply = res[0] if res else None



                
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
        thread_res = query_d1("SELECT * FROM threads WHERE id = ?", [thread_id])
        if not thread_res:
            return "スレッドが見つかりません", 404
        thread = thread_res[0]
        
        
        

        # Egress対策: 全レスではなく直近300件だけ取得(古いIDから昇順で表示するため一度desc取得してreverse)
        RECENT_REPLIES_LIMIT = 300

        # SQLiteは昇順でLIMIT取得してから反転させるか、サブクエリを使います
        replies_res = query_d1(
            "SELECT * FROM (SELECT * FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            [thread_id, RECENT_REPLIES_LIMIT]
        )
        recent_replies = replies_res if replies_res else []


        thread['replies'] = recent_replies
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
        query_d1("DELETE FROM threads WHERE id = ?", [thread_id])
    except Exception as e:
        print(f"スレッド削除エラー: {e}")
    return redirect(url_for('index'))

@app.route('/thread/<int:thread_id>/delete/<int:reply_id>', methods=['POST'])
def delete_reply(thread_id, reply_id):
    if not can_manage_board():
        return "権限がありません", 403
    try:
        query_d1(
            """UPDATE replies SET author = ?, content = ?, user_id = ?, is_admin = ?, image_url = ? 
               WHERE id = ?""",
            ['あぼーん', 'この書き込みは管理員によって削除されました。', '???', 0, '', reply_id]
        )

    
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

@app.route('/api/server_stats')
def server_stats():
    try:
        mem_used, mem_limit = read_cgroup_memory()
        cpu_percent = read_cgroup_cpu_percent()

        # cgroup読み取りに失敗した場合はpsutilにフォールバック(値の基準は多少ズレる可能性あり)
        if mem_used is None or mem_limit is None:
            mem = psutil.virtual_memory()
            mem_used = mem.used
            mem_limit = mem.total
        if cpu_percent is None:
            cpu_percent = psutil.cpu_percent(interval=None)

        memory_percent = round((mem_used / mem_limit) * 100, 1) if mem_limit else 0

        rx_speed, tx_speed = read_network_speed()

        return {
            "cpu_percent": round(cpu_percent, 1),
            "memory_percent": memory_percent,
            "memory_used_mb": round(mem_used / (1024 * 1024)),
            "memory_total_mb": round(mem_limit / (1024 * 1024)) if mem_limit else 0,
            "net_rx_kbps": round(rx_speed / 1024, 1) if rx_speed is not None else None,
            "net_tx_kbps": round(tx_speed / 1024, 1) if tx_speed is not None else None,
            "timestamp": time.time()
        }
    except Exception as e:
        print(f"サーバー負荷取得エラー: {e}")
        return {"error": "取得に失敗しました"}, 500

@app.route('/api/lobby/active_count')
def lobby_active_count():
    user_token = request.cookies.get('user_bbs_token')
    active_count = update_and_get_user_counts(user_token, "lobby")
    return {"active_count": active_count}

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

# ==================== オンラインオセロ ====================

OTHELLO_DIRS = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]

def othello_new_board():
    b = ['.'] * 64
    b[27] = 'W'
    b[28] = 'B'
    b[35] = 'B'
    b[36] = 'W'
    return ''.join(b)

def othello_idx(r, c):
    return r * 8 + c

def othello_in_bounds(r, c):
    return 0 <= r < 8 and 0 <= c < 8

def othello_opponent(p):
    return 'W' if p == 'B' else 'B'

def othello_flips_for_move(board, player, r, c):
    if not othello_in_bounds(r, c) or board[othello_idx(r, c)] != '.':
        return []
    opp = othello_opponent(player)
    all_flips = []
    for dr, dc in OTHELLO_DIRS:
        line = []
        rr, cc = r + dr, c + dc
        while othello_in_bounds(rr, cc) and board[othello_idx(rr, cc)] == opp:
            line.append((rr, cc))
            rr += dr
            cc += dc
        if line and othello_in_bounds(rr, cc) and board[othello_idx(rr, cc)] == player:
            all_flips.extend(line)
    return all_flips

def othello_valid_moves(board, player):
    return [(r, c) for r in range(8) for c in range(8) if othello_flips_for_move(board, player, r, c)]

def othello_apply_move(board, player, r, c):
    flips = othello_flips_for_move(board, player, r, c)
    board_list = list(board)
    board_list[othello_idx(r, c)] = player
    for (fr, fc) in flips:
        board_list[othello_idx(fr, fc)] = player
    return ''.join(board_list)

def othello_count(board):
    return board.count('B'), board.count('W')

def othello_generate_room_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def get_or_create_user_token():
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True
    return user_token, is_new_user

@app.route('/games')
def games_hub():
    return render_template('games_hub.html')

@app.route('/game')
def game_lobby():
    user_token, is_new_user = get_or_create_user_token()
    response = make_response(render_template('game.html', room=None, my_color=None))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/game/create', methods=['POST'])
def game_create():
    user_token, is_new_user = get_or_create_user_token()
    player_name = (request.form.get('name') or '').strip()[:20] or '名無しさん'

    room_code = othello_generate_room_code()
    for _ in range(5):
        existing = supabase.table('othello_games').select('room_code').eq('room_code', room_code).execute()
        if not existing.data:
            break
        room_code = othello_generate_room_code()

    try:
        supabase.table('othello_games').insert({
            'room_code': room_code,
            'board': othello_new_board(),
            'turn': 'B',
            'player_black_token': user_token,
            'player_black_name': player_name,
            'status': 'waiting'
        }).execute()
    except Exception as e:
        print(f"オセロ部屋作成エラー: {e}")
        return "部屋の作成に失敗しました", 500

    response = make_response(redirect(url_for('game_room', room_code=room_code)))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/game/<room_code>')
def game_room(room_code):
    user_token, is_new_user = get_or_create_user_token()
    room_code = room_code.upper()

    try:
        res = supabase.table('othello_games').select('*').eq('room_code', room_code).execute()
    except Exception as e:
        print(f"オセロ部屋取得エラー: {e}")
        return "エラーが発生しました", 500

    if not res.data:
        return "部屋が見つかりません", 404

    game = res.data[0]
    my_color = None
    if game.get('player_black_token') == user_token:
        my_color = 'B'
    elif game.get('player_white_token') == user_token:
        my_color = 'W'

    response = make_response(render_template('game.html', room=game, my_color=my_color))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/game/<room_code>/join', methods=['POST'])
def game_join(room_code):
    user_token, is_new_user = get_or_create_user_token()
    room_code = room_code.upper()
    body = request.get_json(silent=True) or {}
    player_name = (body.get('name') or '').strip()[:20] or '名無しさん'

    try:
        res = supabase.table('othello_games').select('*').eq('room_code', room_code).execute()
    except Exception as e:
        return {"success": False, "error": "取得エラーが発生しました"}, 500

    if not res.data:
        return {"success": False, "error": "部屋が見つかりません"}, 404

    game = res.data[0]

    if game.get('player_black_token') == user_token or game.get('player_white_token') == user_token:
        return {"success": True}

    if game.get('player_white_token'):
        return {"success": False, "error": "この部屋は満員です"}, 400

    try:
        supabase.table('othello_games').update({
            'player_white_token': user_token,
            'player_white_name': player_name,
            'status': 'playing',
            'updated_at': datetime.utcnow().isoformat()
        }).eq('room_code', room_code).execute()
    except Exception as e:
        return {"success": False, "error": "参加処理でエラーが発生しました"}, 500

    response = make_response({"success": True})
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/game/<room_code>/move', methods=['POST'])
def game_move(room_code):
    user_token = request.cookies.get('user_bbs_token')
    room_code = room_code.upper()
    data = request.get_json(silent=True) or {}
    r = data.get('row')
    c = data.get('col')

    if not isinstance(r, int) or not isinstance(c, int) or not (0 <= r < 8) or not (0 <= c < 8):
        return {"success": False, "error": "不正な座標です"}, 400

    try:
        res = supabase.table('othello_games').select('*').eq('room_code', room_code).execute()
    except Exception as e:
        return {"success": False, "error": "取得エラーが発生しました"}, 500

    if not res.data:
        return {"success": False, "error": "部屋が見つかりません"}, 404

    game = res.data[0]

    if game['status'] != 'playing':
        return {"success": False, "error": "対局中ではありません"}, 400

    my_color = None
    if game.get('player_black_token') == user_token:
        my_color = 'B'
    elif game.get('player_white_token') == user_token:
        my_color = 'W'

    if not my_color:
        return {"success": False, "error": "この部屋の参加者ではありません"}, 403

    if game['turn'] != my_color:
        return {"success": False, "error": "相手のターンです"}, 400

    board = game['board']
    if not othello_flips_for_move(board, my_color, r, c):
        return {"success": False, "error": "そこには置けません"}, 400

    new_board = othello_apply_move(board, my_color, r, c)
    opp = othello_opponent(my_color)
    new_turn = opp
    new_status = 'playing'
    winner = None

    if not othello_valid_moves(new_board, opp):
        if not othello_valid_moves(new_board, my_color):
            new_status = 'finished'
            b_count, w_count = othello_count(new_board)
            if b_count > w_count:
                winner = 'B'
            elif w_count > b_count:
                winner = 'W'
            else:
                winner = 'draw'
        else:
            new_turn = my_color

    update_data = {
        'board': new_board,
        'turn': new_turn,
        'status': new_status,
        'updated_at': datetime.utcnow().isoformat()
    }
    if winner:
        update_data['winner'] = winner

    try:
        supabase.table('othello_games').update(update_data).eq('room_code', room_code).execute()
    except Exception as e:
        print(f"オセロ着手エラー: {e}")
        return {"success": False, "error": "更新処理でエラーが発生しました"}, 500

    return {"success": True}

@app.route('/api/game/<room_code>/state')
def game_state(room_code):
    room_code = room_code.upper()
    try:
        res = supabase.table('othello_games').select('*').eq('room_code', room_code).execute()
    except Exception as e:
        return {"error": "取得エラーが発生しました"}, 500

    if not res.data:
        return {"error": "部屋が見つかりません"}, 404

    game = res.data[0]
    user_token = request.cookies.get('user_bbs_token')
    my_color = None
    if game.get('player_black_token') == user_token:
        my_color = 'B'
    elif game.get('player_white_token') == user_token:
        my_color = 'W'

    b_count, w_count = othello_count(game['board'])

    black_token = game.get('player_black_token')
    white_token = game.get('player_white_token')

    return {
        "board": game['board'],
        "turn": game['turn'],
        "status": game['status'],
        "winner": game.get('winner'),
        "has_white": bool(white_token),
        "my_color": my_color,
        "black_count": b_count,
        "white_count": w_count,
        "black_name": game.get('player_black_name') or '名無しさん',
        "white_name": game.get('player_white_name') or '名無しさん',
        "black_id": get_daily_user_id(black_token) if black_token else None,
        "white_id": get_daily_user_id(white_token) if white_token else None
    }

# ==================== 過去ログアーカイブ(Supabase容量対策) ====================

ARCHIVE_SECRET = os.environ.get('ARCHIVE_SECRET')
ARCHIVE_PINNED_IDS = [1, 2, 3, 4]  # 固定スレは対象外
try:
    ARCHIVE_AFTER_DAYS = int(os.environ.get('ARCHIVE_AFTER_DAYS', '30').strip())
except (ValueError, AttributeError):
    print("警告: ARCHIVE_AFTER_DAYSの値が不正なため、デフォルトの30日を使用します")
    ARCHIVE_AFTER_DAYS = 30

@app.route('/internal/archive-old-threads', methods=['POST'])
def archive_old_threads():
    # 誰でも叩けると全スレ削除される危険なエンドポイントなので、合言葉必須。定期実行サービス(Railway Cronなど)専用
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return {"error": "unauthorized"}, 403

    cutoff = (datetime.utcnow() - timedelta(days=ARCHIVE_AFTER_DAYS)).isoformat()
    archived = []
    errors = []

    try:
        threads_res = supabase.table('threads').select('*').execute()
        all_threads = threads_res.data or []
    except Exception as e:
        return {"error": f"スレッド一覧の取得に失敗しました: {e}"}, 500

    for t in all_threads:
        tid = int(t['id'])
        if tid in ARCHIVE_PINNED_IDS:
            continue

        try:
            replies_res = supabase.table('replies').select('*').eq('thread_id', tid).order('id', desc=True).limit(1).execute()
            last_reply = replies_res.data[0] if replies_res.data else None

            # 最後の動きの日時(最終レス、無ければスレ作成日時)が基準より新しければスキップ
            last_activity = last_reply['date'] if last_reply else t.get('created_at')
            if last_activity and last_activity > cutoff:
                continue
            if not last_activity:
                continue

            all_replies_res = supabase.table('replies').select('*').eq('thread_id', tid).order('id', desc=False).execute()

            archive_payload = {
                "thread": t,
                "replies": all_replies_res.data or [],
                "archived_at": datetime.utcnow().isoformat()
            }

            archive_key = f"archive/thread_{tid}.json"
            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=archive_key,
                Body=json.dumps(archive_payload, ensure_ascii=False, indent=2).encode('utf-8'),
                ContentType='application/json'
            )

            supabase.table('replies').delete().eq('thread_id', tid).execute()
            supabase.table('threads').delete().eq('id', tid).execute()

            try:
                supabase.table('archived_threads_index').upsert({
                    'thread_id': tid,
                    'title': t.get('title', '(無題)'),
                    'reply_count': len(all_replies_res.data or []),
                    'archived_at': datetime.utcnow().isoformat()
                }).execute()
            except Exception as ie:
                print(f"アーカイブ索引登録エラー(thread_id={tid}): {ie}")

            archived.append(tid)
        except Exception as e:
            errors.append({"thread_id": tid, "error": str(e)})
            print(f"アーカイブエラー(thread_id={tid}): {e}")

    return {
        "archived_count": len(archived),
        "archived_thread_ids": archived,
        "errors": errors
    }

@app.route('/archive')
def archive_list():
    page = request.args.get('page', default=1, type=int)
    per_page = 20
    start_index = (page - 1) * per_page
    end_index = start_index + per_page - 1

    archived_threads = []
    has_next = False
    try:
        res = supabase.table('archived_threads_index').select('*').order('archived_at', desc=True).range(start_index, end_index).execute()
        archived_threads = res.data or []
        has_next = len(archived_threads) == per_page
    except Exception as e:
        print(f"過去ログ一覧取得エラー: {e}")

    return render_template('archive_list.html', archived_threads=archived_threads, current_page=page, has_next=has_next)

@app.route('/archive/<int:thread_id>')
def archive_view(thread_id):
    archive_key = f"archive/thread_{thread_id}.json"
    try:
        obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=archive_key)
        payload = json.loads(obj['Body'].read().decode('utf-8'))
    except Exception as e:
        print(f"過去ログ取得エラー(thread_id={thread_id}): {e}")
        return "この過去ログは見つかりませんでした", 404

    thread = payload.get('thread', {})
    replies = payload.get('replies', [])

    for r in replies:
        if r.get('date'):
            try:
                dt_utc = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
        if r.get('content'):
            r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', r['content'])

    return render_template('archive_view.html', thread=thread, replies=replies, archived_at=payload.get('archived_at'))

# ==================== オンラインチェス ====================

CHESS_DIRS_ROOK = [(-1,0),(1,0),(0,-1),(0,1)]
CHESS_DIRS_BISHOP = [(-1,-1),(-1,1),(1,-1),(1,1)]
CHESS_DIRS_KING = CHESS_DIRS_ROOK + CHESS_DIRS_BISHOP
CHESS_KNIGHT_MOVES = [(-2,-1),(-2,1),(-1,-2),(-1,2),(1,-2),(1,2),(2,-1),(2,1)]

def chess_new_board():
    board = [''] * 64
    back_black = ['bR','bN','bB','bQ','bK','bB','bN','bR']
    back_white = ['wR','wN','wB','wQ','wK','wB','wN','wR']
    for c in range(8):
        board[0*8+c] = back_black[c]
        board[1*8+c] = 'bP'
        board[6*8+c] = 'wP'
        board[7*8+c] = back_white[c]
    return board

def chess_in_bounds(r, c):
    return 0 <= r < 8 and 0 <= c < 8

def chess_find_king(board, color):
    target = color + 'K'
    for i, p in enumerate(board):
        if p == target:
            return i // 8, i % 8
    return None

def chess_square_attacked(board, r, c, by_color):
    for dc in (-1, 1):
        pr, pc = r + 1, c + dc
        if by_color == 'w' and chess_in_bounds(pr, pc) and board[pr*8+pc] == 'wP':
            return True
        pr2, pc2 = r - 1, c + dc
        if by_color == 'b' and chess_in_bounds(pr2, pc2) and board[pr2*8+pc2] == 'bP':
            return True
    for dr, dc in CHESS_KNIGHT_MOVES:
        rr, cc = r + dr, c + dc
        if chess_in_bounds(rr, cc) and board[rr*8+cc] == by_color + 'N':
            return True
    for dr, dc in CHESS_DIRS_KING:
        rr, cc = r + dr, c + dc
        if chess_in_bounds(rr, cc) and board[rr*8+cc] == by_color + 'K':
            return True
    for dr, dc in CHESS_DIRS_ROOK:
        rr, cc = r + dr, c + dc
        while chess_in_bounds(rr, cc):
            p = board[rr*8+cc]
            if p:
                if p == by_color+'R' or p == by_color+'Q':
                    return True
                break
            rr += dr; cc += dc
    for dr, dc in CHESS_DIRS_BISHOP:
        rr, cc = r + dr, c + dc
        while chess_in_bounds(rr, cc):
            p = board[rr*8+cc]
            if p:
                if p == by_color+'B' or p == by_color+'Q':
                    return True
                break
            rr += dr; cc += dc
    return False

def chess_in_check(board, color):
    pos = chess_find_king(board, color)
    if not pos:
        return False
    r, c = pos
    opp = 'b' if color == 'w' else 'w'
    return chess_square_attacked(board, r, c, opp)

def chess_pseudo_moves(board, r, c, castling, en_passant):
    piece = board[r*8+c]
    if not piece:
        return []
    color, ptype = piece[0], piece[1]
    moves = []

    if ptype == 'P':
        direction = -1 if color == 'w' else 1
        start_row = 6 if color == 'w' else 1
        promo_row = 0 if color == 'w' else 7
        one_r = r + direction
        if chess_in_bounds(one_r, c) and not board[one_r*8+c]:
            moves.append((one_r, c, 'promo' if one_r == promo_row else None))
            two_r = r + 2*direction
            if r == start_row and not board[two_r*8+c]:
                moves.append((two_r, c, 'double'))
        for dc in (-1, 1):
            tr, tc = r + direction, c + dc
            if chess_in_bounds(tr, tc):
                target = board[tr*8+tc]
                if target and target[0] != color:
                    moves.append((tr, tc, 'promo' if tr == promo_row else None))
                elif en_passant and en_passant == (tr, tc):
                    moves.append((tr, tc, 'enpassant'))
    elif ptype == 'N':
        for dr, dc in CHESS_KNIGHT_MOVES:
            tr, tc = r+dr, c+dc
            if chess_in_bounds(tr, tc):
                target = board[tr*8+tc]
                if not target or target[0] != color:
                    moves.append((tr, tc, None))
    elif ptype in ('B', 'R', 'Q'):
        dirs = []
        if ptype in ('B', 'Q'): dirs += CHESS_DIRS_BISHOP
        if ptype in ('R', 'Q'): dirs += CHESS_DIRS_ROOK
        for dr, dc in dirs:
            tr, tc = r+dr, c+dc
            while chess_in_bounds(tr, tc):
                target = board[tr*8+tc]
                if not target:
                    moves.append((tr, tc, None))
                else:
                    if target[0] != color:
                        moves.append((tr, tc, None))
                    break
                tr += dr; tc += dc
    elif ptype == 'K':
        for dr, dc in CHESS_DIRS_KING:
            tr, tc = r+dr, c+dc
            if chess_in_bounds(tr, tc):
                target = board[tr*8+tc]
                if not target or target[0] != color:
                    moves.append((tr, tc, None))
        row = 7 if color == 'w' else 0
        if r == row and c == 4:
            k_flag = 'K' if color == 'w' else 'k'
            q_flag = 'Q' if color == 'w' else 'q'
            opp = 'b' if color == 'w' else 'w'
            if (k_flag in castling and not board[row*8+5] and not board[row*8+6]
                    and board[row*8+7] == color+'R'
                    and not chess_square_attacked(board, row, 4, opp)
                    and not chess_square_attacked(board, row, 5, opp)
                    and not chess_square_attacked(board, row, 6, opp)):
                moves.append((row, 6, 'castle_k'))
            if (q_flag in castling and not board[row*8+3] and not board[row*8+2] and not board[row*8+1]
                    and board[row*8+0] == color+'R'
                    and not chess_square_attacked(board, row, 4, opp)
                    and not chess_square_attacked(board, row, 3, opp)
                    and not chess_square_attacked(board, row, 2, opp)):
                moves.append((row, 2, 'castle_q'))
    return moves

def chess_apply_move(board, castling, r, c, tr, tc, extra):
    new_board = board[:]
    piece = new_board[r*8+c]
    color = piece[0]
    new_board[r*8+c] = ''
    new_en_passant = None

    if extra == 'enpassant':
        new_board[r*8+tc] = ''
        new_board[tr*8+tc] = piece
    elif extra == 'double':
        new_board[tr*8+tc] = piece
        new_en_passant = ((r+tr)//2, c)
    elif extra == 'promo':
        new_board[tr*8+tc] = color + 'Q'
    elif extra == 'castle_k':
        new_board[tr*8+tc] = piece
        new_board[r*8+7] = ''
        new_board[r*8+5] = color + 'R'
    elif extra == 'castle_q':
        new_board[tr*8+tc] = piece
        new_board[r*8+0] = ''
        new_board[r*8+3] = color + 'R'
    else:
        new_board[tr*8+tc] = piece

    new_castling = castling
    if piece[1] == 'K':
        new_castling = new_castling.replace('K','').replace('Q','') if color=='w' else new_castling.replace('k','').replace('q','')
    for (rr, cc), flag in [((7,0),'Q'), ((7,7),'K'), ((0,0),'q'), ((0,7),'k')]:
        if (r, c) == (rr, cc) or (tr, tc) == (rr, cc):
            new_castling = new_castling.replace(flag, '')

    return new_board, new_castling, new_en_passant

def chess_legal_moves_for(board, color, castling, en_passant):
    all_moves = []
    for i, p in enumerate(board):
        if p and p[0] == color:
            r, c = i // 8, i % 8
            for tr, tc, extra in chess_pseudo_moves(board, r, c, castling, en_passant):
                new_board, _, _ = chess_apply_move(board, castling, r, c, tr, tc, extra)
                if not chess_in_check(new_board, color):
                    all_moves.append((r, c, tr, tc, extra))
    return all_moves

def chess_game_status(board, color, castling, en_passant):
    moves = chess_legal_moves_for(board, color, castling, en_passant)
    if not moves:
        return 'checkmate' if chess_in_check(board, color) else 'stalemate'
    return 'ongoing'

@app.route('/chess')
def chess_lobby():
    user_token, is_new_user = get_or_create_user_token()
    response = make_response(render_template('chess.html', room=None, my_color=None))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/chess/create', methods=['POST'])
def chess_create():
    user_token, is_new_user = get_or_create_user_token()
    player_name = request.form.get('name', '').strip()[:20] or None

    room_code = othello_generate_room_code()
    try:
        existing = supabase.table('chess_games').select('room_code').eq('room_code', room_code).execute()
        while existing.data:
            room_code = othello_generate_room_code()
            existing = supabase.table('chess_games').select('room_code').eq('room_code', room_code).execute()

        supabase.table('chess_games').insert({
            'room_code': room_code,
            'board': json.dumps(chess_new_board()),
            'turn': 'w',
            'castling': 'KQkq',
            'en_passant': None,
            'player_white_token': user_token,
            'player_white_name': player_name,
            'status': 'waiting'
        }).execute()
    except Exception as e:
        print(f"チェス部屋作成エラー: {e}")
        return "部屋の作成に失敗しました", 500

    response = make_response(redirect(url_for('chess_room', room_code=room_code)))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/chess/<room_code>')
def chess_room(room_code):
    user_token, is_new_user = get_or_create_user_token()
    room_code = room_code.upper()
    try:
        res = supabase.table('chess_games').select('*').eq('room_code', room_code).execute()
        if not res.data:
            return "この部屋は見つかりませんでした", 404
        game = res.data[0]
    except Exception as e:
        print(f"チェス部屋取得エラー: {e}")
        return "データベースエラーが発生しました", 500

    my_color = None
    if game.get('player_white_token') == user_token:
        my_color = 'w'
    elif game.get('player_black_token') == user_token:
        my_color = 'b'

    response = make_response(render_template('chess.html', room=game, my_color=my_color))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/chess/<room_code>/join', methods=['POST'])
def chess_join(room_code):
    user_token, is_new_user = get_or_create_user_token()
    room_code = room_code.upper()
    player_name = (request.get_json(silent=True) or {}).get('name', '').strip()[:20] or None

    try:
        res = supabase.table('chess_games').select('*').eq('room_code', room_code).execute()
        if not res.data:
            return {"success": False, "error": "部屋が見つかりません"}, 404
        game = res.data[0]

        if game.get('player_white_token') == user_token or game.get('player_black_token') == user_token:
            return {"success": True}

        if game.get('status') != 'waiting' or game.get('player_black_token'):
            return {"success": False, "error": "この部屋にはもう参加できません"}, 400

        supabase.table('chess_games').update({
            'player_black_token': user_token,
            'player_black_name': player_name,
            'status': 'playing'
        }).eq('room_code', room_code).execute()
    except Exception as e:
        print(f"チェス参加エラー: {e}")
        return {"success": False, "error": "データベースエラーが発生しました"}, 500

    response = make_response({"success": True})
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/chess/<room_code>/move', methods=['POST'])
def chess_move(room_code):
    user_token, _ = get_or_create_user_token()
    room_code = room_code.upper()
    data = request.get_json(silent=True) or {}
    fr, fc, tr, tc = data.get('from_row'), data.get('from_col'), data.get('to_row'), data.get('to_col')

    if None in (fr, fc, tr, tc):
        return {"success": False, "error": "不正なリクエストです"}, 400

    try:
        res = supabase.table('chess_games').select('*').eq('room_code', room_code).execute()
        if not res.data:
            return {"success": False, "error": "部屋が見つかりません"}, 404
        game = res.data[0]

        if game.get('status') != 'playing':
            return {"success": False, "error": "対局中ではありません"}, 400

        my_color = 'w' if game.get('player_white_token') == user_token else ('b' if game.get('player_black_token') == user_token else None)
        if my_color != game.get('turn'):
            return {"success": False, "error": "あなたの手番ではありません"}, 400

        board = json.loads(game['board'])
        castling = game.get('castling') or ''
        en_passant_raw = game.get('en_passant')
        en_passant = tuple(json.loads(en_passant_raw)) if en_passant_raw else None

        legal = chess_legal_moves_for(board, my_color, castling, en_passant)
        matched = next((m for m in legal if m[0]==fr and m[1]==fc and m[2]==tr and m[3]==tc), None)
        if not matched:
            return {"success": False, "error": "その手は指せません"}, 400

        _, _, _, _, extra = matched
        new_board, new_castling, new_en_passant = chess_apply_move(board, castling, fr, fc, tr, tc, extra)

        next_turn = 'b' if my_color == 'w' else 'w'
        status_check = chess_game_status(new_board, next_turn, new_castling, new_en_passant)

        update_payload = {
            'board': json.dumps(new_board),
            'turn': next_turn,
            'castling': new_castling,
            'en_passant': json.dumps(new_en_passant) if new_en_passant else None,
            'updated_at': datetime.utcnow().isoformat()
        }
        if status_check == 'checkmate':
            update_payload['status'] = 'finished'
            update_payload['winner'] = my_color
        elif status_check == 'stalemate':
            update_payload['status'] = 'finished'
            update_payload['winner'] = 'draw'

        supabase.table('chess_games').update(update_payload).eq('room_code', room_code).execute()
    except Exception as e:
        print(f"チェス着手エラー: {e}")
        return {"success": False, "error": "データベースエラーが発生しました"}, 500

    return {"success": True}

@app.route('/api/chess/<room_code>/state')
def chess_state(room_code):
    user_token, _ = get_or_create_user_token()
    room_code = room_code.upper()
    try:
        res = supabase.table('chess_games').select('*').eq('room_code', room_code).execute()
        if not res.data:
            return {"error": "not found"}, 404
        game = res.data[0]
    except Exception as e:
        print(f"チェス状態取得エラー: {e}")
        return {"error": "server error"}, 500

    white_token = game.get('player_white_token')
    black_token = game.get('player_black_token')
    my_color = 'w' if white_token == user_token else ('b' if black_token == user_token else None)

    board = json.loads(game['board'])
    in_check_color = None
    if game.get('status') == 'playing':
        if chess_in_check(board, 'w'):
            in_check_color = 'w'
        elif chess_in_check(board, 'b'):
            in_check_color = 'b'

    return {
        "board": board,
        "turn": game['turn'],
        "status": game['status'],
        "winner": game.get('winner'),
        "has_black": bool(black_token),
        "my_color": my_color,
        "in_check": in_check_color,
        "white_name": game.get('player_white_name') or '名無しさん',
        "black_name": game.get('player_black_name') or '名無しさん',
        "white_id": get_daily_user_id(white_token) if white_token else None,
        "black_id": get_daily_user_id(black_token) if black_token else None
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
