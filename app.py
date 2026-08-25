from flask import Flask, render_template, request, redirect, url_for, make_response, session
from datetime import datetime, timedelta
import json
import html
import os
import hashlib
import uuid
import time
import re
import random
import httpx
import boto3
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 7

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

app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345')

CF_SHARED_SECRET = os.environ.get('CF_SHARED_SECRET')

@app.before_request
def response_to_uptimerobot():
    if request.method == 'HEAD':
        return make_response('', 200)

s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    region_name='auto'
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'bbs-images')
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL')  


LAST_THREAD_TIMES = {}
LAST_REPLY_TIMES = {}
LAST_REPLY_SIGNATURES = {}

def get_daily_user_id(ip_address):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

def get_client_ip():
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
    'シコシコ':'4545',
    'オナニー':'0721',
    '射精':'身寸米青',
    '精子':'米青子',
    
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

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')
    
@app.route('/roles')
def roles():
    return render_template('roles.html')

# =========================
# D1版 ゲーム機能（オセロ・チェス・アーカイブ）
# =========================

def _game_token():
    token = request.cookies.get('game_player_token') or request.cookies.get('user_bbs_token')
    if not token:
        token = str(uuid.uuid4())
    return token

def _game_name(default='名無しさん'):
    name = request.form.get('name')
    if not name and request.is_json:
        body = request.get_json(silent=True) or {}
        name = body.get('name')
    if not name:
        name = request.cookies.get('bbs_saved_author')
    name = html.escape(str(name or default).strip())[:20]
    return name or default

def _new_room_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(30):
        code = ''.join(random.choice(alphabet) for _ in range(6))
        if not query_d1('SELECT 1 FROM othello_rooms WHERE room_code = ? LIMIT 1', [code]) and not query_d1('SELECT 1 FROM chess_rooms WHERE room_code = ? LIMIT 1', [code]):
            return code
    return uuid.uuid4().hex[:6].upper()

def _initial_othello():
    b = ['.'] * 64
    b[3*8+3] = 'W'; b[3*8+4] = 'B'; b[4*8+3] = 'B'; b[4*8+4] = 'W'
    return ''.join(b)

def _othello_valid(board, player):
    opp = 'W' if player == 'B' else 'B'
    dirs = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
    out=[]
    for r in range(8):
        for c in range(8):
            if board[r*8+c] != '.': continue
            ok=False
            for dr,dc in dirs:
                rr,cc=r+dr,c+dc; seen=False
                while 0<=rr<8 and 0<=cc<8 and board[rr*8+cc]==opp:
                    seen=True; rr+=dr; cc+=dc
                if seen and 0<=rr<8 and 0<=cc<8 and board[rr*8+cc]==player:
                    ok=True; break
            if ok: out.append((r,c))
    return out

