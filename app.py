from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime, timedelta
import json
import html
import os
import hashlib
import uuid
import time
import re
import random
from urllib.parse import unquote
import httpx
import boto3
import psutil
import secrets
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import uvicorn

load_dotenv()

app = FastAPI()

# --- Jinja2テンプレート ---
templates = Jinja2Templates(directory="templates")

# --- 静的ファイル配信（Flaskの /static 相当） ---
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345')

# --- セッション（Flaskのsession相当。itsdangerousで署名したクッキーに保存） ---
app.add_middleware(SessionMiddleware, secret_key=FLASK_SECRET_KEY)

# Nginx1台のみの場合: --proxy-headers 付きでuvicornを起動し forwarded-allow-ips を設定
# Cloudflare + Nginx の場合も同様。X-Forwarded-*の解決はASGIサーバー側(uvicorn --proxy-headers)
# もしくはリバースプロキシ側で行う。アプリ側はCF-Connecting-IPを直接信頼する実装のまま。

psutil.cpu_percent(interval=None)


# --- ウェブソケット接続管理（スレッドごとの新着レス配信） ---
# 注意: gunicornのワーカーは1つ(--workers 1)であることが前提。
# ワーカーが複数だと、書き込みを受けたワーカーと接続を持つワーカーが別プロセスになり、
# このメモリ上の管理だけでは他ワーカーの接続者に配信できない。
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, list[WebSocket]] = {}

    async def connect(self, thread_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(thread_id, []).append(websocket)

    def disconnect(self, thread_id: int, websocket: WebSocket):
        conns = self.active_connections.get(thread_id)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                del self.active_connections[thread_id]

    async def broadcast(self, thread_id: int, reply: dict):
        conns = list(self.active_connections.get(thread_id, []))
        if not conns:
            return
        dead = []
        for ws in conns:
            try:
                await ws.send_json({"type": "new_reply", "reply": reply})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(thread_id, ws)


manager = ConnectionManager()


@app.websocket('/ws/thread/{thread_id}')
async def thread_ws(websocket: WebSocket, thread_id: int):
    await manager.connect(thread_id, websocket)
    try:
        while True:
            # クライアント側からは基本何も送ってこない想定。接続維持のためだけに待ち受ける
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(thread_id, websocket)
    except Exception:
        manager.disconnect(thread_id, websocket)


def json_resp(content, status_code: int = 200):
    return JSONResponse(content=content, status_code=status_code)


def text_resp(content, status_code: int = 200):
    return HTMLResponse(content=content, status_code=status_code)


async def get_json_silent(request: Request):
    try:
        return await request.json()
    except Exception:
        return {}


# --- HEADリクエストに常に200を返す（Flaskのbefore_request相当） ---
@app.middleware("http")
async def response_to_uptimerobot(request: Request, call_next):
    if request.method == 'HEAD':
        return Response(content='', status_code=200)
    return await call_next(request)


# --- スレッドのカテゴリ定義（value, 表示ラベル, バッジ配色キー） ---
THREAD_CATEGORIES = [
    ('announcement', 'お知らせ', 'red'),
    ('chat',         '雑談',      'blue'),
    ('tech',         '技術・学問', 'purple'),
    ('ops',          '運営・要望', 'slate'),
    ('anime',        'アニメ・漫画', 'pink'),
    ('gadget',       'ガジェット', 'teal'),
    ('ai_it',        'AI・IT',    'indigo'),
    ('game',         'ゲーム',    'amber'),
    ('other',        'その他',    'gray'),
]
THREAD_CATEGORY_VALUES = {c[0] for c in THREAD_CATEGORIES}
THREAD_CATEGORY_LABELS = {c[0]: c[1] for c in THREAD_CATEGORIES}
THREAD_CATEGORY_COLORS = {c[0]: c[2] for c in THREAD_CATEGORIES}
DEFAULT_THREAD_CATEGORY = 'other'

# --- スレッド並び替えの定義（value, 表示ラベル, ORDER BY句） ---
# ORDER BY句はホワイトリストの定数のみを使うため、SQLインジェクションの心配はない
THREAD_SORT_OPTIONS = [
    ('latest_activity', '最終更新順',              'last_activity DESC'),
    ('id_desc',          'スレ番号（最新順）',       't.id DESC'),
    ('id_asc',            'スレ番号（#1から順）',    't.id ASC'),
    ('replies_desc',      'レス数が多い順',          'replies_count DESC'),
    ('viewers_desc',      '閲覧人数順',              'thread_active_count DESC'),
]
THREAD_SORT_SQL = {s[0]: s[2] for s in THREAD_SORT_OPTIONS}
DEFAULT_THREAD_SORT = 'latest_activity'

# --- Cloudflare D1 接続設定 ---
CF_D1_ACCOUNT_ID = os.environ.get('CF_D1_ACCOUNT_ID')
CF_D1_DATABASE_ID = os.environ.get('CF_D1_DATABASE_ID')
CF_D1_API_TOKEN = os.environ.get('CF_D1_API_TOKEN')


def query_d1(sql, params=None):
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


# ---- サーバー状況（CPU/メモリ/ネットワーク）計測用 ----
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


CF_SHARED_SECRET = os.environ.get('CF_SHARED_SECRET')

s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    region_name='auto'
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'bbs-images')
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL')

# --- メール送信設定(Resend) ---
RESEND_API_KEY = os.environ.get('RESEND_API_KEY')
RESEND_FROM_EMAIL = os.environ.get('RESEND_FROM_EMAIL', 'noreply@example.com')
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'http://localhost:8080')


