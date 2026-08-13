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

def update_and_get
