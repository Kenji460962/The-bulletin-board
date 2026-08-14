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
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 7

psutil.cpu_percent(interval=None)

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

# ---- cgroup 設定 ----
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

LAST_THREAD_TIMES = {}
LAST_REPLY_TIMES = {}
LAST_REPLY_SIGNATURES = {}

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
    # クライアントの盤面表現: 先頭文字が色(w/b)、2文字目が駒(KQRBNP)
    return ''.join([
        'bR','bN','bB','bQ','bK','bB','bN','bR',
        'bP','bP','bP','bP','bP','bP','bP','bP',
        '','','','','','','','',
        '','','','','','','','',
        '','','','','','','','',
        '','','','','','','','',
        'wP','wP','wP','wP','wP','wP','wP','wP',
        'wR','wN','wB','wQ','wK','wB','wN','wR'
    ])

def _chess_board():
    rows=[['bR','bN','bB','bQ','bK','bB','bN','bR'],['bP']*8,['']*8,['']*8,['']*8,['']*8,['wP']*8,['wR','wN','wB','wQ','wK','wB','wN','wR']]
    return ''.join(rows[r][c] for r in range(8) for c in range(8))

def _chess_pseudo(board, r, c):
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
            if 0<=rr<8 and 0<=cc<8 and board[rr*8+cc] and board[rr*8+cc][0]!=color: out.append((rr,cc))
    return out

def _cookie_response(resp, token):
    if not request.cookies.get('game_player_token'):
        resp.set_cookie('game_player_token', token, max_age=60*60*24*365, httponly=True, samesite='Lax')
    return resp




@app.route('/games')
def games_hub():
    return render_template('games_hub.html')

@app.route('/archive')
def archive_list():
    # 現在のthreadsテーブルには作成日時列がないため、最後のレス日時を基準に
    # 「一定期間動きのないスレ」を過去ログとして扱う。
    days = int(os.environ.get('ARCHIVE_AFTER_DAYS', '30') or 30)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    threads = query_d1(
        '''SELECT t.id, t.title, MAX(r.date) AS last_date
           FROM threads t
           LEFT JOIN replies r ON r.thread_id = t.id
           GROUP BY t.id, t.title
           HAVING last_date IS NOT NULL AND last_date < ?
           ORDER BY t.id DESC LIMIT 100''',
        [cutoff]
    )
    return render_template('archive.html', threads=threads, archive_after_days=days)

@app.route('/game')
def game_lobby():
    resp=make_response(render_template('game.html', room=None, my_color=None))
    return _cookie_response(resp, _game_token())

@app.route('/game/create', methods=['POST'])
def game_create():
    token=_game_token(); name=_game_name(); code=_new_room_code(); now=datetime.utcnow().isoformat()
    query_d1('INSERT INTO othello_rooms (room_code,black_token,black_name,white_token,white_name,board,turn,status,winner,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)', [code,token,name,None,None,_initial_othello(),'B','waiting',None,now,now])
    resp=redirect(url_for('game_room',room_code=code)); return _cookie_response(resp,token)

@app.route('/game/<room_code>')
def game_room(room_code):
    rows=query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1',[room_code.upper()])
    if not rows: return redirect(url_for('game_lobby'))
    room=rows[0]; token=_game_token(); my_color='B' if room.get('black_token')==token else ('W' if room.get('white_token')==token else None)
    resp=make_response(render_template('game.html',room=room,my_color=my_color)); return _cookie_response(resp,token)

@app.route('/game/<room_code>/join',methods=['POST'])
def game_join(room_code):
    code=room_code.upper(); rows=query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1',[code])
    if not rows: return {'success':False,'error':'部屋が見つかりません'},404
    room=rows[0]; token=_game_token(); name=_game_name()
    if room.get('black_token')==token or room.get('white_token')==token: return {'success':True}
    if room.get('white_token'): return {'success':False,'error':'この部屋は満員です'},409
    query_d1('UPDATE othello_rooms SET white_token=?,white_name=?,status=?,updated_at=? WHERE room_code=?',[token,name,'playing',datetime.utcnow().isoformat(),code])
    return {'success':True}

@app.route('/api/game/<room_code>/state')
def game_state(room_code):
    rows=query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1',[room_code.upper()])
    if not rows: return {'error':'not found'},404
    r=rows[0]; token=_game_token(); my='B' if r.get('black_token')==token else ('W' if r.get('white_token')==token else None)
    b=r['board']; bc=b.count('B'); wc=b.count('W');
    valid=_othello_valid(b,r['turn']) if r['status']=='playing' else []
    return {'success':True,'room_code':r['room_code'],'board':b,'turn':r['turn'],'status':r['status'],'winner':r['winner'],'black_name':r.get('black_name') or '名無しさん','white_name':r.get('white_name') or '名無しさん','black_id':(r.get('black_token') or '')[:4],'white_id':(r.get('white_token') or '')[:4],'has_white':bool(r.get('white_token')),'my_color':my,'black_count':bc,'white_count':wc,'valid_moves':valid}