async def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Resend API経由でメールを送信する。失敗してもアプリは落とさずFalseを返す。"""
    if not RESEND_API_KEY:
        print(f"[メール送信スキップ] RESEND_API_KEY未設定 -> {to_email}: {subject}")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": RESEND_FROM_EMAIL,
                    "to": [to_email],
                    "subject": subject,
                    "html": html_body,
                },
            )
            if resp.status_code >= 400:
                print(f"Resend送信エラー: {resp.status_code} {resp.text}")
                return False
            return True
    except Exception as e:
        print(f"メール送信例外: {e}")
        return False


LAST_THREAD_TIMES = {}
LAST_REPLY_TIMES = {}
LAST_REPLY_SIGNATURES = {}


def get_daily_user_id(ip_address):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]


def resolve_op_user_id(thread_row: dict):
    """スレの表示ID(ID:xxxxx)を決定する。
    ログイン会員が立てたスレはthreads.user_idに固定IDが保存されているのでそれを使う。
    それが無い(ログイン機能導入前の古いスレ)場合のみ、従来通りIPから日替わりIDを計算する。"""
    if not thread_row:
        return None
    stored = thread_row.get('user_id')
    if stored:
        return stored
    ip = thread_row.get('ip_address')
    return get_daily_user_id(ip) if ip else None


def get_client_ip(request: Request):
    # サーバーIPへの直接アクセスは不可にしてあるため、リクエストは必ず
    # Cloudflareを経由する。よってCF-Connecting-IP(Cloudflareが書き換える
    # 正規のクライアントIP)をそのまま信頼してよい。
    ip = request.headers.get('CF-Connecting-IP')

    # CF-Connecting-IPが無い場合(ローカル開発環境など)のフォールバック。
    # uvicornを--proxy-headersで起動していれば request.client.host は
    # 正しく解決されたクライアントIPになる。
    if not ip:
        ip = request.client.host if request.client else None

    return ip


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


def get_staff_role(request: Request):
    return request.session.get('staff_role')


def can_manage_board(request: Request):
    return request.session.get('staff_role') in ['admin', 'sub_admin']


# =========================
# 会員ログイン機能(スレ立てに必要な一般ユーザーアカウント)
# staff_role(運営)とは別枠。session内のキーも member_ で分けて衝突を避ける。
# =========================

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,20}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
TOKEN_EXPIRE_HOURS_VERIFY = 24
TOKEN_EXPIRE_HOURS_RESET = 1


def get_profile_username(user_id_value):
    """投稿のuser_id(表示ID)が会員のpublic_idと一致するなら、そのユーザー名を返す(ゲスト/STAFFならNone)。"""
    if not user_id_value or user_id_value == 'STAFF':
        return None
    res = query_d1("SELECT username FROM users WHERE public_id = ?", [user_id_value])
    return res[0]['username'] if res else None


def get_profile_usernames_map(user_id_values):
    """複数のuser_idをまとめて会員名に解決する(スレ表示時の一括取得用)。"""
    ids = [v for v in set(user_id_values) if v and v != 'STAFF']
    if not ids:
        return {}
    placeholders = ','.join(['?'] * len(ids))
    rows = query_d1(f"SELECT public_id, username FROM users WHERE public_id IN ({placeholders})", ids)
    return {row['public_id']: row['username'] for row in rows} if rows else {}


def get_current_member(request: Request):
    """ログイン中の会員情報をsessionから取得(未ログインならNone)。"""
    member_id = request.session.get('member_id')
    if not member_id:
        return None
    return {
        'id': member_id,
        'username': request.session.get('member_username'),
    }


def is_member_logged_in(request: Request) -> bool:
    return bool(request.session.get('member_id'))


def _generate_public_id() -> str:
    """投稿に表示される「ID:xxxxxxxx」用の、アカウントに紐づく固定ランダムID。
    重複はほぼあり得ないが、念のため既存と衝突しないことを確認する。"""
    for _ in range(5):
        candidate = secrets.token_hex(4)
        existing = query_d1("SELECT id FROM users WHERE public_id = ?", [candidate])
        if not existing:
            return candidate
    return secrets.token_hex(6)


def get_member_public_id(request: Request):
    """ログイン中会員の固定表示ID。旧アカウント(public_id未発行)の場合はここで発行して保存する。"""
    if not is_member_logged_in(request):
        return None
    cached = request.session.get('member_public_id')
    if cached:
        return cached
    member_id = request.session.get('member_id')
    try:
        res = query_d1("SELECT public_id FROM users WHERE id = ?", [member_id])
        public_id = res[0]['public_id'] if res else None
        if not public_id:
            public_id = _generate_public_id()
            query_d1("UPDATE users SET public_id = ? WHERE id = ?", [public_id, member_id])
        request.session['member_public_id'] = public_id
        return public_id
    except Exception as e:
        print(f"public_id取得エラー: {e}")
        return None


def _make_token() -> str:
    return secrets.token_urlsafe(32)


def _issue_token(user_id: int, purpose: str, expire_hours: int) -> str:
    token = _make_token()
    expires_at = (datetime.utcnow() + timedelta(hours=expire_hours)).isoformat()
    query_d1(
        "INSERT INTO email_tokens (user_id, token, purpose, expires_at, used) VALUES (?, ?, ?, ?, 0)",
        [user_id, token, purpose, expires_at]
    )
    return token


def _consume_token(token: str, purpose: str):
    """有効なトークンならユーザー行を返し、usedを1に更新する。無効ならNone。"""
    res = query_d1(
        "SELECT * FROM email_tokens WHERE token = ? AND purpose = ? AND used = 0",
        [token, purpose]
    )
    if not res:
        return None
    row = res[0]
    try:
        expires_at = datetime.fromisoformat(row['expires_at'])
    except Exception:
        return None
    if datetime.utcnow() > expires_at:
        return None
    query_d1("UPDATE email_tokens SET used = 1 WHERE id = ?", [row['id']])
    user_res = query_d1("SELECT * FROM users WHERE id = ?", [row['user_id']])
    return user_res[0] if user_res else None


@app.get('/login_secret_8823')
async def staff_login_form():
    return HTMLResponse('''
        <form method="post">
            ID: <input type="text" name="username"><br>
            PW: <input type="password" name="password"><br>
            <input type="submit" value="Enter">
        </form>
    ''')


def ensure_staff_member_link(staff_id, staff_name):
    """スタッフアカウントにも会員としてのプロフィール・ゲームランキング参加ができるよう、
    usersテーブルに紐付けアカウントを自動発行する(初回スタッフログイン時のみ)。"""
    res = query_d1("SELECT linked_user_id FROM staff_users WHERE id = ?", [staff_id])
    linked_id = res[0]['linked_user_id'] if res else None
    if linked_id:
        user_res = query_d1("SELECT id, username, public_id FROM users WHERE id = ?", [linked_id])
        if user_res:
            return user_res[0]

    base_username = (staff_name or f"staff{staff_id}").strip() or f"staff{staff_id}"
    username = base_username
    suffix = 1
    while query_d1("SELECT id FROM users WHERE username = ?", [username]):
        suffix += 1
        username = f"{base_username}{suffix}"

    public_id = _generate_public_id()
    random_password_hash = generate_password_hash(secrets.token_urlsafe(16))
    query_d1(
        "INSERT INTO users (username, password_hash, email, email_verified, public_id) VALUES (?, ?, NULL, 0, ?)",
        [username, random_password_hash, public_id]
    )
    new_user_res = query_d1("SELECT id, username, public_id FROM users WHERE username = ?", [username])
    new_user = new_user_res[0]
    query_d1("UPDATE staff_users SET linked_user_id = ? WHERE id = ?", [new_user['id'], staff_id])
    return new_user


@app.post('/login_secret_8823')
async def staff_login(request: Request):
    form = await request.form()
    username = form.get('username')
    password = form.get('password')
    try:
        res = query_d1("SELECT * FROM staff_users WHERE username = ?", [username])
        if res:
            user = res[0]
            if user['password'] == password:
                request.session['staff_id'] = user['id']
                request.session['staff_role'] = user['role']
                request.session['staff_name'] = user['display_name']

                linked_member = ensure_staff_member_link(user['id'], user['display_name'])
                request.session['member_id'] = linked_member['id']
                request.session['member_username'] = linked_member['username']
                request.session['member_public_id'] = linked_member['public_id']

                return RedirectResponse(url='/', status_code=303)
    except Exception as e:
        print(f"Login error: {e}")
    return text_resp("ログイン失敗", 401)


@app.get('/staff_logout')
async def staff_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url='/')


@app.get('/register')
async def register_form(request: Request):
    if is_member_logged_in(request):
        return RedirectResponse(url='/')
    return templates.TemplateResponse(request, 'register.html', {'error': None})


@app.post('/register')
async def register_submit(request: Request):
    form = await request.form()
    username = (form.get('username') or '').strip()
    password = form.get('password') or ''
    password_confirm = form.get('password_confirm') or ''
    email = (form.get('email') or '').strip()

    def render_error(msg):
        return templates.TemplateResponse(request, 'register.html', {'error': msg}, status_code=400)

    if not USERNAME_RE.match(username):
        return render_error('ユーザー名は半角英数字とアンダースコアで3〜20文字にしてください。')
    if len(password) < 8:
        return render_error('パスワードは8文字以上にしてください。')
    if password != password_confirm:
        return render_error('パスワードが一致しません。')
    if email and not EMAIL_RE.match(email):
        return render_error('メールアドレスの形式が正しくありません。')

    try:
        existing = query_d1("SELECT id FROM users WHERE username = ?", [username])
        if existing:
            return render_error('そのユーザー名はすでに使われています。')
        if email:
            existing_email = query_d1("SELECT id FROM users WHERE email = ?", [email])
            if existing_email:
                return render_error('そのメールアドレスはすでに登録されています。')

        password_hash = generate_password_hash(password)
        public_id = _generate_public_id()
        query_d1(
            "INSERT INTO users (username, password_hash, email, email_verified, public_id) VALUES (?, ?, ?, 0, ?)",
            [username, password_hash, email or None, public_id]
        )
        new_user_res = query_d1("SELECT * FROM users WHERE username = ?", [username])
        if not new_user_res:
            return render_error('登録に失敗しました。もう一度お試しください。')
        new_user = new_user_res[0]
    except Exception as e:
        print(f"会員登録エラー: {e}")
        return render_error('データベースエラーが発生しました。')

    if email:
        token = _issue_token(new_user['id'], 'verify', TOKEN_EXPIRE_HOURS_VERIFY)
        verify_url = f"{SITE_BASE_URL.rstrip('/')}/verify_email/{token}"
        await send_email(
            email,
            "【掲示板】メールアドレスの確認",
            f'<p>{html.escape(username)} 様</p>'
            f'<p>ご登録ありがとうございます。以下のリンクからメールアドレスを確認してください(24時間有効)。</p>'
            f'<p><a href="{verify_url}">{verify_url}</a></p>'
        )

    request.session['member_id'] = new_user['id']
    request.session['member_username'] = new_user['username']
    request.session['member_public_id'] = public_id
    return RedirectResponse(url='/', status_code=303)


@app.get('/login')
async def member_login_form(request: Request):
    if is_member_logged_in(request):
        return RedirectResponse(url='/')
    return templates.TemplateResponse(request, 'login.html', {'error': None})


@app.post('/login')
async def member_login_submit(request: Request):
    form = await request.form()
    username = (form.get('username') or '').strip()
    password = form.get('password') or ''

    try:
        res = query_d1("SELECT * FROM users WHERE username = ?", [username])
    except Exception as e:
        print(f"ログインエラー: {e}")
        res = []

    user = res[0] if res else None
    if not user or not check_password_hash(user['password_hash'], password):
        return templates.TemplateResponse(
            request, 'login.html', {'error': 'ユーザー名またはパスワードが違います。'}, status_code=401
        )

    request.session['member_id'] = user['id']
    request.session['member_username'] = user['username']
    return RedirectResponse(url='/', status_code=303)


@app.get('/logout')
async def member_logout(request: Request):
    request.session.pop('member_id', None)
    request.session.pop('member_username', None)
    return RedirectResponse(url='/')


@app.get('/verify_email/{token}')
async def verify_email(request: Request, token: str):
    user = _consume_token(token, 'verify')
    if not user:
        return text_resp("確認リンクが無効か、有効期限が切れています。", 400)
    try:
        query_d1("UPDATE users SET email_verified = 1 WHERE id = ?", [user['id']])
    except Exception as e:
        print(f"メール確認エラー: {e}")
        return text_resp("データベースエラーが発生しました。", 500)
    return templates.TemplateResponse(request, 'email_verified.html', {})


@app.get('/forgot_password')
async def forgot_password_form(request: Request):
    return templates.TemplateResponse(request, 'forgot_password.html', {'sent': False})


@app.post('/forgot_password')
async def forgot_password_submit(request: Request):
    form = await request.form()
    email = (form.get('email') or '').strip()

    # メール登録の有無をユーザーに教えないため、結果に関わらず同じ成功画面を返す
    if email:
        try:
            res = query_d1("SELECT * FROM users WHERE email = ?", [email])
        except Exception as e:
            print(f"パスワードリセット検索エラー: {e}")
            res = []
        if res:
            user = res[0]
            token = _issue_token(user['id'], 'reset', TOKEN_EXPIRE_HOURS_RESET)
            reset_url = f"{SITE_BASE_URL.rstrip('/')}/reset_password/{token}"
            await send_email(
                email,
                "【掲示板】パスワード再設定",
                f'<p>{html.escape(user["username"])} 様</p>'
                f'<p>以下のリンクからパスワードを再設定してください(1時間有効)。</p>'
                f'<p><a href="{reset_url}">{reset_url}</a></p>'
                f'<p>心当たりがない場合は、このメールは無視してください。</p>'
            )

    return templates.TemplateResponse(request, 'forgot_password.html', {'sent': True})


@app.get('/reset_password/{token}')
async def reset_password_form(request: Request, token: str):
    return templates.TemplateResponse(request, 'reset_password.html', {'token': token, 'error': None})


@app.post('/reset_password/{token}')
async def reset_password_submit(request: Request, token: str):
    form = await request.form()
    password = form.get('password') or ''
    password_confirm = form.get('password_confirm') or ''

    if len(password) < 8:
        return templates.TemplateResponse(
            request, 'reset_password.html',
            {'token': token, 'error': 'パスワードは8文字以上にしてください。'}, status_code=400
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request, 'reset_password.html',
            {'token': token, 'error': 'パスワードが一致しません。'}, status_code=400
        )

    user = _consume_token(token, 'reset')
    if not user:
        return templates.TemplateResponse(
            request, 'reset_password.html',
            {'token': token, 'error': 'リンクが無効か、有効期限が切れています。もう一度パスワード再設定をお試しください。'},
            status_code=400
        )

    try:
        query_d1("UPDATE users SET password_hash = ? WHERE id = ?", [generate_password_hash(password), user['id']])
    except Exception as e:
        print(f"パスワード更新エラー: {e}")
        return templates.TemplateResponse(
            request, 'reset_password.html',
            {'token': token, 'error': 'データベースエラーが発生しました。'}, status_code=500
        )

    return RedirectResponse(url='/login', status_code=303)


NG_WORDS = {
    'ﾀﾋ': 'タヒ',
    '死': 'タヒ',
    '死​ね': '〇ね',
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
    'シコシコ': '4545',
    'オナニー': '0721',
    '射精': '身寸米青',
    '精子': '米青子',
}


def filter_ng_words(text):
    if not text:
        return text
    for ng_word, replaced_word in NG_WORDS.items():
        if ng_word in text:
            text = text.replace(ng_word, replaced_word)
    return text


def update_and_get_user_counts(current_token, location):
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


@app.get('/api/lobby/active_count')
async def api_lobby_active_count(request: Request):
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True
    count = update_and_get_user_counts(user_token, "lobby")
    resp = json_resp({'success': True, 'active_count': count, 'count': count})
    if is_new_user:
        resp.set_cookie('user_bbs_token', user_token, max_age=60 * 60 * 24 * 365, httponly=True)
    return resp


@app.get('/privacy')
async def privacy(request: Request):
    return templates.TemplateResponse(request, 'privacy.html', {})


@app.get('/terms')
async def terms(request: Request):
    return templates.TemplateResponse(request, 'terms.html', {})


@app.get('/roles')
async def roles(request: Request):
    return templates.TemplateResponse(request, 'roles.html', {})


@app.get('/rankings')
async def rankings(request: Request):
    def top_n(game, n=30):
        return query_d1(
            "SELECT display_name, rating, wins, losses, draws FROM game_ratings "
            "WHERE game = ? ORDER BY rating DESC LIMIT ?",
            [game, n]
        ) or []

    return templates.TemplateResponse(request, 'rankings.html', {
        'othello_ranking': top_n('othello'),
        'chess_ranking': top_n('chess'),
        'shogi_ranking': top_n('shogi'),
    })


@app.get('/profile/{public_id}')
async def profile_view(request: Request, public_id: str):
    res = query_d1("SELECT id, username, public_id, bio, created_at FROM users WHERE public_id = ?", [public_id])
    if not res:
        return text_resp("そのユーザーは見つかりませんでした。", 404)
    profile_user = res[0]
    member_key = f"member:{profile_user['id']}"
    games = {}
    for g in ('othello', 'chess', 'shogi'):
        gr = query_d1("SELECT rating, wins, losses, draws FROM game_ratings WHERE player_key = ? AND game = ?", [member_key, g])
        games[g] = gr[0] if gr else None

    current_member = get_current_member(request)
    is_own_profile = bool(current_member) and str(current_member['id']) == str(profile_user['id'])

    return templates.TemplateResponse(request, 'profile.html', {
        'profile_user': profile_user,
        'games': games,
        'is_own_profile': is_own_profile,
    })


@app.get('/profile/edit')
async def profile_edit_form(request: Request):
    if not is_member_logged_in(request):
        return RedirectResponse(url='/login')
    member_id = request.session.get('member_id')
    res = query_d1("SELECT username, bio FROM users WHERE id = ?", [member_id])
    user = res[0] if res else {'username': request.session.get('member_username'), 'bio': ''}
    return templates.TemplateResponse(request, 'profile_edit.html', {'user': user, 'error': None})


@app.post('/profile/edit')
async def profile_edit_submit(request: Request):
    if not is_member_logged_in(request):
        return RedirectResponse(url='/login')
    member_id = request.session.get('member_id')
    form = await request.form()
    username = (form.get('username') or '').strip()
    bio = html.escape((form.get('bio') or '').strip()[:200])

    def render_error(msg):
        return templates.TemplateResponse(
            request, 'profile_edit.html',
            {'user': {'username': username, 'bio': bio}, 'error': msg}, status_code=400
        )

    if not USERNAME_RE.match(username):
        return render_error('ユーザー名は半角英数字とアンダースコアで3〜20文字にしてください。')

    try:
        existing = query_d1("SELECT id FROM users WHERE username = ? AND id != ?", [username, member_id])
        if existing:
            return render_error('そのユーザー名はすでに使われています。')
        query_d1("UPDATE users SET username = ?, bio = ? WHERE id = ?", [username, bio, member_id])
    except Exception as e:
        print(f"プロフィール更新エラー: {e}")
        return render_error('データベースエラーが発生しました。')

    request.session['member_username'] = username
    public_id = get_member_public_id(request)
    return RedirectResponse(url=f'/profile/{public_id}', status_code=303)


# =========================
# D1版 ゲーム機能（オセロ・チェス・将棋）
# =========================

def _game_token(request: Request):
    token = request.cookies.get('game_player_token') or request.cookies.get('user_bbs_token')
    if not token:
        token = str(uuid.uuid4())
    return token


async def _game_name(request: Request, default='名無しさん'):
    name = None
    content_type = request.headers.get('content-type', '')
    try:
        if 'application/json' in content_type:
            body = await request.json()
            name = body.get('name')
        else:
            form = await request.form()
            name = form.get('name')
    except Exception:
        name = None
    if not name:
        raw = request.cookies.get('bbs_saved_author')
        if raw:
            try:
                name = unquote(raw)
            except Exception:
                name = raw
    name = html.escape(str(name or default).strip())[:20]
    return name or default


def _new_room_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(30):
        code = ''.join(random.choice(alphabet) for _ in range(6))
        if (not query_d1('SELECT 1 FROM othello_rooms WHERE room_code = ? LIMIT 1', [code])
                and not query_d1('SELECT 1 FROM chess_rooms WHERE room_code = ? LIMIT 1', [code])
                and not query_d1('SELECT 1 FROM shogi_rooms WHERE room_code = ? LIMIT 1', [code])):
            return code
    return uuid.uuid4().hex[:6].upper()


# =========================
# ゲームのレーティング(Elo)・ランキング機能
# 会員はmember:<id>、ゲストはgame_player_token(実質user_bbs_token)をguest:<token>として
# 集計キーに使う。ゲストも含めて全対局を集計する。
# =========================

ELO_K = 32
ELO_DEFAULT_RATING = 1500


def _game_member_id(request: Request):
    """ログイン中ならそのmember idを文字列で返す(ゲストならNone)。部屋作成・参加時にrooms側へ保存しておく。"""
    member = get_current_member(request)
    return str(member['id']) if member else None


def _rating_key(member_id_str, token: str) -> str:
    return f"member:{member_id_str}" if member_id_str else f"guest:{token}"


def _get_or_init_rating(player_key: str, game: str, display_name: str):
    res = query_d1("SELECT * FROM game_ratings WHERE player_key = ? AND game = ?", [player_key, game])
    if res:
        return res[0]
    now = datetime.utcnow().isoformat()
    query_d1(
        "INSERT INTO game_ratings (player_key, game, display_name, rating, wins, losses, draws, updated_at) "
        "VALUES (?, ?, ?, ?, 0, 0, 0, ?)",
        [player_key, game, display_name, ELO_DEFAULT_RATING, now]
    )
    return {'player_key': player_key, 'game': game, 'display_name': display_name,
            'rating': ELO_DEFAULT_RATING, 'wins': 0, 'losses': 0, 'draws': 0}


def apply_game_result(game: str, key_a: str, name_a: str, key_b: str, name_b: str, result_a: float):
    """result_a: 1=Aの勝ち, 0=Aの負け, 0.5=引き分け。両者のEloレーティングと戦績を更新する。
    失敗してもゲーム進行自体には影響させない(集計はベストエフォート)。"""
    try:
        a = _get_or_init_rating(key_a, game, name_a)
        b = _get_or_init_rating(key_b, game, name_b)
        ra, rb = a['rating'], b['rating']
        expected_a = 1 / (1 + 10 ** ((rb - ra) / 400))
        result_b = 1 - result_a
        new_ra = round(ra + ELO_K * (result_a - expected_a))
        new_rb = round(rb + ELO_K * (result_b - (1 - expected_a)))

        def bump(row, result):
            wins, losses, draws = row['wins'], row['losses'], row['draws']
            if result == 1:
                wins += 1
            elif result == 0:
                losses += 1
            else:
                draws += 1
            return wins, losses, draws

        wa, la, da = bump(a, result_a)
        wb, lb, db = bump(b, result_b)
        now = datetime.utcnow().isoformat()
        query_d1(
            "UPDATE game_ratings SET rating=?, wins=?, losses=?, draws=?, display_name=?, updated_at=? "
            "WHERE player_key=? AND game=?",
            [new_ra, wa, la, da, name_a, now, key_a, game]
        )
        query_d1(
            "UPDATE game_ratings SET rating=?, wins=?, losses=?, draws=?, display_name=?, updated_at=? "
            "WHERE player_key=? AND game=?",
            [new_rb, wb, lb, db, name_b, now, key_b, game]
        )
    except Exception as e:
        print(f"レーティング更新エラー({game}): {e}")


def _initial_othello():
    b = ['.'] * 64
    b[3 * 8 + 3] = 'W'; b[3 * 8 + 4] = 'B'; b[4 * 8 + 3] = 'B'; b[4 * 8 + 4] = 'W'
    return ''.join(b)


def _othello_valid(board, player):
    opp = 'W' if player == 'B' else 'B'
    dirs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    out = []
    for r in range(8):
        for c in range(8):
            if board[r * 8 + c] != '.':
                continue
            ok = False
            for dr, dc in dirs:
                rr, cc = r + dr, c + dc; seen = False
                while 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == opp:
                    seen = True; rr += dr; cc += dc
                if seen and 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == player:
                    ok = True; break
            if ok:
                out.append((r, c))
    return out


def _othello_apply(board, player, r, c):
    if (r, c) not in _othello_valid(board, player):
        return None
    a = list(board); a[r * 8 + c] = player; opp = 'W' if player == 'B' else 'B'
    dirs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for dr, dc in dirs:
        rr, cc = r + dr, c + dc; flips = []
        while 0 <= rr < 8 and 0 <= cc < 8 and a[rr * 8 + cc] == opp:
            flips.append((rr, cc)); rr += dr; cc += dc
        if flips and 0 <= rr < 8 and 0 <= cc < 8 and a[rr * 8 + cc] == player:
            for fr, fc in flips:
                a[fr * 8 + fc] = player
    return ''.join(a)


def _initial_chess():
    # 64要素のリストを作成し、JSON文字列にシリアライズして返す
    board_list = [
        'bR', 'bN', 'bB', 'bQ', 'bK', 'bB', 'bN', 'bR',
        'bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        'wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP',
        'wR', 'wN', 'wB', 'wQ', 'wK', 'wB', 'wN', 'wR'
    ]
    return json.dumps(board_list)


def _chess_board():
    return _initial_chess()


def _chess_pseudo(board, r, c, castling='', en_passant=None):
    p = board[r * 8 + c]
    if not p:
        return []
    color, typ = p[0], p[1]; out = []
    dirs = []
    if typ == 'N':
        dirs = [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]
    elif typ == 'K':
        dirs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    elif typ in 'BRQ':
        if typ in 'BQ':
            dirs += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
        if typ in 'RQ':
            dirs += [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if typ in 'NK':
        for dr, dc in dirs:
            rr, cc = r + dr, c + dc
            if 0 <= rr < 8 and 0 <= cc < 8 and (not board[rr * 8 + cc] or board[rr * 8 + cc][0] != color):
                out.append((rr, cc))
    elif typ in 'BRQ':
        for dr, dc in dirs:
            rr, cc = r + dr, c + dc
            while 0 <= rr < 8 and 0 <= cc < 8:
                t = board[rr * 8 + cc]
                if not t:
                    out.append((rr, cc))
                else:
                    if t[0] != color:
                        out.append((rr, cc))
                    break
                rr += dr; cc += dc
    elif typ == 'P':
        d = -1 if color == 'w' else 1; start = 6 if color == 'w' else 1
        rr = r + d
        if 0 <= rr < 8 and not board[rr * 8 + c]:
            out.append((rr, c))
            rr2 = r + 2 * d
            if r == start and not board[rr2 * 8 + c]:
                out.append((rr2, c))
        for dc in (-1, 1):
            rr, cc = r + d, c + dc
            if 0 <= rr < 8 and 0 <= cc < 8:
                if board[rr * 8 + cc] and board[rr * 8 + cc][0] != color:
                    out.append((rr, cc))
                elif en_passant and en_passant == (rr, cc):
                    out.append((rr, cc))

    if typ == 'K':
        row = 7 if color == 'w' else 0
        if r == row and c == 4:
            k_flag = 'K' if color == 'w' else 'k'
            q_flag = 'Q' if color == 'w' else 'q'
            opp = 'b' if color == 'w' else 'w'
            if (k_flag in castling and not board[row * 8 + 5] and not board[row * 8 + 6]
                    and board[row * 8 + 7] == color + 'R'
                    and not _chess_attacked(board, row, 4, opp)
                    and not _chess_attacked(board, row, 5, opp)
                    and not _chess_attacked(board, row, 6, opp)):
                out.append((row, 6))
            if (q_flag in castling and not board[row * 8 + 3] and not board[row * 8 + 2] and not board[row * 8 + 1]
                    and board[row * 8 + 0] == color + 'R'
                    and not _chess_attacked(board, row, 4, opp)
                    and not _chess_attacked(board, row, 3, opp)
                    and not _chess_attacked(board, row, 2, opp)):
                out.append((row, 2))
    return out


def _chess_find_king(board, color):
    target = color + 'K'
    for i, p in enumerate(board):
        if p == target:
            return i // 8, i % 8
    return None


def _chess_attacked(board, r, c, by_color):
    # ポーンの攻撃
    d = 1 if by_color == 'w' else -1
    for dc in (-1, 1):
        pr, pc = r + d, c + dc
        if 0 <= pr < 8 and 0 <= pc < 8 and board[pr * 8 + pc] == by_color + 'P':
            return True
    # ナイトの攻撃
    for dr, dc in [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]:
        rr, cc = r + dr, c + dc
        if 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == by_color + 'N':
            return True
    # 王の攻撃(隣接マス)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, c + dc
            if 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == by_color + 'K':
                return True
    # 直線(ルーク・クイーン)
    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        rr, cc = r + dr, c + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            p = board[rr * 8 + cc]
            if p:
                if p[0] == by_color and p[1] in ('R', 'Q'):
                    return True
                break
            rr += dr; cc += dc
    # 斜め(ビショップ・クイーン)
    for dr, dc in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
        rr, cc = r + dr, c + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            p = board[rr * 8 + cc]
            if p:
                if p[0] == by_color and p[1] in ('B', 'Q'):
                    return True
                break
            rr += dr; cc += dc
    return False


def _chess_in_check(board, color):
    pos = _chess_find_king(board, color)
    if not pos:
        return False
    r, c = pos
    opp = 'b' if color == 'w' else 'w'
    return _chess_attacked(board, r, c, opp)


def _chess_apply(board, r, c, tr, tc):
    """盤面をコピーして着手を適用した新しい盤面を返す(王手判定のシミュレーション用)"""
    nb = board[:]
    piece = nb[r * 8 + c]
    color, typ = piece[0], piece[1]
    nb[r * 8 + c] = ''

    if typ == 'K' and abs(tc - c) == 2:
        # キャスリング: 王が横に2マス動く手 -> ルークも一緒に動かす
        nb[tr * 8 + tc] = piece
        row = r
        if tc == 6:
            nb[row * 8 + 7] = ''
            nb[row * 8 + 5] = color + 'R'
        elif tc == 2:
            nb[row * 8 + 0] = ''
            nb[row * 8 + 3] = color + 'R'
    elif typ == 'P' and c != tc and not board[tr * 8 + tc]:
        # アンパッサン: ポーンが斜めに動いたのに移動先が空 -> 通過されたポーンを取る
        nb[tr * 8 + tc] = piece
        nb[r * 8 + tc] = ''
    elif typ == 'P' and tr in (0, 7):
        nb[tr * 8 + tc] = color + 'Q'
    else:
        nb[tr * 8 + tc] = piece
    return nb


def _chess_update_castling_rights(castling, typ, color, r, c, tr, tc):
    new_castling = castling or ''
    if typ == 'K':
        new_castling = new_castling.replace('K', '').replace('Q', '') if color == 'w' else new_castling.replace('k', '').replace('q', '')
    for (rr, cc), flag in [((7, 0), 'Q'), ((7, 7), 'K'), ((0, 0), 'q'), ((0, 7), 'k')]:
        if (r, c) == (rr, cc) or (tr, tc) == (rr, cc):
            new_castling = new_castling.replace(flag, '')
    return new_castling


def _chess_legal_moves(board, color, castling='', en_passant=None):
    """自分の王が王手にさらされる手を除いた、本当に指せる手の一覧"""
    moves = []
    for i, p in enumerate(board):
        if p and p[0] == color:
            r, c = i // 8, i % 8
            for tr, tc in _chess_pseudo(board, r, c, castling, en_passant):
                simulated = _chess_apply(board, r, c, tr, tc)
                if not _chess_in_check(simulated, color):
                    moves.append((r, c, tr, tc))
    return moves


SHOGI_HAND_TYPES = ['P', 'L', 'N', 'S', 'G', 'B', 'R']
SHOGI_PROMOTABLE = ('P', 'L', 'N', 'S', 'B', 'R')


def _shogi_empty_hands():
    return {'s': {t: 0 for t in SHOGI_HAND_TYPES}, 'g': {t: 0 for t in SHOGI_HAND_TYPES}}


def _initial_shogi_hands_json():
    return json.dumps(_shogi_empty_hands())


def _initial_shogi_board():
    # 9x9(81マス)の盤面をJSON文字列で返す。空マスは''、駒は 手番色('s'=先手/'g'=後手) + 種類 の2文字。
    # 成り駒には先頭に'+'を付ける(例: 's+R' = 先手の龍)
    board = [''] * 81
    back_rank = ['L', 'N', 'S', 'G', 'K', 'G', 'S', 'N', 'L']
    for c in range(9):
        board[0 * 9 + c] = 'g' + back_rank[c]
        board[8 * 9 + c] = 's' + back_rank[c]
        board[2 * 9 + c] = 'gP'
        board[6 * 9 + c] = 'sP'
    board[1 * 9 + 1] = 'gB'
    board[1 * 9 + 7] = 'gR'
    board[7 * 9 + 1] = 'sR'
    board[7 * 9 + 7] = 'sB'
    return json.dumps(board)


def _shogi_forward(color):
    return -1 if color == 's' else 1


def _shogi_zone(color, r):
    """成れる範囲(敵陣3段)かどうか"""
    return r <= 2 if color == 's' else r >= 6


def _shogi_forced_promotion(base_typ, color, tr):
    """そのまま進むと二度と動けなくなる駒は、強制的に成る"""
    if base_typ in ('P', 'L'):
        return tr == (0 if color == 's' else 8)
    if base_typ == 'N':
        return tr <= 1 if color == 's' else tr >= 7
    return False


def _shogi_piece_moves(board, r, c):
    """(王手を考慮しない)疑似合法手の移動先マス一覧"""
    p = board[r * 9 + c]
    if not p:
        return []
    color, typ = p[0], p[1:]
    out = []

    def step(dr, dc):
        rr, cc = r + dr, c + dc
        if 0 <= rr < 9 and 0 <= cc < 9:
            t = board[rr * 9 + cc]
            if not t or t[0] != color:
                out.append((rr, cc))

    def slide(dr, dc):
        rr, cc = r + dr, c + dc
        while 0 <= rr < 9 and 0 <= cc < 9:
            t = board[rr * 9 + cc]
            if not t:
                out.append((rr, cc))
            else:
                if t[0] != color:
                    out.append((rr, cc))
                break
            rr += dr; cc += dc

    f = _shogi_forward(color)
    if typ == 'P':
        step(f, 0)
    elif typ == 'L':
        slide(f, 0)
    elif typ == 'N':
        step(2 * f, -1); step(2 * f, 1)
    elif typ == 'S':
        for d in [(f, 0), (f, -1), (f, 1), (-f, -1), (-f, 1)]:
            step(*d)
    elif typ in ('G', '+P', '+L', '+N', '+S'):
        for d in [(f, 0), (f, -1), (f, 1), (0, -1), (0, 1), (-f, 0)]:
            step(*d)
    elif typ == 'K':
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                step(dr, dc)
    elif typ == 'B':
        for d in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            slide(*d)
    elif typ == 'R':
        for d in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            slide(*d)
    elif typ == '+B':
        for d in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            slide(*d)
        for d in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            step(*d)
    elif typ == '+R':
        for d in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            slide(*d)
        for d in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            step(*d)
    return out


def _shogi_find_king(board, color):
    target = color + 'K'
    for i, p in enumerate(board):
        if p == target:
            return i // 9, i % 9
    return None


def _shogi_attacked(board, r, c, by_color):
    for i, p in enumerate(board):
        if p and p[0] == by_color:
            rr, cc = i // 9, i % 9
            if (r, c) in _shogi_piece_moves(board, rr, cc):
                return True
    return False


def _shogi_in_check(board, color):
    pos = _shogi_find_king(board, color)
    if not pos:
        return False
    r, c = pos
    opp = 'g' if color == 's' else 's'
    return _shogi_attacked(board, r, c, opp)


def _shogi_apply_move(board, r, c, tr, tc, promote=False):
    """盤面をコピーして着手を適用した新しい盤面を返す"""
    nb = board[:]
    piece = nb[r * 9 + c]
    color, typ = piece[0], piece[1:]
    nb[r * 9 + c] = ''
    new_typ = typ
    if promote and typ in SHOGI_PROMOTABLE:
        new_typ = '+' + typ
    nb[tr * 9 + tc] = color + new_typ
    return nb


def _shogi_drop_allowed(board, color, ptype, r, c):
    if board[r * 9 + c]:
        return False
    if ptype == 'P':
        last_row = 0 if color == 's' else 8
        if r == last_row:
            return False
        for rr in range(9):
            if board[rr * 9 + c] == color + 'P':
                return False  # 二歩
    elif ptype == 'L':
        last_row = 0 if color == 's' else 8
        if r == last_row:
            return False
    elif ptype == 'N':
        if color == 's' and r <= 1:
            return False
        if color == 'g' and r >= 7:
            return False
    return True


def _shogi_legal_board_moves(board, color):
    """自分の王が王手にさらされる手を除いた、盤上の駒を動かす合法手の一覧"""
    moves = []
    for i, p in enumerate(board):
        if p and p[0] == color:
            r, c = i // 9, i % 9
            for tr, tc in _shogi_piece_moves(board, r, c):
                simulated = _shogi_apply_move(board, r, c, tr, tc, promote=False)
                if not _shogi_in_check(simulated, color):
                    moves.append((r, c, tr, tc))
    return moves


def _shogi_legal_drop_moves(board, color, hand):
    """持ち駒を打てる合法手が1つでもあるかを調べるための一覧"""
    moves = []
    for ptype, count in (hand or {}).items():
        if count <= 0:
            continue
        for i, p in enumerate(board):
            if p:
                continue
            r, c = i // 9, i % 9
            if not _shogi_drop_allowed(board, color, ptype, r, c):
                continue
            nb = board[:]
            nb[i] = color + ptype
            if not _shogi_in_check(nb, color):
                moves.append((ptype, r, c))
    return moves


def _cookie_response(request: Request, resp, token):
    if not request.cookies.get('game_player_token'):
        resp.set_cookie('game_player_token', token, max_age=60 * 60 * 24 * 365, httponly=True, samesite='Lax')
    return resp


@app.get('/games')
async def games_hub(request: Request):
    return templates.TemplateResponse(request, 'games_hub.html', {})


@app.get('/archive')
async def archive_list(request: Request):
    try:
        page = int(request.query_params.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    per_page = 20
    offset = (page - 1) * per_page
    rows = query_d1(
        "SELECT * FROM archived_threads_index ORDER BY archived_at DESC LIMIT ? OFFSET ?",
        [per_page, offset]
    )
    archived_threads = rows or []
    has_next = len(archived_threads) == per_page
    return templates.TemplateResponse(request, 'archive_list.html', {
        'archived_threads': archived_threads, 'current_page': page, 'has_next': has_next
    })


@app.get('/archive/{thread_id}')
async def archive_view(request: Request, thread_id: int):
    archive_key = f"archive/thread_{thread_id}.json"
    try:
        obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=archive_key)
        payload = json.loads(obj['Body'].read().decode('utf-8'))
    except Exception as e:
        print(f"過去ログ取得エラー(thread_id={thread_id}): {e}")
        return text_resp("この過去ログは見つかりませんでした", 404)

    thread = payload.get('thread', {})
    replies = payload.get('replies', [])

    for r in replies:
        if r.get('date'):
            try:
                dt_utc = datetime.fromisoformat(str(r['date']).replace('Z', '+00:00'))
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
        if r.get('content'):
            r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', str(r['content']))

    return templates.TemplateResponse(request, 'archive_view.html', {
        'thread': thread, 'replies': replies, 'archived_at': payload.get('archived_at')
    })


ARCHIVE_SECRET = os.environ.get('ARCHIVE_SECRET')
ARCHIVE_PINNED_IDS = [1, 2, 3, 4]


def _fetch_all_from_supabase(sb_url, sb_key, table, columns):
    """PostgRESTのRangeヘッダーでページ送りしながら全件取得する(1000件の壁を回避)"""
    all_rows = []
    page_size = 1000
    offset = 0
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
    }
    while True:
        headers["Range"] = f"{offset}-{offset + page_size - 1}"
        resp = httpx.get(
            f"{sb_url.rstrip('/')}/rest/v1/{table}",
            params={"select": columns, "order": "id.asc"},
            headers=headers,
            timeout=30
        )
        if resp.status_code not in (200, 206):
            raise Exception(f"{table}取得エラー: {resp.status_code} {resp.text[:300]}")
        rows = resp.json()
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return all_rows


def _d1_batch_insert(table, columns, rows, chunk_size):
    """複数行をまとめたINSERT OR IGNOREをchunk_size件ずつD1に流し込む"""
    inserted = 0
    placeholders_one = "(" + ",".join(["?"] * len(columns)) + ")"
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        placeholders = ",".join([placeholders_one] * len(chunk))
        sql = f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES {placeholders}"
        params = []
        for row in chunk:
            for col in columns:
                params.append(row.get(col))
        query_d1(sql, params)
        inserted += len(chunk)
    return inserted


@app.post('/internal/migrate-from-supabase')
async def migrate_from_supabase(request: Request):
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return json_resp({"error": "unauthorized"}, 403)

    sb_url = request.headers.get('X-Supabase-Url')
    sb_key = request.headers.get('X-Supabase-Key')
    if not sb_url or not sb_key:
        return json_resp({"error": "X-Supabase-Url / X-Supabase-Key ヘッダーが必要です"}, 400)

    try:
        threads = _fetch_all_from_supabase(sb_url, sb_key, 'threads', 'id,title,created_at,ip_address')
        replies = _fetch_all_from_supabase(sb_url, sb_key, 'replies', 'id,thread_id,author,content,user_id,is_admin,image_url,ip_address,date,role')
    except Exception as e:
        return json_resp({"error": f"Supabaseからの取得に失敗しました: {e}"}, 500)

    try:
        threads_inserted = _d1_batch_insert(
            'threads', ['id', 'title', 'created_at', 'ip_address'], threads, chunk_size=200
        )
        replies_inserted = _d1_batch_insert(
            'replies', ['id', 'thread_id', 'author', 'content', 'user_id', 'is_admin', 'image_url', 'ip_address', 'date', 'role'], replies, chunk_size=90
        )
    except Exception as e:
        return json_resp({"error": f"D1への書き込みに失敗しました: {e}"}, 500)

    return {
        "threads_fetched": len(threads),
        "replies_fetched": len(replies),
        "threads_inserted_or_ignored": threads_inserted,
        "replies_inserted_or_ignored": replies_inserted
    }


@app.post('/internal/migrate-from-supabase-safe')
async def migrate_from_supabase_safe(request: Request):
    # ID衝突を避けるため、今のD1の最大IDより確実に大きい番号にずらしてから追加する版
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return json_resp({"error": "unauthorized"}, 403)

    sb_url = request.headers.get('X-Supabase-Url')
    sb_key = request.headers.get('X-Supabase-Key')
    if not sb_url or not sb_key:
        return json_resp({"error": "X-Supabase-Url / X-Supabase-Key ヘッダーが必要です"}, 400)

    try:
        max_tid_res = query_d1("SELECT MAX(id) as m FROM threads", [])
        max_rid_res = query_d1("SELECT MAX(id) as m FROM replies", [])
        current_max_tid = (max_tid_res[0]['m'] if max_tid_res and max_tid_res[0]['m'] is not None else 0)
        current_max_rid = (max_rid_res[0]['m'] if max_rid_res and max_rid_res[0]['m'] is not None else 0)
    except Exception as e:
        return json_resp({"error": f"現在のD1の最大IDの取得に失敗しました: {e}"}, 500)

    thread_offset = current_max_tid + 10000
    reply_offset = current_max_rid + 10000

    try:
        threads = _fetch_all_from_supabase(sb_url, sb_key, 'threads', 'id,title,created_at,ip_address')
        replies = _fetch_all_from_supabase(sb_url, sb_key, 'replies', 'id,thread_id,author,content,user_id,is_admin,image_url,ip_address,date,role')
    except Exception as e:
        return json_resp({"error": f"Supabaseからの取得に失敗しました: {e}"}, 500)

    # ID・thread_idをまとめてずらす
    for t in threads:
        t['id'] = t['id'] + thread_offset
    for r in replies:
        r['id'] = r['id'] + reply_offset
        r['thread_id'] = r['thread_id'] + thread_offset

    try:
        threads_inserted = _d1_batch_insert(
            'threads', ['id', 'title', 'created_at', 'ip_address'], threads, chunk_size=200
        )
        replies_inserted = _d1_batch_insert(
            'replies', ['id', 'thread_id', 'author', 'content', 'user_id', 'is_admin', 'image_url', 'ip_address', 'date', 'role'], replies, chunk_size=90
        )
    except Exception as e:
        return json_resp({"error": f"D1への書き込みに失敗しました: {e}"}, 500)

    return {
        "thread_offset": thread_offset,
        "reply_offset": reply_offset,
        "threads_fetched": len(threads),
        "replies_fetched": len(replies),
        "threads_inserted_or_ignored": threads_inserted,
        "replies_inserted_or_ignored": replies_inserted
    }


@app.post('/internal/rebuild-archive-index')
async def rebuild_archive_index(request: Request):
    # R2に実在するJSONから、D1の索引テーブル(archived_threads_index)を作り直す
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return json_resp({"error": "unauthorized"}, 403)

    rebuilt = []
    errors = []

    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        keys = []
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix='archive/'):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    keys.append(obj['Key'])
    except Exception as e:
        return json_resp({"error": f"R2一覧の取得に失敗しました: {e}"}, 500)

    for key in keys:
        try:
            m = re.search(r'thread_(\d+)\.json$', key)
            if not m:
                continue
            tid = int(m.group(1))

            obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
            payload = json.loads(obj['Body'].read().decode('utf-8'))

            title = payload.get('thread', {}).get('title', '(無題)')
            reply_count = len(payload.get('replies', []))
            archived_at = payload.get('archived_at') or datetime.utcnow().isoformat()

            query_d1(
                "INSERT OR REPLACE INTO archived_threads_index (thread_id, title, reply_count, archived_at) VALUES (?, ?, ?, ?)",
                [tid, title, reply_count, archived_at]
            )
            rebuilt.append(tid)
        except Exception as e:
            errors.append({"key": key, "error": str(e)})
            print(f"索引再構築エラー({key}): {e}")

    return {"rebuilt_count": len(rebuilt), "rebuilt_thread_ids": rebuilt, "errors": errors}


@app.post('/internal/archive-old-threads')
async def archive_old_threads(request: Request):
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return json_resp({"error": "unauthorized"}, 403)

    days = int(os.environ.get('ARCHIVE_AFTER_DAYS', '30') or 30)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    archived = []
    errors = []

    try:
        all_threads = query_d1("SELECT * FROM threads", []) or []
    except Exception as e:
        return json_resp({"error": f"スレッド一覧の取得に失敗しました: {e}"}, 500)

    for t in all_threads:
        tid = int(t['id'])
        if tid in ARCHIVE_PINNED_IDS:
            continue

        try:
            last_reply_res = query_d1(
                "SELECT date FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                [tid]
            )
            last_activity = last_reply_res[0]['date'] if last_reply_res else t.get('created_at')
            if not last_activity or last_activity > cutoff:
                continue

            all_replies = query_d1(
                "SELECT * FROM replies WHERE thread_id = ? ORDER BY id ASC",
                [tid]
            ) or []

            archive_payload = {
                "thread": t,
                "replies": all_replies,
                "archived_at": datetime.utcnow().isoformat()
            }

            archive_key = f"archive/thread_{tid}.json"
            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=archive_key,
                Body=json.dumps(archive_payload, ensure_ascii=False, indent=2).encode('utf-8'),
                ContentType='application/json'
            )

            query_d1("DELETE FROM replies WHERE thread_id = ?", [tid])
            query_d1("DELETE FROM threads WHERE id = ?", [tid])

            query_d1(
                "INSERT OR REPLACE INTO archived_threads_index (thread_id, title, reply_count, archived_at) VALUES (?, ?, ?, ?)",
                [tid, t.get('title', '(無題)'), len(all_replies), datetime.utcnow().isoformat()]
            )

            archived.append(tid)
        except Exception as e:
            errors.append({"thread_id": tid, "error": str(e)})
            print(f"アーカイブエラー(thread_id={tid}): {e}")

    return {
        "archived_count": len(archived),
        "archived_thread_ids": archived,
        "errors": errors
    }


@app.get('/game')
async def game_lobby(request: Request):
    resp = templates.TemplateResponse(request, 'game.html', {'room': None, 'my_color': None})
    return _cookie_response(request, resp, _game_token(request))


@app.post('/game/create')
async def game_create(request: Request):
    token = _game_token(request)
    name = await _game_name(request)
    member_id = _game_member_id(request)
    code = _new_room_code()
    now = datetime.utcnow().isoformat()
    query_d1(
        '''INSERT INTO othello_rooms
           (room_code, black_token, black_name, black_member_id, white_token, white_name, board, turn, status, winner, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _initial_othello(), 'B', 'waiting', None, now, now]
    )
    resp = RedirectResponse(url=f'/game/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@app.get('/game/{room_code}')
async def game_room(request: Request, room_code: str):
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return RedirectResponse(url='/game')
    room = rows[0]
    token = _game_token(request)
    my_color = 'B' if room.get('black_token') == token else ('W' if room.get('white_token') == token else None)
    resp = templates.TemplateResponse(request, 'game.html', {'room': room, 'my_color': my_color})
    return _cookie_response(request, resp, token)


@app.post('/game/{room_code}/join')
async def game_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    room = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if room.get('black_token') == token or room.get('white_token') == token:
        return {'success': True}
    if room.get('white_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    query_d1(
        'UPDATE othello_rooms SET white_token=?,white_name=?,white_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@app.get('/api/game/{room_code}/state')
async def game_state(request: Request, room_code: str):
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return json_resp({'error': 'not found'}, 404)
    r = rows[0]
    token = _game_token(request)
    my = 'B' if r.get('black_token') == token else ('W' if r.get('white_token') == token else None)
    board = r['board']
    black_count = board.count('B')
    white_count = board.count('W')
    valid_moves = _othello_valid(board, r['turn']) if r['status'] == 'playing' else []
    return {
        'success': True,
        'room_code': r['room_code'],
        'board': board,
        'turn': r['turn'],
        'status': r['status'],
        'winner': r['winner'],
        'black_name': r.get('black_name') or '名無しさん',
        'white_name': r.get('white_name') or '名無しさん',
        'black_id': (r.get('black_token') or '')[:4],
        'white_id': (r.get('white_token') or '')[:4],
        'has_white': bool(r.get('white_token')),
        'my_color': my,
        'black_count': black_count,
        'white_count': white_count,
        'valid_moves': valid_moves,
    }


@app.post('/game/{room_code}/move')
async def game_move(request: Request, room_code: str):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    player = 'B' if r.get('black_token') == token else ('W' if r.get('white_token') == token else None)
    if not player:
        return json_resp({'success': False, 'error': '観戦者は着手できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != player:
        return {'success': False, 'error': '相手のターンです'}

    body = await get_json_silent(request)
    row = int(body.get('row', -1))
    col = int(body.get('col', -1))
    new_board = _othello_apply(r['board'], player, row, col)
    if new_board is None:
        return {'success': False, 'error': 'そこには置けません'}

    opponent = 'W' if player == 'B' else 'B'
    next_turn = opponent
    status = 'playing'
    winner = None
    if not _othello_valid(new_board, opponent):
        if _othello_valid(new_board, player):
            next_turn = player
        else:
            status = 'finished'
            black_count = new_board.count('B')
            white_count = new_board.count('W')
            winner = 'B' if black_count > white_count else ('W' if white_count > black_count else 'draw')
            black_key = _rating_key(r.get('black_member_id'), r.get('black_token'))
            white_key = _rating_key(r.get('white_member_id'), r.get('white_token'))
            result_black = 1 if winner == 'B' else (0 if winner == 'W' else 0.5)
            apply_game_result('othello', black_key, r.get('black_name') or '名無しさん',
                               white_key, r.get('white_name') or '名無しさん', result_black)

    now = datetime.utcnow().isoformat()
    query_d1(
        'UPDATE othello_rooms SET board=?,turn=?,status=?,winner=?,updated_at=? WHERE room_code=? AND turn=?',
        [new_board, next_turn, status, winner, now, code, player]
    )
    return {'success': True}


@app.get('/chess')
async def chess_lobby(request: Request):
    resp = templates.TemplateResponse(request, 'chess.html', {'room': None, 'my_color': None})
    return _cookie_response(request, resp, _game_token(request))


@app.post('/chess/create')
async def chess_create(request: Request):
    token = _game_token(request)
    name = await _game_name(request)
    member_id = _game_member_id(request)
    code = _new_room_code()
    now = datetime.utcnow().isoformat()
    query_d1(
        '''INSERT INTO chess_rooms
           (room_code, white_token, white_name, white_member_id, black_token, black_name, board, turn, status, winner, in_check, castling, en_passant, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _chess_board(), 'w', 'waiting', None, None, 'KQkq', None, now, now]
    )
    resp = RedirectResponse(url=f'/chess/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@app.get('/chess/{room_code}')
async def chess_room(request: Request, room_code: str):
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return RedirectResponse(url='/chess')
    r = rows[0]
    token = _game_token(request)
    my = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)
    resp = templates.TemplateResponse(request, 'chess.html', {'room': r, 'my_color': my})
    return _cookie_response(request, resp, token)


@app.post('/chess/{room_code}/join')
async def chess_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if r.get('white_token') == token or r.get('black_token') == token:
        return {'success': True}
    if r.get('black_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    query_d1(
        'UPDATE chess_rooms SET black_token=?,black_name=?,black_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@app.get('/api/chess/{room_code}/state')
async def chess_state(request: Request, room_code: str):
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return json_resp({'error': 'not found'}, 404)
    r = rows[0]
    token = _game_token(request)
    my = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)

    # DBに保存されたJSON文字列を配列に変換してフロントに渡す
    try:
        board_data = json.loads(r['board'])
    except Exception:
        board_data = []

    en_passant_raw = r.get('en_passant')
    en_passant_out = json.loads(en_passant_raw) if en_passant_raw else None

    return {
        'success': True,
        'room_code': r['room_code'],
        'board': board_data,
        'turn': r['turn'],
        'status': r['status'],
        'winner': r['winner'],
        'in_check': r['in_check'],
        'castling': r.get('castling') or 'KQkq',
        'en_passant': en_passant_out,
        'white_name': r.get('white_name') or '名無しさん',
        'black_name': r.get('black_name') or '名無しさん',
        'white_id': (r.get('white_token') or '')[:4],
        'black_id': (r.get('black_token') or '')[:4],
        'has_black': bool(r.get('black_token')),
        'my_color': my,
    }


@app.post('/chess/{room_code}/move')
async def chess_move(request: Request, room_code: str):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)
    if not color:
        return json_resp({'success': False, 'error': '観戦者は着手できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != color:
        return {'success': False, 'error': '相手のターンです'}

    body = await get_json_silent(request)
    try:
        fr, fc, tr, tc = [int(body[k]) for k in ('from_row', 'from_col', 'to_row', 'to_col')]
    except Exception:
        return json_resp({'success': False, 'error': '着手情報が不正です'}, 400)
    if not all(0 <= x < 8 for x in (fr, fc, tr, tc)):
        return json_resp({'success': False, 'error': '着手位置が不正です'}, 400)

    # JSON文字列をリストに読み込んで操作する
    try:
        board = json.loads(r['board'])
    except Exception:
        return json_resp({'success': False, 'error': '盤面データの読み込みに失敗しました'}, 500)

    castling = r.get('castling') or 'KQkq'
    en_passant_raw = r.get('en_passant')
    en_passant = tuple(json.loads(en_passant_raw)) if en_passant_raw else None

    piece = board[fr * 8 + fc]
    if not piece or piece[0] != color:
        return {'success': False, 'error': '自分の駒を選んでください'}
    if (tr, tc) not in _chess_pseudo(board, fr, fc, castling, en_passant):
        return {'success': False, 'error': 'その駒はそこへ動かせません'}

    # その手を指した結果、自分の王が王手にさらされる場合は指せない
    simulated = _chess_apply(board, fr, fc, tr, tc)
    if _chess_in_check(simulated, color):
        return {'success': False, 'error': 'その手を指すと自分の王が王手にさらされます'}

    # キャスリング権の更新(王・ルークが動いた/取られたら該当する権利を失う)
    new_castling = _chess_update_castling_rights(castling, piece[1], color, fr, fc, tr, tc)

    # アンパッサンの対象マスの更新(ポーンが2マス動いた時だけセット)
    new_en_passant = None
    if piece[1] == 'P' and abs(tr - fr) == 2:
        new_en_passant = [(fr + tr) // 2, fc]

    board = simulated
    next_color = 'b' if color == 'w' else 'w'

    # 次の手番が王手されているか、さらに合法手が残っているか(チェックメイト/ステイルメイト判定)
    next_in_check = _chess_in_check(board, next_color)
    next_has_moves = len(_chess_legal_moves(board, next_color, new_castling, new_en_passant)) > 0

    new_status = r['status']
    winner = r.get('winner')
    if not next_has_moves:
        new_status = 'finished'
        winner = 'draw' if not next_in_check else color
        white_key = _rating_key(r.get('white_member_id'), r.get('white_token'))
        black_key = _rating_key(r.get('black_member_id'), r.get('black_token'))
        result_white = 0.5 if winner == 'draw' else (1 if winner == 'w' else 0)
        apply_game_result('chess', white_key, r.get('white_name') or '名無しさん',
                           black_key, r.get('black_name') or '名無しさん', result_white)

    new_board_json = json.dumps(board)
    new_ep_json = json.dumps(new_en_passant) if new_en_passant else None
    now = datetime.utcnow().isoformat()
    query_d1(
        'UPDATE chess_rooms SET board=?,turn=?,updated_at=?,in_check=?,status=?,winner=?,castling=?,en_passant=? WHERE room_code=? AND turn=?',
        [new_board_json, next_color, now, (next_color if next_in_check else None), new_status, winner, new_castling, new_ep_json, code, color]
    )
    return {'success': True}


@app.get('/shogi')
async def shogi_lobby(request: Request):
    resp = templates.TemplateResponse(request, 'syogi.html', {'room': None, 'my_color': None})
    return _cookie_response(request, resp, _game_token(request))


@app.post('/shogi/create')
async def shogi_create(request: Request):
    token = _game_token(request)
    name = await _game_name(request)
    member_id = _game_member_id(request)
    code = _new_room_code()
    now = datetime.utcnow().isoformat()
    query_d1(
        '''INSERT INTO shogi_rooms
           (room_code, sente_token, sente_name, sente_member_id, gote_token, gote_name, board, hands, turn, status, winner, in_check, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _initial_shogi_board(), _initial_shogi_hands_json(), 's', 'waiting', None, None, now, now]
    )
    resp = RedirectResponse(url=f'/shogi/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@app.get('/shogi/{room_code}')
async def shogi_room(request: Request, room_code: str):
    rows = query_d1('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return RedirectResponse(url='/shogi')
    r = rows[0]
    token = _game_token(request)
    my = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)
    resp = templates.TemplateResponse(request, 'syogi.html', {'room': r, 'my_color': my})
    return _cookie_response(request, resp, token)


@app.post('/shogi/{room_code}/join')
async def shogi_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if r.get('sente_token') == token or r.get('gote_token') == token:
        return {'success': True}
    if r.get('gote_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    query_d1(
        'UPDATE shogi_rooms SET gote_token=?,gote_name=?,gote_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@app.get('/api/shogi/{room_code}/state')
async def shogi_state(request: Request, room_code: str):
    rows = query_d1('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return json_resp({'error': 'not found'}, 404)
    r = rows[0]
    token = _game_token(request)
    my = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)

    try:
        board_data = json.loads(r['board'])
    except Exception:
        board_data = []
    try:
        hands_data = json.loads(r['hands'])
    except Exception:
        hands_data = _shogi_empty_hands()

    return {
        'success': True,
        'room_code': r['room_code'],
        'board': board_data,
        'hands': hands_data,
        'turn': r['turn'],
        'status': r['status'],
        'winner': r['winner'],
        'in_check': r['in_check'],
        'sente_name': r.get('sente_name') or '名無しさん',
        'gote_name': r.get('gote_name') or '名無しさん',
        'sente_id': (r.get('sente_token') or '')[:4],
        'gote_id': (r.get('gote_token') or '')[:4],
        'has_gote': bool(r.get('gote_token')),
        'my_color': my,
    }


@app.post('/shogi/{room_code}/move')
async def shogi_move(request: Request, room_code: str):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)
    if not color:
        return json_resp({'success': False, 'error': '観戦者は着手できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != color:
        return {'success': False, 'error': '相手のターンです'}

    body = await get_json_silent(request)
    try:
        fr, fc, tr, tc = [int(body[k]) for k in ('from_row', 'from_col', 'to_row', 'to_col')]
    except Exception:
        return json_resp({'success': False, 'error': '着手情報が不正です'}, 400)
    if not all(0 <= x < 9 for x in (fr, fc, tr, tc)):
        return json_resp({'success': False, 'error': '着手位置が不正です'}, 400)
    want_promote = bool(body.get('promote'))

    try:
        board = json.loads(r['board'])
    except Exception:
        return json_resp({'success': False, 'error': '盤面データの読み込みに失敗しました'}, 500)
    try:
        hands = json.loads(r['hands'])
    except Exception:
        hands = _shogi_empty_hands()

    piece = board[fr * 9 + fc]
    if not piece or piece[0] != color:
        return {'success': False, 'error': '自分の駒を選んでください'}
    typ = piece[1:]
    if (tr, tc) not in _shogi_piece_moves(board, fr, fc):
        return {'success': False, 'error': 'その駒はそこへ動かせません'}

    is_already_promoted = typ.startswith('+')
    base_typ = typ[1:] if is_already_promoted else typ
    promote = False
    if not is_already_promoted and base_typ in SHOGI_PROMOTABLE:
        if _shogi_forced_promotion(base_typ, color, tr):
            promote = True
        elif want_promote and (_shogi_zone(color, fr) or _shogi_zone(color, tr)):
            promote = True

    # 王手放置チェック用のシミュレーション(成りは玉の安全性に影響しないためpromote=Falseで判定)
    simulated = _shogi_apply_move(board, fr, fc, tr, tc, promote=False)
    if _shogi_in_check(simulated, color):
        return {'success': False, 'error': 'その手を指すと自分の王が王手にさらされます'}

    captured = board[tr * 9 + tc]
    board = _shogi_apply_move(board, fr, fc, tr, tc, promote=promote)

    if captured:
        cap_typ = captured[1:]
        cap_base = cap_typ[1:] if cap_typ.startswith('+') else cap_typ
        hands.setdefault(color, {t: 0 for t in SHOGI_HAND_TYPES})
        hands[color][cap_base] = hands[color].get(cap_base, 0) + 1

    next_color = 'g' if color == 's' else 's'
    next_in_check = _shogi_in_check(board, next_color)
    next_has_moves = bool(_shogi_legal_board_moves(board, next_color)) or bool(_shogi_legal_drop_moves(board, next_color, hands.get(next_color, {})))

    new_status = r['status']
    winner = r.get('winner')
    if not next_has_moves:
        # 詰み(合法手が1つもない)は着手した側の勝ち
        new_status = 'finished'
        winner = color
        sente_key = _rating_key(r.get('sente_member_id'), r.get('sente_token'))
        gote_key = _rating_key(r.get('gote_member_id'), r.get('gote_token'))
        result_sente = 1 if winner == 's' else 0
        apply_game_result('shogi', sente_key, r.get('sente_name') or '名無しさん',
                           gote_key, r.get('gote_name') or '名無しさん', result_sente)

    now = datetime.utcnow().isoformat()
    query_d1(
        'UPDATE shogi_rooms SET board=?,hands=?,turn=?,updated_at=?,in_check=?,status=?,winner=? WHERE room_code=? AND turn=?',
        [json.dumps(board), json.dumps(hands), next_color, now, (next_color if next_in_check else None), new_status, winner, code, color]
    )
    return {'success': True}


@app.post('/shogi/{room_code}/drop')
async def shogi_drop(request: Request, room_code: str):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)
    if not color:
        return json_resp({'success': False, 'error': '観戦者は着手できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != color:
        return {'success': False, 'error': '相手のターンです'}

    body = await get_json_silent(request)
    ptype = str(body.get('piece', '')).upper()
    try:
        tr, tc = int(body['row']), int(body['col'])
    except Exception:
        return json_resp({'success': False, 'error': '着手情報が不正です'}, 400)
    if ptype not in SHOGI_HAND_TYPES:
        return json_resp({'success': False, 'error': '不正な駒です'}, 400)
    if not (0 <= tr < 9 and 0 <= tc < 9):
        return json_resp({'success': False, 'error': '着手位置が不正です'}, 400)

    try:
        board = json.loads(r['board'])
    except Exception:
        return json_resp({'success': False, 'error': '盤面データの読み込みに失敗しました'}, 500)
    try:
        hands = json.loads(r['hands'])
    except Exception:
        hands = _shogi_empty_hands()

    if hands.get(color, {}).get(ptype, 0) <= 0:
        return {'success': False, 'error': 'その持ち駒はありません'}
    if not _shogi_drop_allowed(board, color, ptype, tr, tc):
        return {'success': False, 'error': 'そこには打てません'}

    new_board = board[:]
    new_board[tr * 9 + tc] = color + ptype
    if _shogi_in_check(new_board, color):
        return {'success': False, 'error': 'その手を指すと自分の王が王手にさらされます'}

    hands[color][ptype] -= 1

    next_color = 'g' if color == 's' else 's'
    next_in_check = _shogi_in_check(new_board, next_color)
    next_has_moves = bool(_shogi_legal_board_moves(new_board, next_color)) or bool(_shogi_legal_drop_moves(new_board, next_color, hands.get(next_color, {})))

    new_status = r['status']
    winner = r.get('winner')
    if not next_has_moves:
        new_status = 'finished'
        winner = color
        sente_key = _rating_key(r.get('sente_member_id'), r.get('sente_token'))
        gote_key = _rating_key(r.get('gote_member_id'), r.get('gote_token'))
        result_sente = 1 if winner == 's' else 0
        apply_game_result('shogi', sente_key, r.get('sente_name') or '名無しさん',
                           gote_key, r.get('gote_name') or '名無しさん', result_sente)

    now = datetime.utcnow().isoformat()
    query_d1(
        'UPDATE shogi_rooms SET board=?,hands=?,turn=?,updated_at=?,in_check=?,status=?,winner=? WHERE room_code=? AND turn=?',
        [json.dumps(new_board), json.dumps(hands), next_color, now, (next_color if next_in_check else None), new_status, winner, code, color]
    )
    return {'success': True}


def _fetch_threads_with_stats(where_sql, where_params, order_sql, limit=None, offset=None):
    """threads を、レス数・最終更新日時・現在の閲覧人数つきで取得する共通ヘルパー"""
    active_cutoff = (datetime.utcnow() - timedelta(minutes=5)).isoformat()

    sql = f"""
        SELECT
            t.*,
            (SELECT COUNT(*) FROM replies r WHERE r.thread_id = t.id) AS replies_count,
            COALESCE(
                (SELECT MAX(r2.date) FROM replies r2 WHERE r2.thread_id = t.id),
                t.created_at
            ) AS last_activity,
            (SELECT COUNT(*) FROM active_users au
                WHERE au.location = ('thread_' || t.id) AND au.last_seen >= ?) AS thread_active_count
        FROM threads t
        {where_sql}
        ORDER BY {order_sql}
    """
    params = [active_cutoff] + list(where_params)

    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset or 0]

    return query_d1(sql, params)


@app.api_route('/', methods=['GET', 'HEAD'])
async def index(request: Request):
    client_ip = get_client_ip(request)
    if is_banned_ip(client_ip):
        return text_resp("あなたはアクセス禁止（BAN）されています。", 403)

    if request.method == 'HEAD':
        return Response(content='', status_code=200)

    try:
        page = int(request.query_params.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    per_page = 20
    start_index = (page - 1) * per_page

    search_query = request.query_params.get('q', '').strip()

    category = request.query_params.get('category', '').strip()
    if category not in THREAD_CATEGORY_VALUES:
        category = ''

    sort = request.query_params.get('sort', DEFAULT_THREAD_SORT).strip()
    if sort not in THREAD_SORT_SQL:
        sort = DEFAULT_THREAD_SORT
    order_sql = THREAD_SORT_SQL[sort]

    try:
        where_clauses = []
        where_params = []
        if search_query:
            where_clauses.append("t.title LIKE ?")
            where_params.append(f"%{search_query}%")
        if category:
            where_clauses.append("t.category = ?")
            where_params.append(category)
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        threads = _fetch_threads_with_stats(where_sql, where_params, order_sql, limit=per_page, offset=start_index)

        has_next = len(threads) == per_page

        pinned_ids = [4, 3, 2, 1]
        pinned_threads = []

        # 固定表示は「検索・カテゴリ絞り込み・並び替えなし」かつ1ページ目の時だけ行う
        show_pinned = (not search_query) and (not category) and sort == DEFAULT_THREAD_SORT and page == 1

        if show_pinned:
            for pid in pinned_ids:
                for i, t in enumerate(threads):
                    if int(t['id']) == pid:
                        pinned_threads.append(threads.pop(i))
                        break

            for pid in pinned_ids:
                if any(int(pt['id']) == pid for pt in pinned_threads):
                    continue
                try:
                    pinned_res = _fetch_threads_with_stats("WHERE t.id = ?", [pid], order_sql)
                    if pinned_res:
                        pinned_threads.append(pinned_res[0])
                except Exception as pe:
                    print(f"固定スレッド取得エラー: {pe}")

            for pt in pinned_threads:
                pt['is_pinned'] = True
                threads.insert(0, pt)

        for t in threads:
            if t.get('is_pinned') or int(t['id']) in [1, 2, 3, 4]:
                t['is_pinned'] = True
            if not t.get('category'):
                t['category'] = DEFAULT_THREAD_CATEGORY

        try:
            admin_res = query_d1("SELECT message FROM admin_messages WHERE id = ?", [1])
            admin_message = admin_res[0]['message'] if admin_res else "ここに管理者の一言が表示されます。"
        except Exception as ae:
            admin_message = "管理者の一言の取得に失敗しました。"

    except Exception as e:
        print(f"スレッド一覧取得エラー: {e}")
        threads = []
        has_next = False
        admin_message = "管理者の一言の取得に失敗しました。"

    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    active_count = update_and_get_user_counts(user_token, "lobby")
    is_admin_user = can_manage_board(request)
    current_member = get_current_member(request)

    response = templates.TemplateResponse(request, 'index.html', {
        'threads': threads,
        'admin_message': admin_message,
        'is_admin_user': is_admin_user,
        'current_member': current_member,
        'active_count': active_count,
        'current_page': page,
        'has_next': has_next,
        'search_query': search_query,
        'thread_categories': THREAD_CATEGORIES,
        'thread_category_labels': THREAD_CATEGORY_LABELS,
        'thread_category_colors': THREAD_CATEGORY_COLORS,
        'current_category': category,
        'thread_sort_options': THREAD_SORT_OPTIONS,
        'current_sort': sort,
        'current_year': datetime.utcnow().year,
        'category_meta_json': json.dumps(
            {key: {'label': label, 'color': color} for key, label, color in THREAD_CATEGORIES},
            ensure_ascii=False
        ),
    })

    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60 * 60 * 24 * 365, httponly=True)

    return response


@app.post('/update_admin_message')
async def update_admin_message(request: Request):
    if not can_manage_board(request):
        return text_resp("権限がありません", 403)
    form = await request.form()
    message = form.get('message')
    if message:
        try:
            query_d1("UPDATE admin_messages SET message = ? WHERE id = ?", [message, 1])
        except Exception as e:
            print(f"メッセージ更新エラー: {e}")
    return RedirectResponse(url='/', status_code=303)


@app.post('/create_thread')
async def create_thread(request: Request):
    client_ip = get_client_ip(request)
    if is_banned_ip(client_ip):
        return json_resp({"error": "あなたはアクセス禁止（BAN）されています。"}, 403)

    if not is_member_logged_in(request):
        return json_resp({"error": "スレッドを作成するにはログインが必要です。", "login_required": True}, 401)

    form = await request.form()
    title = form.get('title')
    if not title:
        return json_resp({"error": "タイトルが必要です"}, 400)

    title = filter_ng_words(title)
    title = html.escape(title)

    if len(title) > 30:
        return json_resp({"error": "スレッド名は30文字以内で入力してください"}, 400)

    category = form.get('category', DEFAULT_THREAD_CATEGORY)
    if category not in THREAD_CATEGORY_VALUES:
        category = DEFAULT_THREAD_CATEGORY

    is_admin = can_manage_board(request)
    now = time.time()

    thread_cooldown = 300
    if not is_admin and is_proxy_or_vpn(client_ip):
        thread_cooldown = 900

    if not is_admin:
        if client_ip in LAST_THREAD_TIMES and now - LAST_THREAD_TIMES[client_ip] < thread_cooldown:
            remaining_time = int(thread_cooldown - (now - LAST_THREAD_TIMES[client_ip]))
            minutes = remaining_time // 60
            seconds = remaining_time % 60
            return json_resp({"error": f"スレッド作成は5分に1回までです。(proxy,VPNは15分）あと {minutes}分 {seconds}秒 お待ちください。"}, 429)

    LAST_THREAD_TIMES[client_ip] = now

    try:
        member_public_id = get_member_public_id(request)
        query_d1(
            "INSERT INTO threads (title, ip_address, category, user_id) VALUES (?, ?, ?, ?)",
            [title, client_ip, category, member_public_id]
        )
        res = query_d1("SELECT * FROM threads ORDER BY id DESC LIMIT 1")
        new_thread = res[0] if res else None
        if new_thread and not new_thread.get('category'):
            new_thread['category'] = category
    except Exception as e:
        print(f"スレッド作成エラー: {e}")
        return json_resp({"error": "データベースエラーが発生しました"}, 500)

    return {"success": True, "thread": new_thread}


# --- 「もっと見る」用: 過去のレスを追加読み込みするAPI ---
@app.get('/thread/{thread_id}/get_older_replies')
async def get_older_replies(request: Request, thread_id: int):
    client_ip = get_client_ip(request)
    if is_banned_ip(client_ip):
        return json_resp({"success": False, "error": "Banned"}, 403)

    before_id_raw = request.query_params.get('before_id')
    try:
        before_id = int(before_id_raw) if before_id_raw is not None else None
    except (TypeError, ValueError):
        before_id = None
    if not before_id:
        return json_resp({"success": False, "error": "before_idが必要です", "replies": [], "has_more": False}, 400)

    try:
        count_res = query_d1("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ? AND id < ?", [thread_id, before_id])
        count_before = count_res[0]['cnt'] if count_res else 0

        LOAD_LIMIT = 300
        older_res = query_d1(
            "SELECT * FROM replies WHERE thread_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
            [thread_id, before_id, LOAD_LIMIT]
        )
        older_replies = list(reversed(older_res)) if older_res else []
        start_num = count_before - len(older_replies) + 1

        thread_res = query_d1("SELECT ip_address, user_id FROM threads WHERE id = ?", [thread_id])
        op_user_id = resolve_op_user_id(thread_res[0]) if thread_res else None
        member_map = get_profile_usernames_map([r.get('user_id') for r in older_replies] + [op_user_id])

        formatted_replies = []
        for i, r in enumerate(older_replies):
            reply_dict = dict(r)
            if reply_dict.get('date'):
                try:
                    raw_date = str(reply_dict['date']).replace('Z', '+00:00')
                    dt_utc = datetime.fromisoformat(raw_date)
                    dt_jst = dt_utc + timedelta(hours=9)
                    reply_dict['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    pass
            if reply_dict.get('content'):
                try:
                    content_str = str(reply_dict['content'])
                    content_str = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', content_str)
                    content_str = re.sub(r'&gt;&gt;(\d+)|>>(\d+)', r'<a href="#post-\1\2" class="post-anchor" onclick="scrollToPost(\1\2); return false;">&gt;&gt;\1\2</a>', content_str)
                    reply_dict['content'] = content_str
                except Exception:
                    pass
            reply_dict['is_op'] = bool(op_user_id) and reply_dict.get('user_id') == op_user_id
            reply_dict['profile_username'] = member_map.get(reply_dict.get('user_id'))
            reply_dict['post_num'] = start_num + i
            formatted_replies.append(reply_dict)

        return {"success": True, "replies": formatted_replies, "has_more": count_before > len(older_replies)}
    except Exception as e:
        print(f"過去レス取得エラー: {e}")
        return json_resp({"success": False, "error": "データベースエラー", "replies": [], "has_more": False}, 500)


# --- リアルタイム自動更新用API ---
@app.get('/thread/{thread_id}/get_new_replies')
async def get_new_replies(request: Request, thread_id: int):
    client_ip = get_client_ip(request)
    if is_banned_ip(client_ip):
        return json_resp({"success": False, "error": "Banned"}, 403)

    try:
        after_id = int(request.query_params.get('after_id', 0))
    except (TypeError, ValueError):
        after_id = 0
    try:
        replies = query_d1(
            "SELECT * FROM replies WHERE thread_id = ? AND id > ? ORDER BY id ASC",
            [thread_id, after_id]
        )
        if not replies:
            replies = []

        thread_res = query_d1("SELECT ip_address, user_id FROM threads WHERE id = ?", [thread_id])
        op_user_id = resolve_op_user_id(thread_res[0]) if thread_res else None

        total_count_res = query_d1("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id])
        total_reply_count = total_count_res[0]['cnt'] if total_count_res else 0
        start_num = total_reply_count - len(replies) + 1
        member_map = get_profile_usernames_map([r.get('user_id') for r in replies] + [op_user_id])

        formatted_replies = []
        for idx, r in enumerate(replies):
            reply_dict = dict(r)
            if reply_dict.get('date'):
                try:
                    raw_date = str(reply_dict['date']).replace('Z', '+00:00')
                    dt_utc = datetime.fromisoformat(raw_date)
                    dt_jst = dt_utc + timedelta(hours=9)
                    reply_dict['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    pass

            if reply_dict.get('content'):
                try:
                    content_str = str(reply_dict['content'])
                    # 1. URLのリンク化
                    content_str = re.sub(
                        r'(https?://[^\s<>]+)',
                        r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>',
                        content_str
                    )
                    # 2. >>数字 のアンカーリンク化を追加
                    content_str = re.sub(
                        r'&gt;&gt;(\d+)|>>(\d+)',
                        r'<a href="#post-\1\2" class="post-anchor" onclick="scrollToPost(\1\2); return false;">&gt;&gt;\1\2</a>',
                        content_str
                    )
                    reply_dict['content'] = content_str
                except Exception:
                    pass

            reply_dict['is_op'] = bool(op_user_id) and reply_dict.get('user_id') == op_user_id
            reply_dict['profile_username'] = member_map.get(reply_dict.get('user_id'))
            reply_dict['post_num'] = start_num + idx
            formatted_replies.append(reply_dict)

        return {"success": True, "replies": formatted_replies}
    except Exception as e:
        print(f"新着レス取得エラー: {e}")
        return json_resp({"success": False, "error": "データベースエラー", "replies": []}, 500)


@app.api_route('/thread/{thread_id}', methods=['GET', 'POST'])
async def thread_view(request: Request, thread_id: int):
    client_ip = get_client_ip(request)
    if is_banned_ip(client_ip):
        return text_resp("あなたはアクセス禁止（BAN）されています。", 403)

    if request.method == 'POST':
        form = await request.form()
        content = form.get('content') or ""

        if len(content) > 500:
            return json_resp({"success": False, "error": "500文字以内で入力してください。"}, 400)

        author_input = form.get('author') or "名無しさん"

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

        staff_role = get_staff_role(request)

        if staff_role:
            author_input = request.session.get('staff_name')
            is_admin = can_manage_board(request)
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
            member_public_id = get_member_public_id(request)
            user_id = member_public_id if member_public_id else get_daily_user_id(client_ip)

        content = html.escape(content)
        content = re.sub(r'&gt;&gt;(\d+)', r'>>\1', content)

        now = time.time()
        if not staff_role:
            reply_cooldown = 3
            if client_ip in LAST_REPLY_TIMES and now - LAST_REPLY_TIMES[client_ip] < reply_cooldown:
                return json_resp({"success": False, "error": f"連続投稿はできません。{reply_cooldown}秒お待ちください。"}, 429)
            LAST_REPLY_TIMES[client_ip] = now

        image_url = ""
        upload = form.get('image')
        if upload is not None and getattr(upload, 'filename', ''):
            try:
                orig_filename = secure_filename(upload.filename)
                ext = os.path.splitext(orig_filename)[1]
                unique_filename = f"{uuid.uuid4()}{ext}"
                s3_client.upload_fileobj(upload.file, R2_BUCKET_NAME, unique_filename, ExtraArgs={'ContentType': upload.content_type})
                image_url = f"{R2_PUBLIC_URL.rstrip('/')}/{unique_filename}"
            except Exception as e:
                print(f"R2 Upload Error: {e}")

        if content.strip() or image_url:
            # 同一クライアントから同じレスが短時間に二重送信された場合を防止。
            # フロント側の二重イベント登録や通信リトライがあってもDBへ二重保存しない。
            reply_signature = hashlib.sha256(
                f"{thread_id}|{client_ip}|{author_input}|{content}|{image_url}".encode('utf-8')
            ).hexdigest()
            signature_now = time.time()
            previous_signature_time = LAST_REPLY_SIGNATURES.get(reply_signature)
            if previous_signature_time is not None and signature_now - previous_signature_time < 5:
                return json_resp({"success": False, "duplicate": True, "error": "同じ内容が連続して送信されたため、重複投稿を防止しました。"}, 409)

            try:
                query_d1(
                    """INSERT INTO replies (thread_id, author, content, user_id, is_admin, role, image_url, ip_address) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [thread_id, author_input, content, user_id, 1 if is_admin else 0, role_to_save, image_url, client_ip]
                )
                LAST_REPLY_SIGNATURES[reply_signature] = signature_now
                res = query_d1("SELECT * FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT 1", [thread_id])
                new_reply = res[0] if res else None
                if new_reply:
                    if new_reply.get('date'):
                        dt_utc = datetime.fromisoformat(new_reply['date'].replace('Z', '+00:00'))
                        dt_jst = dt_utc + timedelta(hours=9)
                        new_reply['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')

                    if new_reply.get('content'):
                        content_str = str(new_reply['content'])
                        content_str = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', content_str)
                        content_str = re.sub(r'&gt;&gt;(\d+)|>>(\d+)', r'<a href="#post-\1\2" class="post-anchor" onclick="scrollToPost(\1\2); return false;">&gt;&gt;\1\2</a>', content_str)
                        new_reply['content'] = content_str

                    try:
                        thread_res = query_d1("SELECT ip_address, user_id FROM threads WHERE id = ?", [thread_id])
                        op_user_id = resolve_op_user_id(thread_res[0]) if thread_res else None
                        new_reply['is_op'] = bool(op_user_id) and new_reply.get('user_id') == op_user_id
                    except Exception as ope:
                        new_reply['is_op'] = False

                    new_reply['profile_username'] = get_profile_username(new_reply.get('user_id'))

                    try:
                        total_count_res = query_d1("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id])
                        new_reply['post_num'] = total_count_res[0]['cnt'] if total_count_res else None
                    except Exception:
                        new_reply['post_num'] = None

                    await manager.broadcast(thread_id, new_reply)
                    return {"success": True, "reply": new_reply}
            except Exception as e:
                print(f"レス保存エラー: {e}")
                return json_resp({"success": False, "error": "データベースエラーが発生しました。"}, 500)
        return json_resp({"success": False, "error": "書き込み内容が空です。"}, 400)

    try:
        thread_res = query_d1("SELECT * FROM threads WHERE id = ?", [thread_id])
        if not thread_res:
            return text_resp("スレッドが見つかりません", 404)
        thread = thread_res[0]

        # 合計レス数を取得(通し番号の計算とページングに使う)
        count_res = query_d1("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id])
        total_reply_count = count_res[0]['cnt'] if count_res else 0

        # D1のAPI応答サイズ制限対策として、直近300件だけ取得する(古い順に並べ直す)
        RECENT_REPLIES_LIMIT = 300
        replies_res = query_d1(
            "SELECT * FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
            [thread_id, RECENT_REPLIES_LIMIT]
        )
        loaded_replies = list(reversed(replies_res)) if replies_res else []
        start_num = total_reply_count - len(loaded_replies) + 1
        for i, r in enumerate(loaded_replies):
            r['post_num'] = start_num + i

        thread['replies'] = loaded_replies
        thread['total_reply_count'] = total_reply_count
        thread['has_older'] = total_reply_count > len(loaded_replies)

        for r in thread['replies']:
            if r.get('date'):
                dt_utc = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
            if r.get('content'):
                content_str = str(r['content'])
                content_str = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', content_str)
                content_str = re.sub(r'&gt;&gt;(\d+)|>>(\d+)', r'<a href="#post-\1\2" class="post-anchor" onclick="scrollToPost(\1\2); return false;">&gt;&gt;\1\2</a>', content_str)
                r['content'] = content_str

        op_user_id = resolve_op_user_id(thread)
        member_map = get_profile_usernames_map([r.get('user_id') for r in thread['replies']] + [op_user_id])
        for r in thread['replies']:
            r['is_op'] = bool(op_user_id) and r.get('user_id') == op_user_id
            r['profile_username'] = member_map.get(r.get('user_id'))
    except Exception as e:
        print(f"スレッド読み込みエラー: {e}")
        return text_resp("データベースエラーが発生しました", 500)

    is_admin_user = can_manage_board(request)
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)

    response = templates.TemplateResponse(request, 'thread.html', {
        'thread': thread,
        'is_admin_user': is_admin_user,
        'active_count': active_count,
        'back_to_board': "/?tab=threads",
        'op_user_id': op_user_id,
        'current_member': get_current_member(request)
    })

    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60 * 60 * 24 * 365, httponly=True)

    return response


