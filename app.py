from flask import Flask, render_template, request, redirect, url_for, make_response, session, jsonify
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
import psutil
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 7
app.secret_key = os.environ.get('SECRET_KEY', 'default-secure-secret-key')

# CPU使用率測定の初期化
psutil.cpu_percent(interval=None)

# --- Cloudflare D1 接続設定 ---
CF_D1_ACCOUNT_ID = os.environ.get('CF_D1_ACCOUNT_ID')
CF_D1_DATABASE_ID = os.environ.get('CF_D1_DATABASE_ID')
CF_D1_API_TOKEN = os.environ.get('CF_D1_API_TOKEN')

def query_d1(sql, params=None):
    """Cloudflare D1データベースに対してSQLクエリを実行するヘルパー関数"""
    if not CF_D1_ACCOUNT_ID or not CF_D1_DATABASE_ID or not CF_D1_API_TOKEN:
        return []
        
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_D1_ACCOUNT_ID}/d1/database/{CF_D1_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {CF_D1_API_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        resp = httpx.post(url, json={"sql": sql, "params": params or []}, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("result"):
                return data["result"][0].get("results", [])
    except Exception as e:
        print(f"D1 Query Error: {e}")
    return []

# --- サーバーメトリクス・環境監視用のヘルパー関数 ---
def read_cgroup_memory():
    """cgroup環境でのメモリ使用量・制限を取得"""
    try:
        with open('/sys/fs/cgroup/memory/memory.usage_in_bytes', 'r') as f:
            mem_used = int(f.read().strip())
        with open('/sys/fs/cgroup/memory/memory.limit_in_bytes', 'r') as f:
            mem_limit = int(f.read().strip())
        return mem_used, mem_limit
    except Exception:
        return None, None

def read_cgroup_cpu_percent():
    return None

def read_network_speed():
    return 0, 0

def can_manage_board():
    """掲示板の管理権限チェック（必要に応じて拡張）"""
    return True

# --- ルーティング定義 ---

@app.route('/')
def index():
    """スレッド一覧画面"""
    threads = query_d1("SELECT * FROM threads ORDER BY created_at DESC")
    return render_template('index.html', threads=threads)

@app.route('/thread/<thread_id>')
def thread_detail(thread_id):
    """スレッド詳細画面（レス一覧表示）"""
    threads = query_d1("SELECT * FROM threads WHERE id = ?", [thread_id])
    if not threads:
        return "スレッドが見つかりません", 404
    thread = threads[0]
    
    replies = query_d1("SELECT * FROM replies WHERE thread_id = ? ORDER BY created_at ASC", [thread_id])
    
    # 各レスにレス番号（1オリジン）を動的に付与
    for idx, r in enumerate(replies, start=1):
        r['res_no'] = idx
        
    return render_template('thread.html', thread=thread, replies=replies)

@app.route('/thread/<thread_id>/reply', methods=['POST'])
def post_reply(thread_id):
    """非同期レス投稿処理（JSONを返却）"""
    try:
        content = request.form.get('content', '').strip()
        if not content:
            return jsonify({'success': False, 'error': '本文が空です。'}), 400
            
        reply_id = str(uuid.uuid4())
        created_at = datetime.utcnow().isoformat()
        
        # データベースへの保存クエリ（実際のD1スキーマに合わせて調整してください）
        # query_d1("INSERT INTO replies (id, thread_id, content, created_at) VALUES (?, ?, ?, ?)", [reply_id, thread_id, content, created_at])
        
        # 現在のレス数を取得してレス番号を決定
        existing_replies = query_d1("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id])
        res_no = (existing_replies[0]['cnt'] if existing_replies else 0) + 1
        
        new_reply = {
            'id': reply_id,
            'thread_id': thread_id,
            'content': html.escape(content),
            'created_at': created_at,
            'res_no': res_no
        }
        
        return jsonify({'success': True, 'reply': new_reply})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/server_metrics')
def server_metrics():
    """サーバーのCPU・メモリ使用率などのメトリクスをJSONで返すエンドポイント"""
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
    rx_kbps = round(rx_speed / 1024, 1)
    tx_kbps = round(tx_speed / 1024, 1)

    return jsonify({
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "memory_used_mb": memory_used_mb,
        "memory_limit_mb": memory_limit_mb,
        "rx_kbps": rx_kbps,
        "tx_kbps": tx_kbps
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