@app.route('/game/<room_code>/move',methods=['POST'])
def game_move(room_code):
    code=room_code.upper(); rows=query_d1('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1',[code])
    if not rows: return {'success':False,'error':'部屋が見つかりません'},404
    r=rows[0]; token=_game_token(); player='B' if r.get('black_token')==token else ('W' if r.get('white_token')==token else None)
    if not player: return {'success':False,'error':'観戦者は着手できません'},403
    if r['status']!='playing': return {'success':False,'error':'対局は終了しています'}
    if r['turn']!=player: return {'success':False,'error':'相手のターンです'}
    body=request.get_json(silent=True) or {}; rr=int(body.get('row',-1)); cc=int(body.get('col',-1))
    newb=_othello_apply(r['board'],player,rr,cc)
    if newb is None: return {'success':False,'error':'そこには置けません'}
    opp='W' if player=='B' else 'B'; next_turn=opp; status='playing'; winner=None
    if not _othello_valid(newb,opp):
        if _othello_valid(newb,player): next_turn=player
        else:
            status='finished'; bc=newb.count('B'); wc=newb.count('W'); winner='B' if bc>wc else ('W' if wc>bc else 'draw')
    now=datetime.utcnow().isoformat(); query_d1('UPDATE othello_rooms SET board=?,turn=?,status=?,winner=?,updated_at=? WHERE room_code=? AND turn=?',[newb,next_turn,status,winner,now,code,player])
    return {'success':True}

@app.route('/chess')
def chess_lobby():
    resp=make_response(render_template('chess.html',room=None,my_color=None)); return _cookie_response(resp,_game_token())

@app.route('/chess/create',methods=['POST'])
def chess_create():
    token=_game_token(); name=_game_name(); code=_new_room_code(); now=datetime.utcnow().isoformat()
    query_d1('INSERT INTO chess_rooms (room_code,white_token,white_name,black_token,black_name,board,turn,status,winner,in_check,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[code,token,name,None,None,_chess_board(),'w','waiting',None,None,now,now])
    resp=redirect(url_for('chess_room',room_code=code)); return _cookie_response(resp,token)

@app.route('/chess/<room_code>')
def chess_room(room_code):
    rows=query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1',[room_code.upper()])
    if not rows: return redirect(url_for('chess_lobby'))
    r=rows[0]; token=_game_token(); my='w' if r.get('white_token')==token else ('b' if r.get('black_token')==token else None)
    resp=make_response(render_template('chess.html',room=r,my_color=my)); return _cookie_response(resp,token)

@app.route('/chess/<room_code>/join',methods=['POST'])
def chess_join(room_code):
    code=room_code.upper(); rows=query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1',[code])
    if not rows: return {'success':False,'error':'部屋が見つかりません'},404
    r=rows[0]; token=_game_token(); name=_game_name()
    if r.get('white_token')==token or r.get('black_token')==token: return {'success':True}
    if r.get('black_token'): return {'success':False,'error':'この部屋は満員です'},409
    query_d1('UPDATE chess_rooms SET black_token=?,black_name=?,status=?,updated_at=? WHERE room_code=?',[token,name,'playing',datetime.utcnow().isoformat(),code])
    return {'success':True}

@app.route('/api/chess/<room_code>/state')
def chess_state(room_code):
    rows=query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1',[room_code.upper()])
    if not rows: return {'error':'not found'},404
    r=rows[0]; token=_game_token(); my='w' if r.get('white_token')==token else ('b' if r.get('black_token')==token else None)
    return {'success':True,'room_code':r['room_code'],'board':r['board'],'turn':r['turn'],'status':r['status'],'winner':r['winner'],'in_check':r['in_check'],'white_name':r.get('white_name') or '名無しさん','black_name':r.get('black_name') or '名無しさん','white_id':(r.get('white_token') or '')[:4],'black_id':(r.get('black_token') or '')[:4],'has_black':bool(r.get('black_token')),'my_color':my}

@app.route('/chess/<room_code>/move',methods=['POST'])
def chess_move(room_code):
    code=room_code.upper(); rows=query_d1('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1',[code])
    if not rows: return {'success':False,'error':'部屋が見つかりません'},404
    r=rows[0]; token=_game_token(); color='w' if r.get('white_token')==token else ('b' if r.get('black_token')==token else None)
    if not color: return {'success':False,'error':'観戦者は着手できません'},403
    if r['status']!='playing': return {'success':False,'error':'対局は終了しています'}
    if r['turn']!=color: return {'success':False,'error':'相手のターンです'}
    body=request.get_json(silent=True) or {}
    try: fr,fc,tr,tc=[int(body[k]) for k in ('from_row','from_col','to_row','to_col')]
    except Exception: return {'success':False,'error':'着手情報が不正です'},400
    if not all(0<=x<8 for x in (fr,fc,tr,tc)): return {'success':False,'error':'着手位置が不正です'},400
    board=r['board']; piece=board[fr*8+fc]
    if not piece or piece[0]!=color: return {'success':False,'error':'自分の駒を選んでください'}
    if (tr,tc) not in _chess_pseudo(board,fr,fc): return {'success':False,'error':'その駒はそこへ動かせません'}
    a=list(board); a[tr*8+tc]=piece; a[fr*8+fc]=''
    # 簡易昇格: 最終段でポーンをクイーンにする
    if piece[1]=='P' and tr in (0,7): a[tr*8+tc]=color+'Q'
    newb=''.join(a); nextc='b' if color=='w' else 'w'; now=datetime.utcnow().isoformat()
    # この既存HTMLは盤面/手番表示を中心にしているため、王手・詰み判定は簡易運用
    query_d1('UPDATE chess_rooms SET board=?,turn=?,updated_at=? WHERE room_code=? AND turn=?',[newb,nextc,now,code,color])
    return {'success':True}


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