@app.post('/thread/{thread_id}/delete_thread')
async def delete_thread(request: Request, thread_id: int):
    if not can_manage_board(request):
        return text_resp("権限がありません", 403)
    try:
        query_d1("DELETE FROM threads WHERE id = ?", [thread_id])
    except Exception as e:
        print(f"スレッド削除エラー: {e}")
    return RedirectResponse(url='/', status_code=303)


@app.post('/thread/{thread_id}/delete/{reply_id}')
async def delete_reply(request: Request, thread_id: int, reply_id: int):
    if not can_manage_board(request):
        return text_resp("権限がありません", 403)
    try:
        query_d1(
            """UPDATE replies SET author = ?, content = ?, user_id = ?, is_admin = ?, image_url = ? 
               WHERE id = ? AND thread_id = ?""",
            ['あぼーん', 'この書き込みは管理員によって削除されました。', '???', 0, '', reply_id, thread_id]
        )
    except Exception as e:
        print(f"レス削除エラー: {e}")
    return RedirectResponse(url=f'/thread/{thread_id}', status_code=303)


@app.post('/ban_user/{thread_id}/{reply_id}')
async def ban_user(request: Request, thread_id: int, reply_id: int):
    if not can_manage_board(request):
        return text_resp("権限がありません", 403)
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
        referer = request.headers.get('referer') or '/'
        return RedirectResponse(url=referer, status_code=303)
    except Exception as e:
        print(f"BANエラー: {e}")
        return text_resp(f"エラーが発生しました: {e}", 500)


@app.post('/ban_thread_owner/{thread_id}')
async def ban_thread_owner(request: Request, thread_id: int):
    if not can_manage_board(request):
        return text_resp("権限がありません", 403)
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
        return RedirectResponse(url='/', status_code=303)
    except Exception as e:
        print(f"スレッドオーナーBANエラー: {e}")
        return text_resp(f"エラーが発生しました: {e}", 500)


@app.get('/api/server_stats')
async def server_metrics():
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
        "memory_total_mb": memory_limit_mb,
        "net_rx_kbps": rx_kbps,
        "net_tx_kbps": tx_kbps,
        "timestamp": time.time()
    }


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    uvicorn.run(app, host='0.0.0.0', port=port)