def _othello_apply(board, player, r, c):
    if (r,c) not in _othello_valid(board, player): return None
    a=list(board); a[r*8+c]=player; opp='W' if player=='B' else 'B'
    dirs=[(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
    for dr,dc in dirs:
        rr,cc=r+dr,c+dc; flips=[]
        while 0<=rr<8 and 0<=cc<8 and a[rr*8+cc]==opp:
            flips.append((rr,cc)); rr+=dr; cc+=dc
        if flips and 0<=rr<8 and 0<=cc<8 and a[rr*8+cc]==player:
            for fr,fc in flips: a[fr*8+fc]=player
    return ''.join(a)


def _initial_chess():
    # 64要素のリストを作成し、JSON文字列にシリアライズして返す
    board_list = [
        'bR','bN','bB','bQ','bK','bB','bN','bR',
        'bP','bP','bP','bP','bP','bP','bP','bP',
        '','','','','','','','',
        '','','','','','','','',
        '','','','','','','','',
        '','','','','','','','',
        'wP','wP','wP','wP','wP','wP','wP','wP',
        'wR','wN','wB','wQ','wK','wB','wN','wR'
    ]
    return json.dumps(board_list)

def _chess_board():
    return _initial_chess()

def _chess_pseudo(board, r, c, castling='', en_passant=None):
    p=board[r*8+c]
    if not p: return []
    color,typ=p[0],p[1]; out=[]
    dirs=[]
    if typ=='N': dirs=[(-2,-1),(-2,1),(-1,-2),(-1,2),(1,-2),(1,2),(2,-1),(2,1)]
    elif typ=='K': dirs=[(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
    elif typ in 'BRQ':
        if typ in 'BQ': dirs += [(-1,-1),(-1,1),(1,-1),(1,1)]
        if typ in 'RQ': dirs += [(-1,0),(1,0),(0,-1),(0,1)]
    if typ in 'NK':
        for dr,dc in dirs:
            rr,cc=r+dr,c+dc
            if 0<=rr<8 and 0<=cc<8 and (not board[rr*8+cc] or board[rr*8+cc][0]!=color): out.append((rr,cc))
    elif typ in 'BRQ':
        for dr,dc in dirs:
            rr,cc=r+dr,c+dc
            while 0<=rr<8 and 0<=cc<8:
                t=board[rr*8+cc]
                if not t: out.append((rr,cc))
                else:
                    if t[0]!=color: out.append((rr,cc))
                    break
                rr+=dr; cc+=dc
    elif typ=='P':
        d=-1 if color=='w' else 1; start=6 if color=='w' else 1
        rr=r+d
        if 0<=rr<8 and not board[rr*8+c]:
            out.append((rr,c))
            rr2=r+2*d
            if r==start and not board[rr2*8+c]: out.append((rr2,c))
        for dc in (-1,1):
            rr,cc=r+d,c+dc
            if 0<=rr<8 and 0<=cc<8:
                if board[rr*8+cc] and board[rr*8+cc][0]!=color:
                    out.append((rr,cc))
                elif en_passant and en_passant==(rr,cc):
                    out.append((rr,cc))

    if typ=='K':
        row = 7 if color=='w' else 0
        if r==row and c==4:
            k_flag = 'K' if color=='w' else 'k'
            q_flag = 'Q' if color=='w' else 'q'
            opp = 'b' if color=='w' else 'w'
            if (k_flag in castling and not board[row*8+5] and not board[row*8+6]
                    and board[row*8+7]==color+'R'
                    and not _chess_attacked(board,row,4,opp)
                    and not _chess_attacked(board,row,5,opp)
                    and not _chess_attacked(board,row,6,opp)):
                out.append((row,6))
            if (q_flag in castling and not board[row*8+3] and not board[row*8+2] and not board[row*8+1]
                    and board[row*8+0]==color+'R'
                    and not _chess_attacked(board,row,4,opp)
                    and not _chess_attacked(board,row,3,opp)
                    and not _chess_attacked(board,row,2,opp)):
                out.append((row,2))
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
        if 0 <= pr < 8 and 0 <= pc < 8 and board[pr*8+pc] == by_color + 'P':
            return True
    # ナイトの攻撃
    for dr, dc in [(-2,-1),(-2,1),(-1,-2),(-1,2),(1,-2),(1,2),(2,-1),(2,1)]:
        rr, cc = r + dr, c + dc
        if 0 <= rr < 8 and 0 <= cc < 8 and board[rr*8+cc] == by_color + 'N':
            return True
    # 王の攻撃(隣接マス)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0: continue
            rr, cc = r + dr, c + dc
            if 0 <= rr < 8 and 0 <= cc < 8 and board[rr*8+cc] == by_color + 'K':
                return True
    # 直線(ルーク・クイーン)
    for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
        rr, cc = r + dr, c + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            p = board[rr*8+cc]
            if p:
                if p[0] == by_color and p[1] in ('R', 'Q'): return True
                break
            rr += dr; cc += dc
    # 斜め(ビショップ・クイーン)
    for dr, dc in [(-1,-1),(-1,1),(1,-1),(1,1)]:
        rr, cc = r + dr, c + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            p = board[rr*8+cc]
            if p:
                if p[0] == by_color and p[1] in ('B', 'Q'): return True
                break
            rr += dr; cc += dc
    return False

def _chess_in_check(board, color):
    pos = _chess_find_king(board, color)
    if not pos: return False
    r, c = pos
    opp = 'b' if color == 'w' else 'w'
    return _chess_attacked(board, r, c, opp)

def _chess_apply(board, r, c, tr, tc):
    """盤面をコピーして着手を適用した新しい盤面を返す(王手判定のシミュレーション用)"""
    nb = board[:]
    piece = nb[r*8+c]
    color, typ = piece[0], piece[1]
    nb[r*8+c] = ''

    if typ == 'K' and abs(tc - c) == 2:
        # キャスリング: 王が横に2マス動く手 -> ルークも一緒に動かす
        nb[tr*8+tc] = piece
        row = r
        if tc == 6:
            nb[row*8+7] = ''
            nb[row*8+5] = color + 'R'
        elif tc == 2:
            nb[row*8+0] = ''
            nb[row*8+3] = color + 'R'
    elif typ == 'P' and c != tc and not board[tr*8+tc]:
        # アンパッサン: ポーンが斜めに動いたのに移動先が空 -> 通過されたポーンを取る
        nb[tr*8+tc] = piece
        nb[r*8+tc] = ''
    elif typ == 'P' and tr in (0, 7):
        nb[tr*8+tc] = color + 'Q'
    else:
        nb[tr*8+tc] = piece
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

def _cookie_response(resp, token):
    if not request.cookies.get('game_player_token'):
        resp.set_cookie('game_player_token', token, max_age=60*60*24*365, httponly=True, samesite='Lax')
    return resp


@app.route('/games')
def games_hub():
    return render_template('games_hub.html')

@app.route('/archive')
def archive_list():
    page = request.args.get('page', default=1, type=int)
    per_page = 20
    offset = (page - 1) * per_page
    rows = query_d1(
        "SELECT * FROM archived_threads_index ORDER BY archived_at DESC LIMIT ? OFFSET ?",
        [per_page, offset]
    )
    archived_threads = rows or []
    has_next = len(archived_threads) == per_page
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
                dt_utc = datetime.fromisoformat(str(r['date']).replace('Z', '+00:00'))
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
        if r.get('content'):
            r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', str(r['content']))

    return render_template('archive_view.html', thread=thread, replies=replies, archived_at=payload.get('archived_at'))

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

@app.route('/internal/migrate-from-supabase', methods=['POST'])
def migrate_from_supabase():
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return {"error": "unauthorized"}, 403

    sb_url = request.headers.get('X-Supabase-Url')
    sb_key = request.headers.get('X-Supabase-Key')
    if not sb_url or not sb_key:
        return {"error": "X-Supabase-Url / X-Supabase-Key ヘッダーが必要です"}, 400

    try:
        threads = _fetch_all_from_supabase(sb_url, sb_key, 'threads', 'id,title,created_at,ip_address')
        replies = _fetch_all_from_supabase(sb_url, sb_key, 'replies', 'id,thread_id,author,content,user_id,is_admin,image_url,ip_address,date,role')
    except Exception as e:
        return {"error": f"Supabaseからの取得に失敗しました: {e}"}, 500

    try:
        threads_inserted = _d1_batch_insert(
            'threads', ['id', 'title', 'created_at', 'ip_address'], threads, chunk_size=200
        )
        replies_inserted = _d1_batch_insert(
            'replies', ['id', 'thread_id', 'author', 'content', 'user_id', 'is_admin', 'image_url', 'ip_address', 'date', 'role'], replies, chunk_size=90
        )
    except Exception as e:
        return {"error": f"D1への書き込みに失敗しました: {e}"}, 500

    return {
        "threads_fetched": len(threads),
        "replies_fetched": len(replies),
        "threads_inserted_or_ignored": threads_inserted,
        "replies_inserted_or_ignored": replies_inserted
    }

@app.route('/internal/migrate-from-supabase-safe', methods=['POST'])
def migrate_from_supabase_safe():
    # ID衝突を避けるため、今のD1の最大IDより確実に大きい番号にずらしてから追加する版
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return {"error": "unauthorized"}, 403

    sb_url = request.headers.get('X-Supabase-Url')
    sb_key = request.headers.get('X-Supabase-Key')
    if not sb_url or not sb_key:
        return {"error": "X-Supabase-Url / X-Supabase-Key ヘッダーが必要です"}, 400

    try:
        max_tid_res = query_d1("SELECT MAX(id) as m FROM threads", [])
        max_rid_res = query_d1("SELECT MAX(id) as m FROM replies", [])
        current_max_tid = (max_tid_res[0]['m'] if max_tid_res and max_tid_res[0]['m'] is not None else 0)
        current_max_rid = (max_rid_res[0]['m'] if max_rid_res and max_rid_res[0]['m'] is not None else 0)
    except Exception as e:
        return {"error": f"現在のD1の最大IDの取得に失敗しました: {e}"}, 500

    thread_offset = current_max_tid + 10000
    reply_offset = current_max_rid + 10000

    try:
        threads = _fetch_all_from_supabase(sb_url, sb_key, 'threads', 'id,title,created_at,ip_address')
        replies = _fetch_all_from_supabase(sb_url, sb_key, 'replies', 'id,thread_id,author,content,user_id,is_admin,image_url,ip_address,date,role')
    except Exception as e:
        return {"error": f"Supabaseからの取得に失敗しました: {e}"}, 500

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
        return {"error": f"D1への書き込みに失敗しました: {e}"}, 500

    return {
        "thread_offset": thread_offset,
        "reply_offset": reply_offset,
        "threads_fetched": len(threads),
        "replies_fetched": len(replies),
        "threads_inserted_or_ignored": threads_inserted,
        "replies_inserted_or_ignored": replies_inserted
    }

@app.route('/internal/rebuild-archive-index', methods=['POST'])
def rebuild_archive_index():
    # R2に実在するJSONから、D1の索引テーブル(archived_threads_index)を作り直す
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return {"error": "unauthorized"}, 403

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
        return {"error": f"R2一覧の取得に失敗しました: {e}"}, 500

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

@app.route('/internal/archive-old-threads', methods=['POST'])
def archive_old_threads():
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return {"error": "unauthorized"}, 403

    days = int(os.environ.get('ARCHIVE_AFTER_DAYS', '30') or 30)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    archived = []
    errors = []

    try:
        all_threads = query_d1("SELECT * FROM threads", []) or []
    except Exception as e:
        return {"error": f"スレッド一覧の取得に失敗しました: {e}"}, 500

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

@app.route('/game')
def game_lobby():
    resp=make_response(render_template('game.html', room=None, my_color=None))
    return _cookie_response(resp, _game_token())

@app.route('/game/create', methods=['POST'])
def game_create():
    token = _game_token()
    name = _game_name()
    code = _new_room_code()
    now = datetime.utcnow().isoformat()
    query_d1(
        '''INSERT INTO othello_rooms
           (room_code, black_token, black_name, white_token, white_name, board, turn, status, winner, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, None, None, _initial_othello(), 'B', 'waiting', None, now, now]
    )
    resp = redirect(url_for('game_room', room_code=code))
    return _cookie_response(resp, token)

@app.route('/game/<room_code>')
def game_room(room_code):
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return redirect(url_for('game_lobby'))
    room = rows[0]
    token = _game_token()
    my_color = 'B' if room.get('black_token') == token else ('W' if room.get('white_token') == token else None)
    resp = make_response(render_template('game.html', room=room, my_color=my_color))
    return _cookie_response(resp, token)

@app.route('/game/<room_code>/join', methods=['POST'])
def game_join(room_code):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return {'success': False, 'error': '部屋が見つかりません'}, 404
    room = rows[0]
    token = _game_token()
    name = _game_name()
    if room.get('black_token') == token or room.get('white_token') == token:
        return {'success': True}
    if room.get('white_token'):
        return {'success': False, 'error': 'この部屋は満員です'}, 409
    query_d1(
        'UPDATE othello_rooms SET white_token=?,white_name=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}

@app.route('/api/game/<room_code>/state')
def game_state(room_code):
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return {'error': 'not found'}, 404
    r = rows[0]
    token = _game_token()
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

@app.route('/game/<room_code>/move', methods=['POST'])
def game_move(room_code):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return {'success': False, 'error': '部屋が見つかりません'}, 404
    r = rows[0]
    token = _game_token()
    player = 'B' if r.get('black_token') == token else ('W' if r.get('white_token') == token else None)
    if not player:
        return {'success': False, 'error': '観戦者は着手できません'}, 403
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != player:
        return {'success': False, 'error': '相手のターンです'}

    body = request.get_json(silent=True) or {}
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

    now = datetime.utcnow().isoformat()
    query_d1(
        'UPDATE othello_rooms SET board=?,turn=?,status=?,winner=?,updated_at=? WHERE room_code=? AND turn=?',
        [new_board, next_turn, status, winner, now, code, player]
    )
    return {'success': True}

@app.route('/chess')
def chess_lobby():
    resp = make_response(render_template('chess.html', room=None, my_color=None))
    return _cookie_response(resp, _game_token())

@app.route('/chess/create', methods=['POST'])
def chess_create():
    token = _game_token()
    name = _game_name()
    code = _new_room_code()
    now = datetime.utcnow().isoformat()
    query_d1(
        '''INSERT INTO chess_rooms
           (room_code, white_token, white_name, black_token, black_name, board, turn, status, winner, in_check, castling, en_passant, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, None, None, _chess_board(), 'w', 'waiting', None, None, 'KQkq', None, now, now]
    )
    resp = redirect(url_for('chess_room', room_code=code))
    return _cookie_response(resp, token)

@app.route('/chess/<room_code>')
def chess_room(room_code):
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return redirect(url_for('chess_lobby'))
    r = rows[0]
    token = _game_token()
    my = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)
    resp = make_response(render_template('chess.html', room=r, my_color=my))
    return _cookie_response(resp, token)

@app.route('/chess/<room_code>/join', methods=['POST'])
def chess_join(room_code):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return {'success': False, 'error': '部屋が見つかりません'}, 404
    r = rows[0]
    token = _game_token()
    name = _game_name()
    if r.get('white_token') == token or r.get('black_token') == token:
        return {'success': True}
    if r.get('black_token'):
        return {'success': False, 'error': 'この部屋は満員です'}, 409
    query_d1(
        'UPDATE chess_rooms SET black_token=?,black_name=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}

@app.route('/api/chess/<room_code>/state')
def chess_state(room_code):
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return {'error': 'not found'}, 404
    r = rows[0]
    token = _game_token()
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

@app.route('/chess/<room_code>/move', methods=['POST'])
def chess_move(room_code):
    code = room_code.upper()
    rows = query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return {'success': False, 'error': '部屋が見つかりません'}, 404
    r = rows[0]
    token = _game_token()
    color = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)
    if not color:
        return {'success': False, 'error': '観戦者は着手できません'}, 403
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != color:
        return {'success': False, 'error': '相手のターンです'}

    body = request.get_json(silent=True) or {}
    try:
        fr, fc, tr, tc = [int(body[k]) for k in ('from_row', 'from_col', 'to_row', 'to_col')]
    except Exception:
        return {'success': False, 'error': '着手情報が不正です'}, 400
    if not all(0 <= x < 8 for x in (fr, fc, tr, tc)):
        return {'success': False, 'error': '着手位置が不正です'}, 400

    # JSON文字列をリストに読み込んで操作する
    try:
        board = json.loads(r['board'])
    except Exception:
        return {'success': False, 'error': '盤面データの読み込みに失敗しました'}, 500

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

    new_board_json = json.dumps(board)
    new_ep_json = json.dumps(new_en_passant) if new_en_passant else None
    now = datetime.utcnow().isoformat()
    query_d1(
        'UPDATE chess_rooms SET board=?,turn=?,updated_at=?,in_check=?,status=?,winner=?,castling=?,en_passant=? WHERE room_code=? AND turn=?',
        [new_board_json, next_color, now, (next_color if next_in_check else None), new_status, winner, new_castling, new_ep_json, code, color]
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

    search_query = request.args.get('q', default='', type=str).strip()

    category = request.args.get('category', default='', type=str).strip()
    if category not in THREAD_CATEGORY_VALUES:
        category = ''

    sort = request.args.get('sort', default=DEFAULT_THREAD_SORT, type=str).strip()
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
    is_admin_user = can_manage_board()

    response = make_response(render_template(
        'index.html', 
        threads=threads, 
        admin_message=admin_message, 
        is_admin_user=is_admin_user, 
        active_count=active_count,
        current_page=page,      
        has_next=has_next,
        search_query=search_query,
        thread_categories=THREAD_CATEGORIES,
        thread_category_labels=THREAD_CATEGORY_LABELS,
        thread_category_colors=THREAD_CATEGORY_COLORS,
        current_category=category,
        thread_sort_options=THREAD_SORT_OPTIONS,
        current_sort=sort,
        current_year=datetime.utcnow().year,
        category_meta_json=json.dumps(
            {key: {'label': label, 'color': color} for key, label, color in THREAD_CATEGORIES},
            ensure_ascii=False
        ),
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

    category = request.form.get('category', default=DEFAULT_THREAD_CATEGORY, type=str)
    if category not in THREAD_CATEGORY_VALUES:
        category = DEFAULT_THREAD_CATEGORY
    
    is_admin = can_manage_board()
    now = time.time()

    thread_cooldown = 300
    if not is_admin and is_proxy_or_vpn(client_ip):
        thread_cooldown = 900

    if not is_admin:
        if client_ip in LAST_THREAD_TIMES and now - LAST_THREAD_TIMES[client_ip] < thread_cooldown:
            remaining_time = int(thread_cooldown - (now - LAST_THREAD_TIMES[client_ip]))
            minutes = remaining_time // 60
            seconds = remaining_time % 60
            return {"error": f"スレッド作成は5分に1回までです。(proxy,VPNは15分）あと {minutes}分 {seconds}秒 お待ちください。"}, 429
            
    LAST_THREAD_TIMES[client_ip] = now 
    
    try:
        query_d1(
            "INSERT INTO threads (title, ip_address, category) VALUES (?, ?, ?)",
            [title, client_ip, category]
        )
        res = query_d1("SELECT * FROM threads ORDER BY id DESC LIMIT 1")
        new_thread = res[0] if res else None
        if new_thread and not new_thread.get('category'):
            new_thread['category'] = category
    except Exception as e:
        print(f"スレッド作成エラー: {e}")
        return {"error": "データベースエラーが発生しました"}, 500
        
    return {"success": True, "thread": new_thread}

# --- 「もっと見る」用: 過去のレスを追加読み込みするAPI ---
@app.route('/thread/<int:thread_id>/get_older_replies')
def get_older_replies(thread_id):
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return {"success": False, "error": "Banned"}, 403

    before_id = request.args.get('before_id', type=int)
    if not before_id:
        return {"success": False, "error": "before_idが必要です", "replies": [], "has_more": False}, 400

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

        thread_res = query_d1("SELECT ip_address FROM threads WHERE id = ?", [thread_id])
        op_ip = thread_res[0]['ip_address'] if thread_res else None
        op_user_id = get_daily_user_id(op_ip) if op_ip else None

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
            reply_dict['post_num'] = start_num + i
            formatted_replies.append(reply_dict)

        return {"success": True, "replies": formatted_replies, "has_more": count_before > len(older_replies)}, 200
    except Exception as e:
        print(f"過去レス取得エラー: {e}")
        return {"success": False, "error": "データベースエラー", "replies": [], "has_more": False}, 500

# --- リアルタイム自動更新用API ---
@app.route('/thread/<int:thread_id>/get_new_replies')
def get_new_replies(thread_id):
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return {"success": False, "error": "Banned"}, 403

    after_id = request.args.get('after_id', default=0, type=int)
    try:
        replies = query_d1(
            "SELECT * FROM replies WHERE thread_id = ? AND id > ? ORDER BY id ASC",
            [thread_id, after_id]
        )
        if not replies:
            replies = []

        thread_res = query_d1("SELECT ip_address FROM threads WHERE id = ?", [thread_id])
        op_ip = thread_res[0]['ip_address'] if thread_res else None
        op_user_id = get_daily_user_id(op_ip) if op_ip else None

        total_count_res = query_d1("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id])
        total_reply_count = total_count_res[0]['cnt'] if total_count_res else 0
        start_num = total_reply_count - len(replies) + 1

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
            reply_dict['post_num'] = start_num + idx
            formatted_replies.append(reply_dict)

        return {"success": True, "replies": formatted_replies}, 200
    except Exception as e:
        print(f"新着レス取得エラー: {e}")
        return {"success": False, "error": "データベースエラー", "replies": []}, 500

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
            # 同一クライアントから同じレスが短時間に二重送信された場合を防止。
            # フロント側の二重イベント登録や通信リトライがあってもDBへ二重保存しない。
            reply_signature = hashlib.sha256(
                f"{thread_id}|{client_ip}|{author_input}|{content}|{image_url}".encode('utf-8')
            ).hexdigest()
            signature_now = time.time()
            previous_signature_time = LAST_REPLY_SIGNATURES.get(reply_signature)
            if previous_signature_time is not None and signature_now - previous_signature_time < 5:
                return {"success": False, "duplicate": True, "error": "同じ内容が連続して送信されたため、重複投稿を防止しました。"}, 409

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
                        thread_res = query_d1("SELECT ip_address FROM threads WHERE id = ?", [thread_id])
                        op_ip = thread_res[0]['ip_address'] if thread_res else None
                        op_user_id = get_daily_user_id(op_ip) if op_ip else None
                        new_reply['is_op'] = bool(op_user_id) and new_reply.get('user_id') == op_user_id
                    except Exception as ope:
                        new_reply['is_op'] = False

                    try:
                        total_count_res = query_d1("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id])
                        new_reply['post_num'] = total_count_res[0]['cnt'] if total_count_res else None
                    except Exception:
                        new_reply['post_num'] = None

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
               WHERE id = ? AND thread_id = ?""",
            ['あぼーん', 'この書き込みは管理員によって削除されました。', '???', 0, '', reply_id, thread_id]
        )
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
