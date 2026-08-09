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

# Supabaseを使うためのライブラリを読み込み
from supabase import create_client, Client

import boto3  
import httpx  
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 7
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345')

# Cloudflare専用シークレット（設定されている場合）
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

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://mpzjidhuovorzvtvpwyp.supabase.co')
# ※ここでSupabaseダッシュボードからコピーした正しいanonキーを設定してください
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1wemppZGh1b3Zvcnp2amh1a215Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIwMDYzMjIsImV4cCI6MjA5NzU4MjMyMn0.Q11dCsMYX0LakWydaVD6EIKKJD2Wbv7qHV0GuAyxEeo') 
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ユーザー識別用トークン発行
def get_or_create_user_token():
    token = request.cookies.get('user_bbs_token')
    is_new = False
    if not token:
        token = str(uuid.uuid4())
        is_new = True
    return token, is_new

def get_client_ip():
    if request.headers.get('CF-Connecting-IP'):
        return request.headers.get('CF-Connecting-IP')
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'

def get_daily_user_id(ip):
    today_str = datetime.utcnow().strftime('%Y-%m-%d')
    raw = f"{ip}-{today_str}-talk-ch-salt"
    h = hashlib.sha256(raw.encode()).hexdigest()
    return h[:8]

# オンライン人数管理
def update_and_get_user_counts(user_token, location_key):
    if not user_token:
        return 0
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=5)).isoformat()
    
    try:
        supabase.table('active_users').upsert({
            'user_token': user_token,
            'location': location_key,
            'last_seen': now.isoformat()
        }, on_conflict='user_token,location').execute()

        supabase.table('active_users').delete().lt('last_seen', cutoff).execute()

        res = supabase.table('active_users').select('user_token', count='exact').eq('location', location_key).execute()
        return res.count if res.count is not None else 1
    except Exception as e:
        print(f"アクティブユーザー処理エラー: {e}")
        return 1

# ==================== メイン画面 (index) ====================
@app.route('/')
def index():
    user_token, is_new_user = get_or_create_user_token()
    
    try:
        threads_res = supabase.table('threads').select('*').order('id', desc=False).execute()
        threads = threads_res.data or []

        pinned_res = supabase.table('pinned_threads').select('*').execute()
        pinned_threads = pinned_res.data or []

        for pt in pinned_threads:
            pt['is_pinned'] = True  
            threads.insert(0, pt)

        # 全スレッドのIDを収集し、一括でレス数を計算（フォールバック付き）
        all_thread_ids = [int(t['id']) for t in threads]
        reply_counts = {}
        if all_thread_ids:
            try:
                counts_res = supabase.rpc('get_reply_counts', {'thread_ids': all_thread_ids}).execute()
                for row in (counts_res.data or []):
                    reply_counts[row['thread_id']] = row['reply_count']
            except Exception as re:
                print(f"RPCレス数取得エラー(フォールバック実行): {re}")
                try:
                    fallback_res = supabase.table('replies').select('thread_id').in_('thread_id', all_thread_ids).execute()
                    for row in (fallback_res.data or []):
                        tid = row['thread_id']
                        reply_counts[tid] = reply_counts.get(tid, 0) + 1
                except Exception as e2:
                    print(f"フォールバック取得エラー: {e2}")

        for t in threads:
            t_id = int(t['id'])
            if t.get('is_pinned') or t_id in [1, 2, 3, 4]:
                t['is_pinned'] = True
            
            # レス数をセット（取得できなければ0）
            t['replies_count'] = reply_counts.get(t_id, 0)

            location_key = f"thread_{t_id}"
            t['thread_active_count'] = update_and_get_user_counts(user_token, location_key)

        othello_res = supabase.table('othello_games').select('*').order('created_at', desc=True).limit(20).execute()
        othello_games = othello_res.data or []

    except Exception as e:
        print(f"Index読込エラー: {e}")
        threads = []
        othello_games = []

    response = make_response(render_template('index.html', threads=threads, othello_games=othello_games))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

# ==================== スレッド作成・詳細・返信関連 ====================
@app.route('/create_thread', methods=['POST'])
def create_thread():
    user_token, is_new_user = get_or_create_user_token()
    client_ip = get_client_ip()
    author_id = get_daily_user_id(client_ip)

    title = request.form.get('title', '').strip()
    author = request.form.get('author', '').strip() or '名無しさん'
    content = request.form.get('content', '').strip()

    if not title or not content:
        return "タイトルと内容は必須です", 400

    author_with_id = f"{author} (ID:{author_id})"

    try:
        supabase.table('threads').insert({
            'title': title,
            'author': author_with_id,
            'content': content,
            'created_at': datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"スレッド作成エラー: {e}")
        return f"スレッド作成に失敗しました: {e}", 500

    response = make_response(redirect(url_for('index')))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/thread/<int:thread_id>')
def thread_detail(thread_id):
    user_token, is_new_user = get_or_create_user_token()
    try:
        t_res = supabase.table('threads').select('*').eq('id', thread_id).execute()
        if not t_res.data:
            return "スレッドが見つかりません", 404
        thread = t_res.data[0]

        r_res = supabase.table('replies').select('*').eq('thread_id', thread_id).order('id', desc=False).execute()
        replies = r_res.data or []
    except Exception as e:
        print(f"スレッド詳細読込エラー: {e}")
        return "読み込みエラーが発生しました", 500

    response = make_response(render_template('thread.html', thread=thread, replies=replies))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

# ==================== オセロ機能関連 ====================
def othello_new_board():
    board = [[' ' for _ in range(8)] for _ in range(8)]
    board[3][3] = 'W'
    board[3][4] = 'B'
    board[4][3] = 'B'
    board[4][4] = 'W'
    board_flat = [cell for row in board for cell in row]
    return "".join(board_flat)

def othello_board_to_2d(board_str):
    return [list(board_str[i*8:(i+1)*8]) for i in range(8)]

def othello_board_to_str(board_2d):
    return "".join(["".join(row) for row in board_2d])

def othello_count(board_str):
    b = board_str.count('B')
    w = board_str.count('W')
    return b, w

@app.route('/game/create', methods=['POST'])
def game_create():
    user_token, is_new_user = get_or_create_user_token()
    client_ip = get_client_ip()
    player_id = get_daily_user_id(client_ip)

    data = request.get_json(silent=True) or {}
    player_name = data.get('player_name', '名無しさん')[:20]
    black_name = f"{player_name} (ID:{player_id})"

    room_code = ''.join(os.urandom(3).hex().upper())
    for _ in range(5):
        existing = supabase.table('othello_games').select('room_code').eq('room_code', room_code).execute()
        if not existing.data:
            break
        room_code = ''.join(os.urandom(3).hex().upper())

    try:
        supabase.table('othello_games').insert({
            'room_code': room_code,
            'board': othello_new_board(),
            'turn': 'B',
            'player_black_token': user_token,
            'player_black_name': black_name,
            'status': 'waiting'
        }).execute()
    except Exception as e:
        print(f"オセロ部屋作成エラー: {e}")
        return {"error": "部屋の作成に失敗しました"}, 500

    response = make_response(redirect(url_for('game_room', room_code=room_code)))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/game/<room_code>')
def game_room(room_code):
    room_code = room_code.upper()
    user_token, is_new_user = get_or_create_user_token()
    try:
        res = supabase.table('othello_games').select('*').eq('room_code', room_code).execute()
    except Exception as e:
        return "部屋の取得に失敗しました", 500

    if not res.data:
        return "指定されたオセロの部屋が見つかりません", 404

    response = make_response(render_template('game.html', room_code=room_code))
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

@app.route('/game/<room_code>/join', methods=['POST'])
def game_join(room_code):
    user_token, is_new_user = get_or_create_user_token()
    client_ip = get_client_ip()
    player_id = get_daily_user_id(client_ip)
    
    data = request.get_json(silent=True) or {}
    player_name = data.get('player_name', '名無しさん')[:20]
    white_name = f"{player_name} (ID:{player_id})"

    room_code = room_code.upper()

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
            'player_white_name': white_name,
            'status': 'playing',
            'updated_at': datetime.utcnow().isoformat()
        }).eq('room_code', room_code).execute()
    except Exception as e:
        return {"success": False, "error": "参加処理でエラーが発生しました"}, 500

    response = make_response({"success": True})
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response

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

    return {
        "board": game['board'],
        "turn": game['turn'],
        "status": game['status'],
        "winner": game.get('winner'),
        "has_white": bool(game.get('player_white_token')),
        "my_color": my_color,
        "black_count": b_count,
        "white_count": w_count,
        "player_black_name": game.get('player_black_name', '黒プレイヤー'),
        "player_white_name": game.get('player_white_name', '白(待機中)')
    }

if __name__ == '__main__':
    app.run(debug=True)
