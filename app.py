from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool
from datetime import datetime, timedelta
import json
import html
import os
import hashlib
import uuid
import time
import re
import asyncio
import random
from urllib.parse import unquote, urlparse
import httpx
import boto3
import psutil
import secrets
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from markupsafe import Markup, escape
import io
from PIL import Image, ImageOps
import uvicorn
import redis.asyncio as aioredis
import aiosqlite

load_dotenv()

app = FastAPI()


templates = Jinja2Templates(directory="templates")


# =========================
# 複数worker間でのリアルタイム配信共有(Redis Pub/Sub)
# =========================
# ConnectionManager/DMConnectionManagerが持つWebSocket接続は各workerプロセスの
# メモリ上にしか存在しない。そのため「投稿を受け付けたworker」と「閲覧者が
# 繋がっているworker」が別プロセスだと、素の状態ではリアルタイム配信が届かない。
# これを解決するため、実際の配信はRedisのPub/Subを経由して全workerに伝播させる。
# REDIS_URLが未設定/接続不可の場合は、単一worker構成のときと同じく
# 「自プロセス内の接続にのみ配信」にフォールバックする(動作は継続するが、
# 複数worker構成では配信が届かない閲覧者が出ることに注意)。
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
REALTIME_CHANNEL = 'bbs:realtime'
redis_client = None
_redis_listener_task = None
_cache_cleanup_task = None

# ---------------------------------------------------------------------
# 共有 non-blocking HTTP クライアント
# ---------------------------------------------------------------------
# D1 / proxycheck / Supabase への呼び出しはすべてこのクライアントに集約して
# 接続(TCP+TLS)を使い回す。旧実装は httpx.post/get をその都度呼んでいたため
# 毎回ハンドシェイクが発生し、さらに run_in_threadpool 経由で anyio の
# スレッド上限(既定40)を消費していた。スレッドが枯渇するとイベントループが
# 実質停止し、リバースプロキシのタイムアウトで 504 が返る原因になっていた。
_http_client: httpx.AsyncClient | None = None

D1_QUERY_TIMEOUT = httpx.Timeout(6.0, connect=3.0)   # D1 REST 用の明示タイムアウト(リトライ込みでもnginxの30s以内に収める)
D1_MAX_RETRIES = 1                                   # 読み取りクエリの再試行回数(最悪2回×6s=12sで打ち切る)
D1_SLOW_QUERY_MS = 500.0                             # 低速クエリ警告の閾値(ms)
_d1_slow_query_total = 0                             # /api/server_stats 表示用カウンタ

# ---------------------------------------------------------------------
# アクセス解析(page_views)書き込みキュー
# ---------------------------------------------------------------------
# 旧実装はリクエストごとに asyncio.to_thread で新規スレッドを生成していた。
# bot 等の連打でスレッド枠が枯渇し、本来のユーザー応答が順番待ちになる。
# 有界キュー + 専用ライター1本に変え、あふれたら記録を捨てる(応答を優先)。
PV_QUEUE_MAXSIZE = 1000
_pv_queue: asyncio.Queue = asyncio.Queue(maxsize=PV_QUEUE_MAXSIZE)
_pv_writer_task = None


async def _redis_listener():
    """全workerで常駐し、他workerがpublishしたイベントを受け取って
    自プロセス内のWebSocket接続にだけ配信する(=各プロセスが自分の担当分だけ配る)。

    旧実装は CancelledError しか捕捉していなかったため、Redisの瞬断で
    このタスクが黙って死ぬと以降リアルタイム配信が二度と復活しなかった。
    例外時は指数バックオフで購読を張り直す。"""
    backoff = 1.0
    while True:
        try:
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(REALTIME_CHANNEL)
            backoff = 1.0  # 購読に成功したらバックオフを戻す
            async for message in pubsub.listen():
                if message.get('type') != 'message':
                    continue
                try:
                    data = json.loads(message['data'])
                except Exception:
                    continue
                kind = data.get('kind')
                try:
                    if kind == 'thread_reply':
                        await manager.broadcast_local(data['thread_id'], data['reply'])
                    elif kind == 'dm_message':
                        await dm_manager.send_to_user_local(data['user_id'], data['payload'])
                except Exception as e:
                    print(f"Redisリアルタイム配信の処理エラー: {e}")
        except asyncio.CancelledError:
            # シャットダウン要求。ここは静かに抜ける。
            raise
        except Exception as e:
            print(f"Redis受信ループ異常({e}) — {backoff:.0f}秒後に再接続します。")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


@app.on_event("startup")
async def _startup_redis():
    global redis_client, _redis_listener_task, _cache_cleanup_task
    global _http_client, _pv_writer_task
    # 共有HTTPクライアントは「動いているイベントループ」の中で生成する必要がある。
    # ここで1度だけ作ることで、以降すべてのD1/proxycheck呼び出しが
    # 接続プールとキープアライブを再利用できるようになる。
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    _prime_psutil_cpu()

    # ローカル SQLite(aiosqlite)コネクションを起動時に一度だけ確立する。
    # イベントループ稼働中に開く必要があるため、この起動フック内で生成する。
    await _get_db_conn()

    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        _redis_listener_task = asyncio.create_task(_redis_listener())
        print(f"Redis接続OK ({REDIS_URL})。複数worker間のリアルタイム配信が有効です。")
    except Exception as e:
        redis_client = None
        print(f"Redis接続エラー: {e} — リアルタイム配信は自プロセス内のみに縮退します。"
              f"複数worker構成ではリアルタイム配信が一部の閲覧者に届かなくなるため、"
              f"Redisの起動状況を確認してください。")

    # レート制限・重複投稿判定・プロキシ判定などに使うメモリ上の辞書は、
    # エントリを削除する仕組みがないと稼働時間とアクセス数に比例して
    # 際限なく肥大化し、最終的にメモリ上限超過でプロセスが落ちる原因になる。
    # そのため定期的に古いエントリを掃除するバックグラウンドタスクを起動しておく。
    _cache_cleanup_task = asyncio.create_task(_cleanup_memory_caches_loop())
    # アクセス解析の書き込みは専用ライター1本に集約する(スレッドを増やさない)
    _pv_writer_task = asyncio.create_task(_pv_writer())


@app.on_event("shutdown")
async def _shutdown_redis():
    global redis_client, _redis_listener_task, _cache_cleanup_task
    global _http_client, _pv_writer_task, _db_conn
    if _redis_listener_task:
        _redis_listener_task.cancel()
        try:
            await _redis_listener_task
        except (asyncio.CancelledError, Exception):
            pass
    if _cache_cleanup_task:
        _cache_cleanup_task.cancel()
        try:
            await _cache_cleanup_task
        except (asyncio.CancelledError, Exception):
            pass
    if _pv_writer_task:
        # キューに残っている分を書き切ってから止める(最大でも数秒)
        _pv_writer_task.cancel()
        try:
            await _pv_writer_task
        except (asyncio.CancelledError, Exception):
            pass
    if redis_client:
        try:
            await redis_client.close()
        except Exception:
            pass
    if _http_client:
        try:
            await _http_client.aclose()
        except Exception:
            pass
        _http_client = None
    if _db_conn:
        try:
            await _db_conn.close()
        except Exception:
            pass
        _db_conn = None


# =========================
# 自己紹介文などのURL自動リンク化フィルター
# =========================
# 既にDB側でエスケープ済みのテキストが渡ってきても二重エスケープにならないよう、
# このフィルターは「未エスケープの生テキスト」を受け取る前提で実装する。
# (プロフィール保存時のエスケープは行わず、表示時にここで一括してエスケープ＆リンク化する)
_URL_RE = re.compile(r'(https?://[^\s<>"\']+)')


def linkify(text: str) -> Markup:
    """テキストをHTMLエスケープした上で、URLだけを<a>タグに変換する。
    XSS対策として、text自体は必ずescape()を通してから組み立てる。
    """
    if not text:
        return Markup('')

    parts = []
    last_end = 0
    for m in _URL_RE.finditer(text):
        # URL以外の地の文はエスケープしてそのまま追加
        parts.append(escape(text[last_end:m.start()]))
        url = m.group(1)
        # 文末の句読点・括弧などをURLから除外する（よくある誤爆対策）
        trail = ''
        while url and url[-1] in '.,)>」』、。':
            trail = url[-1] + trail
            url = url[:-1]
        safe_url = escape(url)
        parts.append(Markup(
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer nofollow">{safe_url}</a>'
        ))
        parts.append(escape(trail))
        last_end = m.end()
    parts.append(escape(text[last_end:]))

    return Markup('').join(parts)


templates.env.filters['linkify'] = linkify


if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345')


app.add_middleware(SessionMiddleware, secret_key=FLASK_SECRET_KEY)
# HTML/JSONレスポンスをgzip圧縮する。thread.html/index.htmlなど数十~100KB超の
# ページが多いため、転送量削減の効果が大きい(min_size未満は圧縮コストの方が
# 高くつくのでそのまま返す)。
app.add_middleware(GZipMiddleware, minimum_size=1000)



def _prime_psutil_cpu():
    """psutil.cpu_percent() は「初回呼び出しが基準点」になるため、起動時に
    一度だけ呼んでおく。旧実装は import 直後に呼んでいたが、それは
    ライブラリ読み込み直後のCPU使用率を測ってしまうため起動フックへ移した。"""
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass


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

    async def broadcast_local(self, thread_id: int, reply: dict):
        """同一プロセス内でこのthread_idに接続しているWebSocketにのみ配信する。
        複数worker構成では、各プロセスがRedis経由でこれを呼び合うことで
        プロセスをまたいだ配信を実現する（下のbroadcast()を参照）。"""
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

    async def broadcast(self, thread_id: int, reply: dict):
        """全worker配下の接続に配信する。Redisが使えるときはpublishして
        全プロセスのリスナーにbroadcast_local()を呼ばせる。Redis未接続時は
        このプロセス内だけの配信にフォールバックする(単一worker構成と同じ挙動)。"""
        if redis_client is None:
            await self.broadcast_local(thread_id, reply)
            return
        try:
            await redis_client.publish(REALTIME_CHANNEL, json.dumps({
                "kind": "thread_reply", "thread_id": thread_id, "reply": reply
            }))
        except Exception as e:
            print(f"Redis publish エラー(thread broadcast): {e}")
            # Redis障害時でも、少なくとも自プロセス内の閲覧者には届ける
            await self.broadcast_local(thread_id, reply)


manager = ConnectionManager()


@app.websocket('/ws/thread/{thread_id}')
async def thread_ws(websocket: WebSocket, thread_id: int):
    await manager.connect(thread_id, websocket)
    try:
        while True:
            # クライアントは接続維持の確認用に定期的に 'ping' を送ってくる。
            # pongを返さないと、クライアント側が「死んだ接続」とみなして張り直す。
            msg = await websocket.receive_text()
            if msg == 'ping':
                await websocket.send_text('{"type":"pong"}')
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



@app.middleware("http")
async def response_to_uptimerobot(request: Request, call_next):
    if request.method == 'HEAD':
        return Response(content='', status_code=200)
    return await call_next(request)


# =========================
# 簡易アクセス解析
# トップページとスレッド閲覧のPVだけを対象に、非同期(別スレッド)でD1へ記録する。
# レスポンスをブロックしないよう、書き込みはfire-and-forestで投げっぱなしにする。
# =========================

_THREAD_PATH_RE = re.compile(r'^/thread/(\d+)$')


def _classify_user_agent(ua: str):
    ua = (ua or '').lower()
    if 'ipad' in ua or ('tablet' in ua and 'mobile' not in ua):
        device = 'tablet'
    elif 'mobile' in ua or 'iphone' in ua or 'android' in ua:
        device = 'mobile'
    else:
        device = 'pc'

    if 'edg/' in ua:
        browser = 'Edge'
    elif 'opr/' in ua or 'opera' in ua:
        browser = 'Opera'
    elif 'chrome/' in ua:
        browser = 'Chrome'
    elif 'firefox/' in ua:
        browser = 'Firefox'
    elif 'safari/' in ua:
        browser = 'Safari'
    else:
        browser = 'Other'
    return device, browser


def _log_page_view_enqueue(path, thread_id, visitor_token, referrer_host, device, browser):
    """アクセス解析の1件を書き込みキューに積む(ノンブロッキング)。

    旧実装はリクエストごとに asyncio.to_thread で新規スレッドを生成しており、
    GET / の連打だけでスレッド枠が枯渇し、本来のユーザー応答が待たされていた。
    キューが満杯のときは記録を捨てる(応答速度を最優先する)。
    """
    try:
        _pv_queue.put_nowait(
            (path, thread_id, visitor_token, referrer_host, device, browser)
        )
    except asyncio.QueueFull:
        pass


async def _pv_writer():
    """キューに積まれたアクセス解析を、専用の1タスクから順にDBへ書く。
    スレッドを新規生成しないので、アクセス急増時もユーザー応答を圧迫しない。"""
    sql = ("INSERT INTO page_views (path, thread_id, visitor_token, "
           "referrer_host, device, browser) VALUES (?, ?, ?, ?, ?, ?)")
    while True:
        args = await _pv_queue.get()
        try:
            await execute_query(sql, list(args))
        except Exception as e:
            print(f"アクセス解析ログエラー: {e}")
        finally:
            _pv_queue.task_done()


@app.middleware("http")
async def track_page_views(request: Request, call_next):
    response = await call_next(request)

    try:
        if request.method != 'GET':
            return response

        path = request.url.path
        thread_id = None
        if path != '/':
            m = _THREAD_PATH_RE.match(path)
            if not m:
                return response  # トップページとスレッド閲覧以外は記録しない
            thread_id = int(m.group(1))

        # 「ユニーク」の目安として既存のuser_bbs_tokenを使う。無ければ初回訪問なので
        # IP+UAから一時的なキーを作る(cookieの発行自体はルート側の責務のまま触らない)。
        visitor_token = request.cookies.get('user_bbs_token')
        if not visitor_token:
            client_ip = get_client_ip(request)
            ua_for_hash = request.headers.get('user-agent', '')
            visitor_token = 'tmp:' + hashlib.sha256(f"{client_ip}:{ua_for_hash}".encode()).hexdigest()[:16]

        referrer_host = ''
        referrer = request.headers.get('referer', '')
        if referrer:
            try:
                host = urlparse(referrer).netloc
                if host and host != request.url.netloc:
                    referrer_host = host
            except Exception:
                referrer_host = ''

        device, browser = _classify_user_agent(request.headers.get('user-agent', ''))

        # 有界キューに積むだけ(ノンブロッキング)。実際の書き込みは _pv_writer が行う。
        _log_page_view_enqueue(
            path, thread_id, visitor_token, referrer_host, device, browser
        )
    except Exception as e:
        print(f"アクセス解析トラッキングエラー: {e}")

    return response



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


THREAD_SORT_OPTIONS = [
    ('latest_activity', '最終更新順',              'last_activity DESC'),
    ('id_desc',          'スレ番号（最新順）',       't.id DESC'),
    ('id_asc',            'スレ番号（#1から順）',    't.id ASC'),
    ('replies_desc',      'レス数が多い順',          'replies_count DESC'),
    ('viewers_desc',      '閲覧人数順',              'thread_active_count DESC'),
]
THREAD_SORT_SQL = {s[0]: s[2] for s in THREAD_SORT_OPTIONS}
DEFAULT_THREAD_SORT = 'latest_activity'


SQLITE_DB_PATH = os.environ.get('SQLITE_DB_PATH', '/var/www/app_database.db')

# 書き込み系(INSERT/UPDATE/DELETE/REPLACE)かどうかを判定する。
# 書き込みクエリは実行後に自動で commit() する。
_DB_WRITE_RE = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)

_db_conn: aiosqlite.Connection | None = None
# aiosqlite の1コネクションは内部的に単一スレッドで直列実行されるが、
# 「execute→(必要なら)fetch→commit」を1トランザクションとして扱うため、
# 呼び出し全体をこのロックで直列化し、他コルーチンの書き込みが
# commit前に割り込まない(=読み取りが未コミットの中間状態を見ない)ようにする。
_db_lock = asyncio.Lock()


async def _get_db_conn() -> aiosqlite.Connection:
    """起動フック完了前に呼ばれた場合の保険として、ここでも遅延初期化する
    (通常は _startup_db で初期化済みのものがそのまま返る)。"""
    global _db_conn
    if _db_conn is None:
        _db_conn = await aiosqlite.connect(SQLITE_DB_PATH)
        _db_conn.row_factory = aiosqlite.Row
        await _db_conn.execute("PRAGMA journal_mode=WAL")
        await _db_conn.execute("PRAGMA foreign_keys=ON")
        await _db_conn.commit()
    return _db_conn


async def execute_query(sql, params=None):
    """ローカル SQLite(aiosqlite)へ非同期で問い合わせる。

    旧実装(query_d1)と同じ呼び出し形式・戻り値形式を維持する:
    - SELECT: 辞書(dict)のリストを返す(カラム名でアクセス可能)。
    - INSERT/UPDATE/DELETE: 実行後に自動で commit() し、空リストを返す。
    """
    global _d1_slow_query_total
    conn = await _get_db_conn()
    is_write = bool(_DB_WRITE_RE.match(sql or ""))

    async with _db_lock:
        started = time.perf_counter()
        try:
            cursor = await conn.execute(sql, params or [])
            try:
                if is_write:
                    await conn.commit()
                    result = []
                else:
                    rows = await cursor.fetchall()
                    result = [dict(row) for row in rows]
            finally:
                await cursor.close()

            elapsed_ms = (time.perf_counter() - started) * 1000
            if elapsed_ms >= D1_SLOW_QUERY_MS:
                _d1_slow_query_total += 1
                print(f"SQLite 低速クエリ: {elapsed_ms:.0f}ms / {sql[:120]}")

            return result
        except Exception as e:
            try:
                await conn.rollback()
            except Exception:
                pass
            print(f"SQLite Query Error: {e} / {sql[:200] if sql else sql}")
            return []



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

ALLOWED_AVATAR_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
ALLOWED_AVATAR_CONTENT_TYPES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}
MAX_AVATAR_SIZE_BYTES = 3 * 1024 * 1024  # 3MB（アップロード時点の元ファイルに対する上限）

AVATAR_TARGET_SIZE = 256   # 変換後の一辺のピクセル数（正方形）
AVATAR_WEBP_QUALITY = 82   # WebPの圧縮品質(1-100)


def process_avatar_image(raw_bytes: bytes) -> bytes:
    """アップロードされた画像を
      1) EXIFの向き情報を反映して正立させる（EXIF自体は破棄しプライバシー保護）
      2) 中央を正方形にクロップ
      3) AVATAR_TARGET_SIZEにリサイズ（アップスケールはしない）
      4) 軽量なWebPに変換
    してbytesで返す。破損ファイルや非対応形式の場合はValueErrorを送出する。
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()  # ここで実際にデコードし、壊れた/偽装ファイルを検知する
    except Exception:
        raise ValueError("invalid_image")

    # GIFなどの複数フレーム画像は先頭フレームのみを使用する
    if getattr(img, "is_animated", False):
        img.seek(0)

    img = ImageOps.exif_transpose(img)

    has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
    img = img.convert('RGBA') if has_alpha else img.convert('RGB')

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))

    target = min(AVATAR_TARGET_SIZE, side)
    img = img.resize((target, target), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=AVATAR_WEBP_QUALITY, method=6)
    return buf.getvalue()


REPLY_IMAGE_MAX_DIMENSION = 1600  # 投稿画像の長辺の最大ピクセル数(アップスケールはしない)
REPLY_IMAGE_WEBP_QUALITY = 85


def process_reply_image(raw_bytes: bytes, orig_ext: str) -> tuple[bytes, str]:
    """投稿画像をアップロード前に軽量化する。
    スマホ撮影の写真(数MB・数千px四方)がそのままR2に置かれると、
    そのスレを開いた閲覧者全員がフルサイズをダウンロードすることになり
    表示速度に直結するため、長辺を上限内に縮小しWebPへ変換して容量を落とす。
    アニメーションGIFはWebP変換で動きが失われるため、サイズ上限チェックのみでそのまま返す。
    破損ファイルや非対応形式の場合はValueErrorを送出する(呼び出し側でアップロード自体をスキップする)。
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception:
        raise ValueError("invalid_image")

    if getattr(img, "is_animated", False):
        return raw_bytes, orig_ext

    img = ImageOps.exif_transpose(img)

    has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
    img = img.convert('RGBA') if has_alpha else img.convert('RGB')

    w, h = img.size
    longest = max(w, h)
    if longest > REPLY_IMAGE_MAX_DIMENSION:
        scale = REPLY_IMAGE_MAX_DIMENSION / longest
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=REPLY_IMAGE_WEBP_QUALITY, method=6)
    return buf.getvalue(), '.webp'


RESEND_API_KEY = os.environ.get('RESEND_API_KEY')
RESEND_FROM_EMAIL = os.environ.get('RESEND_FROM_EMAIL', 'noreply@example.com')
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'http://localhost:8080')


async def send_email(to_email: str, subject: str, html_body: str) -> bool:
    
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

# 「BANされていない」と判定したIPだけを短期キャッシュする。
# BAN(True)側は即時反映させたいのでキャッシュしない。
_BANNED_IP_CACHE = {}
_BANNED_IP_CACHE_TTL = 30


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

    ip = request.headers.get('CF-Connecting-IP')

    if not ip:
        ip = request.client.host if request.client else None

    return ip


PROXYCHECK_API_KEY = os.environ.get('PROXYCHECK_API_KEY', '')
_PROXY_CHECK_CACHE = {}
_PROXY_CACHE_TTL = 60 * 60 * 24


async def is_proxy_or_vpn(ip):
    """proxycheck.io でプロキシ/VPN判定する。
    旧実装は同期 httpx.get で、呼び出し側が run_in_threadpool に載せていた。
    共有 AsyncClient を使う非同期関数に変更し、スレッドを消費しないようにする。"""
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
        if _http_client is None:
            return False
        resp = await _http_client.get(
            f"https://proxycheck.io/v2/{ip}", params=params,
            timeout=httpx.Timeout(3.0, connect=2.5)
        )
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


async def is_banned_ip(ip):
    if not ip:
        return False
    # 全リクエストがここを通るため、D1往復を削る目的で「非BAN」だけ30秒キャッシュする。
    cached_at = _BANNED_IP_CACHE.get(ip)
    if cached_at is not None and time.time() - cached_at < _BANNED_IP_CACHE_TTL:
        return False
    try:
        res = await execute_query("SELECT * FROM banned_ips WHERE ip_address = ?", [ip])
        if not res:
            _BANNED_IP_CACHE[ip] = time.time()
        return len(res) > 0
    except Exception as e:
        print(f"BANチェックエラー: {e}")
        return False


async def is_banned_member_public_id(public_id):
    if not public_id:
        return False
    try:
        res = await execute_query("SELECT * FROM banned_members WHERE public_id = ?", [public_id])
        return len(res) > 0
    except Exception as e:
        print(f"会員BANチェックエラー: {e}")
        return False


async def is_banned_request(request: Request, client_ip) -> bool:

    if await is_banned_ip(client_ip):
        return True
    public_id = await get_member_public_id(request)
    if public_id and await is_banned_member_public_id(public_id):
        return True
    return False


def get_staff_role(request: Request):
    return request.session.get('staff_role')



BOARD_MANAGER_ROLES = ['admin', 'sub_admin']


def can_manage_board(request: Request):
    return request.session.get('staff_role') in BOARD_MANAGER_ROLES


USERNAME_RE = re.compile(
    r'^[A-Za-z0-9_\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uFF66-\uFF9F]{2,20}$'
)
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
TOKEN_EXPIRE_HOURS_VERIFY = 24
TOKEN_EXPIRE_HOURS_RESET = 1


async def get_current_member(request: Request):
    
    member_id = request.session.get('member_id')
    if not member_id:
        return None
    # 未キャッシュ時(初回)はどちらもD1往復が発生し得るため並列化しておく
    public_id, icon_path = await asyncio.gather(
        get_member_public_id(request),
        get_member_icon_path(request),
    )
    return {
        'id': member_id,
        'username': request.session.get('member_username'),
        'public_id': public_id,
        'icon_path': icon_path,
    }


def is_member_logged_in(request: Request) -> bool:
    return bool(request.session.get('member_id'))


async def _generate_public_id() -> str:

    for _ in range(5):
        candidate = secrets.token_hex(4)
        existing = await execute_query("SELECT id FROM users WHERE public_id = ?", [candidate])
        if not existing:
            return candidate
    return secrets.token_hex(6)


async def get_member_public_id(request: Request):
   
    if not is_member_logged_in(request):
        return None
    cached = request.session.get('member_public_id')
    if cached:
        return cached
    member_id = request.session.get('member_id')
    try:
        res = await execute_query("SELECT public_id FROM users WHERE id = ?", [member_id])
        public_id = res[0]['public_id'] if res else None
        if not public_id:
            public_id = await _generate_public_id()
            await execute_query("UPDATE users SET public_id = ? WHERE id = ?", [public_id, member_id])
        request.session['member_public_id'] = public_id
        return public_id
    except Exception as e:
        print(f"public_id取得エラー: {e}")
        return None


async def get_member_icon_path(request: Request):
    """ログイン中の会員のアバターURLをセッションにキャッシュしつつ返す。
    レス投稿のたびにusersテーブルへ問い合わせるのを避けるための軽量キャッシュ。
    アバターを更新した際は profile_avatar_upload 側でこのキャッシュを更新する。
    """
    if not is_member_logged_in(request):
        return None
    if 'member_icon_path' in request.session:
        return request.session['member_icon_path']
    member_id = request.session.get('member_id')
    try:
        res = await execute_query("SELECT icon_path FROM users WHERE id = ?", [member_id])
        icon_path = res[0]['icon_path'] if res else None
    except Exception as e:
        print(f"icon_path取得エラー: {e}")
        icon_path = None
    request.session['member_icon_path'] = icon_path
    return icon_path


def _make_token() -> str:
    return secrets.token_urlsafe(32)


async def _issue_token(user_id: int, purpose: str, expire_hours: int) -> str:
    token = _make_token()
    expires_at = (datetime.utcnow() + timedelta(hours=expire_hours)).isoformat()
    await execute_query(
        "INSERT INTO email_tokens (user_id, token, purpose, expires_at, used) VALUES (?, ?, ?, ?, 0)",
        [user_id, token, purpose, expires_at]
    )
    return token


async def _consume_token(token: str, purpose: str):
    
    res = await execute_query(
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
    await execute_query("UPDATE email_tokens SET used = 1 WHERE id = ?", [row['id']])
    user_res = await execute_query("SELECT * FROM users WHERE id = ?", [row['user_id']])
    return user_res[0] if user_res else None



async def _authenticate_user(username: str, password: str):

    try:
        res = await execute_query("SELECT * FROM users WHERE username = ?", [username])
    except Exception as e:
        print(f"ログインエラー: {e}")
        res = []
    user = res[0] if res else None
    if not user or not check_password_hash(user['password_hash'], password):
        return None
    return user


def _apply_login_session(request: Request, user: dict):
    role = user.get('role') or 'user'

    request.session['member_id'] = user['id']
    request.session['member_username'] = user['username']
    # 別アカウントへの切り替え時に古いキャッシュを引き継がないようにクリアしておく
    request.session.pop('member_public_id', None)
    request.session.pop('member_icon_path', None)

    if role != 'user':
        request.session['staff_id'] = user['id']
        request.session['staff_role'] = role
        request.session['staff_name'] = user['username']


@app.get('/login_secret_8823')
async def staff_login_form():
   
    return RedirectResponse(url='/login')


@app.post('/login_secret_8823')
async def staff_login(request: Request):
    return await member_login_submit(request)


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
        return render_error('ユーザー名は半角英数字・アンダースコア・日本語(ひらがな/カタカナ/漢字)で2〜20文字にしてください。')
    if len(password) < 8:
        return render_error('パスワードは8文字以上にしてください。')
    if password != password_confirm:
        return render_error('パスワードが一致しません。')
    if email and not EMAIL_RE.match(email):
        return render_error('メールアドレスの形式が正しくありません。')

    try:
        existing = await execute_query("SELECT id FROM users WHERE username = ?", [username])
        if existing:
            return render_error('そのユーザー名はすでに使われています。')
        if email:
            existing_email = await execute_query("SELECT id FROM users WHERE email = ?", [email])
            if existing_email:
                return render_error('そのメールアドレスはすでに登録されています。')

        password_hash = generate_password_hash(password)
        public_id = await _generate_public_id()
        await execute_query(
            "INSERT INTO users (username, password_hash, email, email_verified, public_id, role) VALUES (?, ?, ?, 0, ?, 'user')",
            [username, password_hash, email or None, public_id]
        )
        new_user_res = await execute_query("SELECT * FROM users WHERE username = ?", [username])
        if not new_user_res:
            return render_error('登録に失敗しました。もう一度お試しください。')
        new_user = new_user_res[0]
    except Exception as e:
        print(f"会員登録エラー: {e}")
        return render_error('データベースエラーが発生しました。')

    if email:
        token = await _issue_token(new_user['id'], 'verify', TOKEN_EXPIRE_HOURS_VERIFY)
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

    user = await _authenticate_user(username, password)
    if not user:
        return templates.TemplateResponse(
            request, 'login.html', {'error': 'ユーザー名またはパスワードが違います。'}, status_code=401
        )

    _apply_login_session(request, user)
    return RedirectResponse(url='/', status_code=303)


@app.get('/logout')
async def member_logout(request: Request):
    request.session.pop('member_id', None)
    request.session.pop('member_username', None)
    request.session.pop('member_public_id', None)
    request.session.pop('member_icon_path', None)
    return RedirectResponse(url='/')


@app.get('/verify_email/{token}')
async def verify_email(request: Request, token: str):
    user = await _consume_token(token, 'verify')
    if not user:
        return text_resp("確認リンクが無効か、有効期限が切れています。", 400)
    try:
        await execute_query("UPDATE users SET email_verified = 1 WHERE id = ?", [user['id']])
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
    
    if email:
        try:
            res = await execute_query("SELECT * FROM users WHERE email = ?", [email])
        except Exception as e:
            print(f"パスワードリセット検索エラー: {e}")
            res = []
        if res:
            user = res[0]
            token = await _issue_token(user['id'], 'reset', TOKEN_EXPIRE_HOURS_RESET)
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

    user = await _consume_token(token, 'reset')
    if not user:
        return templates.TemplateResponse(
            request, 'reset_password.html',
            {'token': token, 'error': 'リンクが無効か、有効期限が切れています。もう一度パスワード再設定をお試しください。'},
            status_code=400
        )

    try:
        await execute_query("UPDATE users SET password_hash = ? WHERE id = ?", [generate_password_hash(password), user['id']])
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


async def update_and_get_user_counts(current_token, location):
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=2)).isoformat()

    if current_token:
        sql_upsert = """
        INSERT INTO active_users (token, location, last_seen) 
        VALUES (?, ?, ?) 
        ON CONFLICT(token) DO UPDATE SET location=excluded.location, last_seen=excluded.last_seen
        """
        # 自分の在室情報の書き込みはレスポンスをブロックしない。
        # D1へのHTTP往復が1回増えるだけでページ表示が体感で遅くなるため、
        # バックグラウンドで実行し、結果を待たずに人数取得へ進む
        # （カウントが自分の分だけ1人分ズレることがあるが表示上は無害）。
        asyncio.create_task(execute_query(sql_upsert, [current_token, location, now.isoformat()]))

    sql_count = "SELECT COUNT(*) as cnt FROM active_users WHERE location = ? AND last_seen >= ?"
    res = await execute_query(sql_count, [location, cutoff])
    count = res[0]['cnt'] if res and len(res) > 0 else 0

    if random.random() < 0.05:
        # 古いレコードの掃除も同様にバックグラウンドへ逃がす
        asyncio.create_task(execute_query("DELETE FROM active_users WHERE last_seen < ?", [cutoff]))

    return count


@app.get('/api/lobby/active_count')
async def api_lobby_active_count(request: Request):
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True
    count = await update_and_get_user_counts(user_token, "lobby")
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
    async def top_n(game, n=30):
        # player_key が 'member:<users.id>' の行だけ users と結合し、
        # プロフィールリンク用のpublic_idとアイコンURLを一緒に取得する。
        # ゲストプレイヤーの行は public_id / icon_path が NULL になる。
        return await execute_query(
            "SELECT gr.display_name, gr.rating, gr.wins, gr.losses, gr.draws, "
            "       u.public_id AS public_id, u.icon_path AS icon_path "
            "FROM game_ratings gr "
            "LEFT JOIN users u ON gr.player_key = ('member:' || u.id) "
            "WHERE gr.game = ? ORDER BY gr.rating DESC LIMIT ?",
            [game, n]
        ) or []

    othello_ranking, chess_ranking, shogi_ranking = await asyncio.gather(
        top_n('othello'), top_n('chess'), top_n('shogi')
    )
    return templates.TemplateResponse(request, 'rankings.html', {
        'othello_ranking': othello_ranking,
        'chess_ranking': chess_ranking,
        'shogi_ranking': shogi_ranking,
    })


@app.get('/profile/edit')
async def profile_edit_form(request: Request):
    if not is_member_logged_in(request):
        return RedirectResponse(url='/login')
    member_id = request.session.get('member_id')
    res = await execute_query("SELECT username, bio, icon_path FROM users WHERE id = ?", [member_id])
    user = res[0] if res else {'username': request.session.get('member_username'), 'bio': '', 'icon_path': None}

    avatar_error_map = {
        'type': '画像の形式が正しくないか、対応していないファイルです（PNG/JPEG/WEBP/GIF）。',
        'size': f'画像サイズは{MAX_AVATAR_SIZE_BYTES // (1024 * 1024)}MB以内にしてください。',
        'upload': 'アップロード中にエラーが発生しました。時間をおいて再度お試しください。',
        'empty': 'ファイルが選択されていません。',
    }
    avatar_error = avatar_error_map.get(request.query_params.get('avatar_error'))

    return templates.TemplateResponse(
        request, 'profile_edit.html', {'user': user, 'error': None, 'avatar_error': avatar_error}
    )


@app.post('/profile/avatar')
async def profile_avatar_upload(request: Request):
    if not is_member_logged_in(request):
        return RedirectResponse(url='/login')
    member_id = request.session.get('member_id')
    public_id = await get_member_public_id(request)

    form = await request.form()
    upload = form.get('avatar')

    if upload is None or not getattr(upload, 'filename', ''):
        return RedirectResponse(url=f'/profile/edit?avatar_error=empty', status_code=303)

    orig_filename = secure_filename(upload.filename)
    ext = os.path.splitext(orig_filename)[1].lower()

    # 拡張子/Content-Typeでの一次チェック（本格的な検証はPillowでのデコード時に行う）
    if ext not in ALLOWED_AVATAR_EXTENSIONS or upload.content_type not in ALLOWED_AVATAR_CONTENT_TYPES:
        return RedirectResponse(url=f'/profile/edit?avatar_error=type', status_code=303)

    contents = await upload.read()
    if len(contents) > MAX_AVATAR_SIZE_BYTES:
        return RedirectResponse(url=f'/profile/edit?avatar_error=size', status_code=303)

    try:
        webp_bytes = await run_in_threadpool(process_avatar_image, contents)
    except ValueError:
        return RedirectResponse(url=f'/profile/edit?avatar_error=type', status_code=303)
    except Image.DecompressionBombError:
        # 画素数が異常に大きい画像（解凍爆弾対策のPillow標準チェックに抵触）
        return RedirectResponse(url=f'/profile/edit?avatar_error=size', status_code=303)
    except Exception as e:
        print(f"アバター画像処理エラー: {e}")
        return RedirectResponse(url=f'/profile/edit?avatar_error=upload', status_code=303)

    try:
        unique_filename = f"avatars/{public_id or member_id}_{uuid.uuid4().hex}.webp"
        await run_in_threadpool(
            s3_client.put_object,
            Bucket=R2_BUCKET_NAME,
            Key=unique_filename,
            Body=webp_bytes,
            ContentType='image/webp',
            # ファイル名がuuidで一意なので、ブラウザ/CDNに長期キャッシュさせて再取得の負荷を減らす
            CacheControl='public, max-age=31536000, immutable',
        )
        icon_path = f"{R2_PUBLIC_URL.rstrip('/')}/{unique_filename}"
        await execute_query("UPDATE users SET icon_path = ? WHERE id = ?", [icon_path, member_id])
        request.session['member_icon_path'] = icon_path  # レス投稿時に使うキャッシュも更新
    except Exception as e:
        print(f"アバターアップロードエラー: {e}")
        return RedirectResponse(url=f'/profile/edit?avatar_error=upload', status_code=303)

    return RedirectResponse(url=f'/profile/{public_id}', status_code=303)


@app.post('/profile/edit')
async def profile_edit_submit(request: Request):
    if not is_member_logged_in(request):
        return RedirectResponse(url='/login')
    member_id = request.session.get('member_id')
    form = await request.form()
    username = (form.get('username') or '').strip()
    # bioは生のまま保存し、表示側(linkifyフィルター)でエスケープ＆リンク化する。
    # (以前はここでhtml.escapeしていたため、テンプレート側の自動エスケープと合わさって
    #  二重エスケープになっていた点を修正)
    bio = (form.get('bio') or '').strip()[:200]

    def render_error(msg):
        return templates.TemplateResponse(
            request, 'profile_edit.html',
            {'user': {'username': username, 'bio': bio}, 'error': msg}, status_code=400
        )

    if not USERNAME_RE.match(username):
        return render_error('ユーザー名は半角英数字・アンダースコア・日本語(ひらがな/カタカナ/漢字)で2〜20文字にしてください。')

    try:
        existing = await execute_query("SELECT id FROM users WHERE username = ? AND id != ?", [username, member_id])
        if existing:
            return render_error('そのユーザー名はすでに使われています。')
        await execute_query("UPDATE users SET username = ?, bio = ? WHERE id = ?", [username, bio, member_id])
    except Exception as e:
        print(f"プロフィール更新エラー: {e}")
        return render_error('データベースエラーが発生しました。')

    request.session['member_username'] = username
    public_id = await get_member_public_id(request)
    return RedirectResponse(url=f'/profile/{public_id}', status_code=303)



def _calc_win_rate(wins: int, losses: int, draws: int):
    """勝率を計算して表示用文字列を返す。対局数0の場合は '-' を返す。"""
    total = (wins or 0) + (losses or 0) + (draws or 0)
    if total == 0:
        return '-'
    rate = (wins or 0) / total * 100
    if rate == round(rate):
        return f"{rate:.0f}%"
    return f"{rate:.1f}%"


@app.get('/profile/{public_id}')
async def profile_view(request: Request, public_id: str):
    # ユーザー本体・作成スレ数・総レス数はpublic_id(URLの値)だけで引けるので、
    # 互いに依存する所がなく最初からまとめて並列に投げられる。
    # (旧実装は「ユーザー取得→ゲーム3種を1つずつ→スレ数→レス数→…」と
    #  10回前後のD1往復を全部直列にしていたため、プロフィールが特に重かった)
    res, thread_count_res, reply_count_res = await asyncio.gather(
        execute_query(
            "SELECT id, username, public_id, bio, icon_path, created_at FROM users WHERE public_id = ?",
            [public_id]
        ),
        execute_query("SELECT COUNT(*) as cnt FROM threads WHERE user_id = ?", [public_id]),
        # threads.user_id には会員のpublic_idが入るが、replies.user_id は運営投稿時に "STAFF"
        # が入る仕様のため、レス数は必ず poster_public_id 側で数える。
        execute_query("SELECT COUNT(*) as cnt FROM replies WHERE poster_public_id = ?", [public_id]),
    )
    if not res:
        return text_resp("そのユーザーは見つかりませんでした。", 404)
    profile_user = res[0]
    board_stats = {
        'thread_count': thread_count_res[0]['cnt'] if thread_count_res else 0,
        'reply_count': reply_count_res[0]['cnt'] if reply_count_res else 0,
    }

    member_key = f"member:{profile_user['id']}"

    # ゲーム3種の成績・フォロー数・ログイン中会員情報・未読DM件数も互いに独立なので並列化
    othello_res, chess_res, shogi_res, follow_counts, current_member, unread_dm_count = await asyncio.gather(
        execute_query("SELECT rating, wins, losses, draws FROM game_ratings WHERE player_key = ? AND game = ?", [member_key, 'othello']),
        execute_query("SELECT rating, wins, losses, draws FROM game_ratings WHERE player_key = ? AND game = ?", [member_key, 'chess']),
        execute_query("SELECT rating, wins, losses, draws FROM game_ratings WHERE player_key = ? AND game = ?", [member_key, 'shogi']),
        get_follow_counts(profile_user['id']),
        get_current_member(request),
        get_unread_dm_count(request),
    )
    games = {}
    for g, gr in (('othello', othello_res), ('chess', chess_res), ('shogi', shogi_res)):
        if gr:
            row = gr[0]
            row['win_rate'] = _calc_win_rate(row.get('wins'), row.get('losses'), row.get('draws'))
            games[g] = row
        else:
            games[g] = None

    is_own_profile = bool(current_member) and str(current_member['id']) == str(profile_user['id'])

    # フォロー／ブロック／DMの状態（未ログイン・自分自身の場合はすべてFalse）
    follow_state = {'following': False, 'followed_by': False, 'mutual': False, 'blocked': False}
    if current_member and not is_own_profile:
        me = current_member['id']
        other = profile_user['id']
        following, followed_by, blocked_res = await asyncio.gather(
            is_following(me, other),
            is_following(other, me),
            execute_query(
                "SELECT 1 AS ok FROM blocks WHERE blocker_id = ? AND blocked_id = ? LIMIT 1",
                [me, other]
            ),
        )
        follow_state['following'] = following
        follow_state['followed_by'] = followed_by
        follow_state['mutual'] = following and followed_by
        follow_state['blocked'] = bool(blocked_res)

    return templates.TemplateResponse(request, 'profile.html', {
        'profile_user': profile_user,
        'games': games,
        'board_stats': board_stats,
        'is_own_profile': is_own_profile,
        'current_member': current_member,
        'follow_counts': follow_counts,
        'follow_state': follow_state,
        'report_reasons': REPORT_REASONS,
        'unread_dm_count': unread_dm_count,
    })


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


async def _new_room_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(30):
        code = ''.join(random.choice(alphabet) for _ in range(6))
        if (not await execute_query('SELECT 1 FROM othello_rooms WHERE room_code = ? LIMIT 1', [code])
                and not await execute_query('SELECT 1 FROM chess_rooms WHERE room_code = ? LIMIT 1', [code])
                and not await execute_query('SELECT 1 FROM shogi_rooms WHERE room_code = ? LIMIT 1', [code])):
            return code
    return uuid.uuid4().hex[:6].upper()


# =========================
# ゲームのレーティング(Elo)・ランキング機能

# =========================

ELO_K = 32
ELO_DEFAULT_RATING = 1500


async def _game_member_id(request: Request):
    """ログイン中ならそのmember idを文字列で返す(ゲストならNone)。部屋作成・参加時にrooms側へ保存しておく。"""
    member = await get_current_member(request)
    return str(member['id']) if member else None


def _rating_key(member_id_str, token: str) -> str:
    return f"member:{member_id_str}" if member_id_str else f"guest:{token}"


async def _get_or_init_rating(player_key: str, game: str, display_name: str):
    res = await execute_query("SELECT * FROM game_ratings WHERE player_key = ? AND game = ?", [player_key, game])
    if res:
        return res[0]
    now = datetime.utcnow().isoformat()
    await execute_query(
        "INSERT INTO game_ratings (player_key, game, display_name, rating, wins, losses, draws, updated_at) "
        "VALUES (?, ?, ?, ?, 0, 0, 0, ?)",
        [player_key, game, display_name, ELO_DEFAULT_RATING, now]
    )
    return {'player_key': player_key, 'game': game, 'display_name': display_name,
            'rating': ELO_DEFAULT_RATING, 'wins': 0, 'losses': 0, 'draws': 0}


async def apply_game_result(game: str, key_a: str, name_a: str, key_b: str, name_b: str, result_a: float):

    try:
        a = await _get_or_init_rating(key_a, game, name_a)
        b = await _get_or_init_rating(key_b, game, name_b)
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
        await execute_query(
            "UPDATE game_ratings SET rating=?, wins=?, losses=?, draws=?, display_name=?, updated_at=? "
            "WHERE player_key=? AND game=?",
            [new_ra, wa, la, da, name_a, now, key_a, game]
        )
        await execute_query(
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
   
    nb = board[:]
    piece = nb[r * 8 + c]
    color, typ = piece[0], piece[1]
    nb[r * 8 + c] = ''

    if typ == 'K' and abs(tc - c) == 2:
       
        nb[tr * 8 + tc] = piece
        row = r
        if tc == 6:
            nb[row * 8 + 7] = ''
            nb[row * 8 + 5] = color + 'R'
        elif tc == 2:
            nb[row * 8 + 0] = ''
            nb[row * 8 + 3] = color + 'R'
    elif typ == 'P' and c != tc and not board[tr * 8 + tc]:
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
    board = [''] * 81
    back_rank = ['L', 'N', 'S', 'G', 'K', 'G', 'S', 'N', 'L']
    for c in range(9):
        board[0 * 9 + c] = 'g' + back_rank[c]
        board[8 * 9 + c] = 's' + back_rank[c]
        board[2 * 9 + c] = 'gP'
        board[6 * 9 + c] = 'sP'
    board[1 * 9 + 1] = 'gR'
    board[1 * 9 + 7] = 'gB'
    board[7 * 9 + 1] = 'sB'
    board[7 * 9 + 7] = 'sR'
    return json.dumps(board)


def _shogi_forward(color):
    return -1 if color == 's' else 1


def _shogi_zone(color, r):
    return r <= 2 if color == 's' else r >= 6


def _shogi_forced_promotion(base_typ, color, tr):
    if base_typ in ('P', 'L'):
        return tr == (0 if color == 's' else 8)
    if base_typ == 'N':
        return tr <= 1 if color == 's' else tr >= 7
    return False


def _shogi_piece_moves(board, r, c):
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
    rows = await execute_query(
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
        def _fetch_archive():
            obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=archive_key)
            return obj['Body'].read()
        raw = await run_in_threadpool(_fetch_archive)
        payload = json.loads(raw.decode('utf-8'))
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
    """
    同期・ブロッキングなhttpx呼び出しを行う関数。
    このプロジェクトの他の同期I/O呼び出し(R2アクセス)と同様、
    必ず `await run_in_threadpool(_fetch_all_from_supabase, ...)` の形で呼ぶこと。
    直接awaitせずに呼ぶと、取得が終わるまでイベントループ全体がブロックされ、
    その間サイト全体が応答しなくなる(全ユーザーに影響する)。
    """
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


async def _d1_batch_insert(table, columns, rows, chunk_size):
    """複数行をまとめたINSERT OR IGNOREをchunk_size件ずつDBに流し込む"""
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
        await execute_query(sql, params)
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
        threads = await run_in_threadpool(
            _fetch_all_from_supabase, sb_url, sb_key, 'threads', 'id,title,created_at,ip_address'
        )
        replies = await run_in_threadpool(
            _fetch_all_from_supabase, sb_url, sb_key, 'replies',
            'id,thread_id,author,content,user_id,is_admin,image_url,ip_address,date,role'
        )
    except Exception as e:
        return json_resp({"error": f"Supabaseからの取得に失敗しました: {e}"}, 500)

    try:
        threads_inserted = await _d1_batch_insert(
            'threads', ['id', 'title', 'created_at', 'ip_address'], threads, chunk_size=200
        )
        replies_inserted = await _d1_batch_insert(
            'replies', ['id', 'thread_id', 'author', 'content', 'user_id', 'is_admin', 'image_url', 'ip_address', 'date', 'role'], replies, chunk_size=90
        )
    except Exception as e:
        return json_resp({"error": f"DBへの書き込みに失敗しました: {e}"}, 500)

    return {
        "threads_fetched": len(threads),
        "replies_fetched": len(replies),
        "threads_inserted_or_ignored": threads_inserted,
        "replies_inserted_or_ignored": replies_inserted
    }


@app.post('/internal/migrate-from-supabase-safe')
async def migrate_from_supabase_safe(request: Request):
    # ID衝突を避けるため、今のDBの最大IDより確実に大きい番号にずらしてから追加する版
    if not ARCHIVE_SECRET or request.headers.get('X-Archive-Secret') != ARCHIVE_SECRET:
        return json_resp({"error": "unauthorized"}, 403)

    sb_url = request.headers.get('X-Supabase-Url')
    sb_key = request.headers.get('X-Supabase-Key')
    if not sb_url or not sb_key:
        return json_resp({"error": "X-Supabase-Url / X-Supabase-Key ヘッダーが必要です"}, 400)

    try:
        max_tid_res = await execute_query("SELECT MAX(id) as m FROM threads", [])
        max_rid_res = await execute_query("SELECT MAX(id) as m FROM replies", [])
        current_max_tid = (max_tid_res[0]['m'] if max_tid_res and max_tid_res[0]['m'] is not None else 0)
        current_max_rid = (max_rid_res[0]['m'] if max_rid_res and max_rid_res[0]['m'] is not None else 0)
    except Exception as e:
        return json_resp({"error": f"現在のDBの最大IDの取得に失敗しました: {e}"}, 500)

    thread_offset = current_max_tid + 10000
    reply_offset = current_max_rid + 10000

    try:
        threads = await run_in_threadpool(
            _fetch_all_from_supabase, sb_url, sb_key, 'threads', 'id,title,created_at,ip_address'
        )
        replies = await run_in_threadpool(
            _fetch_all_from_supabase, sb_url, sb_key, 'replies',
            'id,thread_id,author,content,user_id,is_admin,image_url,ip_address,date,role'
        )
    except Exception as e:
        return json_resp({"error": f"Supabaseからの取得に失敗しました: {e}"}, 500)

    # ID・thread_idをまとめてずらす
    for t in threads:
        t['id'] = t['id'] + thread_offset
    for r in replies:
        r['id'] = r['id'] + reply_offset
        r['thread_id'] = r['thread_id'] + thread_offset

    try:
        threads_inserted = await _d1_batch_insert(
            'threads', ['id', 'title', 'created_at', 'ip_address'], threads, chunk_size=200
        )
        replies_inserted = await _d1_batch_insert(
            'replies', ['id', 'thread_id', 'author', 'content', 'user_id', 'is_admin', 'image_url', 'ip_address', 'date', 'role'], replies, chunk_size=90
        )
    except Exception as e:
        return json_resp({"error": f"DBへの書き込みに失敗しました: {e}"}, 500)

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

    def _list_archive_keys():
        paginator = s3_client.get_paginator('list_objects_v2')
        keys = []
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix='archive/'):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    keys.append(obj['Key'])
        return keys

    try:
        keys = await run_in_threadpool(_list_archive_keys)
    except Exception as e:
        return json_resp({"error": f"R2一覧の取得に失敗しました: {e}"}, 500)

    def _fetch_archive_json(key):
        obj = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return obj['Body'].read()

    for key in keys:
        try:
            m = re.search(r'thread_(\d+)\.json$', key)
            if not m:
                continue
            tid = int(m.group(1))

            raw = await run_in_threadpool(_fetch_archive_json, key)
            payload = json.loads(raw.decode('utf-8'))

            title = payload.get('thread', {}).get('title', '(無題)')
            reply_count = len(payload.get('replies', []))
            archived_at = payload.get('archived_at') or datetime.utcnow().isoformat()

            await execute_query(
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
        all_threads = await execute_query("SELECT * FROM threads", []) or []
    except Exception as e:
        return json_resp({"error": f"スレッド一覧の取得に失敗しました: {e}"}, 500)

    for t in all_threads:
        tid = int(t['id'])
        if tid in ARCHIVE_PINNED_IDS:
            continue

        try:
            last_reply_res = await execute_query(
                "SELECT date FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                [tid]
            )
            last_activity = last_reply_res[0]['date'] if last_reply_res else t.get('created_at')
            if not last_activity or last_activity > cutoff:
                continue

            all_replies = await execute_query(
                "SELECT * FROM replies WHERE thread_id = ? ORDER BY id ASC",
                [tid]
            ) or []

            archive_payload = {
                "thread": t,
                "replies": all_replies,
                "archived_at": datetime.utcnow().isoformat()
            }

            archive_key = f"archive/thread_{tid}.json"
            await run_in_threadpool(
                s3_client.put_object,
                Bucket=R2_BUCKET_NAME,
                Key=archive_key,
                Body=json.dumps(archive_payload, ensure_ascii=False, indent=2).encode('utf-8'),
                ContentType='application/json'
            )

            await execute_query("DELETE FROM replies WHERE thread_id = ?", [tid])
            await execute_query("DELETE FROM threads WHERE id = ?", [tid])

            await execute_query(
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
    member_id = await _game_member_id(request)
    code = await _new_room_code()
    now = datetime.utcnow().isoformat()
    await execute_query(
        '''INSERT INTO othello_rooms
           (room_code, black_token, black_name, black_member_id, white_token, white_name, board, turn, status, winner, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _initial_othello(), 'B', 'waiting', None, now, now]
    )
    resp = RedirectResponse(url=f'/game/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@app.get('/game/{room_code}')
async def game_room(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return RedirectResponse(url='/game')
    room = rows[0]
    token = _game_token(request)
    my_color = 'B' if room.get('black_token') == token else ('W' if room.get('white_token') == token else None)

    # 招待リンクを踏んだ2人目をその場で自動参加させる
    if my_color is None and not room.get('white_token') and room.get('black_token') != token:
        name = await _game_name(request)
        await execute_query(
            'UPDATE othello_rooms SET white_token=?,white_name=?,white_member_id=?,status=?,updated_at=? '
            'WHERE room_code=? AND white_token IS NULL',
            [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
        )
        rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
        room = rows[0]
        my_color = 'W' if room.get('white_token') == token else my_color

    resp = templates.TemplateResponse(request, 'game.html', {'room': room, 'my_color': my_color})
    return _cookie_response(request, resp, token)


@app.post('/game/{room_code}/join')
async def game_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    room = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if room.get('black_token') == token or room.get('white_token') == token:
        return {'success': True}
    if room.get('white_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    await execute_query(
        'UPDATE othello_rooms SET white_token=?,white_name=?,white_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@app.get('/api/game/{room_code}/state')
async def game_state(request: Request, room_code: str):
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
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
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
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
            await apply_game_result('othello', black_key, r.get('black_name') or '名無しさん',
                               white_key, r.get('white_name') or '名無しさん', result_black)

    now = datetime.utcnow().isoformat()
    await execute_query(
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
    member_id = await _game_member_id(request)
    code = await _new_room_code()
    now = datetime.utcnow().isoformat()
    await execute_query(
        '''INSERT INTO chess_rooms
           (room_code, white_token, white_name, white_member_id, black_token, black_name, board, turn, status, winner, in_check, castling, en_passant, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _chess_board(), 'w', 'waiting', None, None, 'KQkq', None, now, now]
    )
    resp = RedirectResponse(url=f'/chess/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@app.get('/chess/{room_code}')
async def chess_room(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return RedirectResponse(url='/chess')
    r = rows[0]
    token = _game_token(request)
    my = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)

    # 招待リンクを踏んだ2人目をその場で自動参加させる
    if my is None and not r.get('black_token') and r.get('white_token') != token:
        name = await _game_name(request)
        await execute_query(
            'UPDATE chess_rooms SET black_token=?,black_name=?,black_member_id=?,status=?,updated_at=? '
            'WHERE room_code=? AND black_token IS NULL',
            [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
        )
        rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
        r = rows[0]
        my = 'b' if r.get('black_token') == token else my

    resp = templates.TemplateResponse(request, 'chess.html', {'room': r, 'my_color': my})
    return _cookie_response(request, resp, token)


@app.post('/chess/{room_code}/join')
async def chess_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if r.get('white_token') == token or r.get('black_token') == token:
        return {'success': True}
    if r.get('black_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    await execute_query(
        'UPDATE chess_rooms SET black_token=?,black_name=?,black_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@app.get('/api/chess/{room_code}/state')
async def chess_state(request: Request, room_code: str):
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
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
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
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
        await apply_game_result('chess', white_key, r.get('white_name') or '名無しさん',
                           black_key, r.get('black_name') or '名無しさん', result_white)

    new_board_json = json.dumps(board)
    new_ep_json = json.dumps(new_en_passant) if new_en_passant else None
    now = datetime.utcnow().isoformat()
    await execute_query(
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
    member_id = await _game_member_id(request)
    code = await _new_room_code()
    now = datetime.utcnow().isoformat()
    await execute_query(
        '''INSERT INTO shogi_rooms
           (room_code, sente_token, sente_name, sente_member_id, gote_token, gote_name, board, hands, turn, status, winner, in_check, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _initial_shogi_board(), _initial_shogi_hands_json(), 's', 'waiting', None, None, now, now]
    )
    resp = RedirectResponse(url=f'/shogi/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@app.get('/shogi/{room_code}')
async def shogi_room(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return RedirectResponse(url='/shogi')
    r = rows[0]
    token = _game_token(request)
    my = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)

    # 招待リンクを踏んだ2人目をその場で自動参加させる
    if my is None and not r.get('gote_token') and r.get('sente_token') != token:
        name = await _game_name(request)
        await execute_query(
            'UPDATE shogi_rooms SET gote_token=?,gote_name=?,gote_member_id=?,status=?,updated_at=? '
            'WHERE room_code=? AND gote_token IS NULL',
            [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
        )
        rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
        r = rows[0]
        my = 'g' if r.get('gote_token') == token else my

    resp = templates.TemplateResponse(request, 'syogi.html', {'room': r, 'my_color': my})
    return _cookie_response(request, resp, token)


@app.post('/shogi/{room_code}/join')
async def shogi_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if r.get('sente_token') == token or r.get('gote_token') == token:
        return {'success': True}
    if r.get('gote_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    await execute_query(
        'UPDATE shogi_rooms SET gote_token=?,gote_name=?,gote_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@app.get('/api/shogi/{room_code}/state')
async def shogi_state(request: Request, room_code: str):
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
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
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
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
        await apply_game_result('shogi', sente_key, r.get('sente_name') or '名無しさん',
                           gote_key, r.get('gote_name') or '名無しさん', result_sente)

    now = datetime.utcnow().isoformat()
    await execute_query(
        'UPDATE shogi_rooms SET board=?,hands=?,turn=?,updated_at=?,in_check=?,status=?,winner=? WHERE room_code=? AND turn=?',
        [json.dumps(board), json.dumps(hands), next_color, now, (next_color if next_in_check else None), new_status, winner, code, color]
    )
    return {'success': True}


@app.post('/shogi/{room_code}/drop')
async def shogi_drop(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
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
        await apply_game_result('shogi', sente_key, r.get('sente_name') or '名無しさん',
                           gote_key, r.get('gote_name') or '名無しさん', result_sente)

    now = datetime.utcnow().isoformat()
    await execute_query(
        'UPDATE shogi_rooms SET board=?,hands=?,turn=?,updated_at=?,in_check=?,status=?,winner=? WHERE room_code=? AND turn=?',
        [json.dumps(new_board), json.dumps(hands), next_color, now, (next_color if next_in_check else None), new_status, winner, code, color]
    )
    return {'success': True}


MAX_THREAD_TAGS = 5
MAX_TAG_LENGTH = 15


def _sanitize_tags(raw: str):
    """カンマ区切りのタグ入力を、重複除去・NGワードフィルタ・エスケープした上で
    最大5個・1個あたり15文字までに制限したリストにする。"""
    tags = []
    for t in (raw or '').split(','):
        t = t.strip()
        if not t:
            continue
        t = html.escape(filter_ng_words(t))[:MAX_TAG_LENGTH]
        if t and t not in tags:
            tags.append(t)
        if len(tags) >= MAX_THREAD_TAGS:
            break
    return tags


def _parse_tags(raw_json):
    """DBに保存されたJSON文字列のタグを、壊れていても落ちないようにリストへ戻す。"""
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


async def _fetch_threads_with_stats(where_sql, where_params, order_sql, limit=None, offset=None):
    """threads を、レス数・最終更新日時・現在の閲覧人数・スレ主の表示名/アイコンつきで取得する共通ヘルパー"""
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
                WHERE au.location = ('thread_' || t.id) AND au.last_seen >= ?) AS thread_active_count,
            u.username AS op_username,
            u.icon_path AS op_icon_path
        FROM threads t
        LEFT JOIN users u ON u.public_id = t.user_id
        {where_sql}
        ORDER BY {order_sql}
    """
    params = [active_cutoff] + list(where_params)

    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset or 0]

    return await execute_query(sql, params)


@app.api_route('/', methods=['GET', 'HEAD'])
async def index(request: Request):
    client_ip = get_client_ip(request)
    if await is_banned_request(request, client_ip):
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

    tag = request.query_params.get('tag', '').strip()[:MAX_TAG_LENGTH]

    sort = request.query_params.get('sort', DEFAULT_THREAD_SORT).strip()
    if sort not in THREAD_SORT_SQL:
        sort = DEFAULT_THREAD_SORT
    order_sql = THREAD_SORT_SQL[sort]

    where_clauses = []
    where_params = []
    if search_query:
        where_clauses.append("t.title LIKE ?")
        where_params.append(f"%{search_query}%")
    if category:
        where_clauses.append("t.category = ?")
        where_params.append(category)
    if tag:
        # tagsはJSON配列文字列で保存しているので、部分一致で引っかける簡易的な絞り込み
        where_clauses.append("t.tags LIKE ?")
        where_params.append(f'%"{tag}"%')
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    # スレ一覧本体と、スレの中身に依存しない他の問い合わせ(管理者の一言・
    # 閲覧人数・ログイン中会員情報・未読DM件数)は互いに無関係なので、
    # 直列にawaitせず並列に投げてD1往復の回数分の待ち時間を潰す。
    # (トップページは最もアクセスされるページなので、ここの直列化が
    #  サイト全体の体感速度に一番効いていた)
    threads_result, admin_result, active_count_result, current_member_result, unread_dm_result = await asyncio.gather(
        _fetch_threads_with_stats(where_sql, where_params, order_sql, limit=per_page, offset=start_index),
        execute_query("SELECT message FROM admin_messages WHERE id = ?", [1]),
        update_and_get_user_counts(user_token, "lobby"),
        get_current_member(request),
        get_unread_dm_count(request),
        return_exceptions=True,
    )

    if isinstance(admin_result, Exception) or not admin_result:
        admin_message = "ここに管理者の一言が表示されます。" if not isinstance(admin_result, Exception) else "管理者の一言の取得に失敗しました。"
    else:
        admin_message = admin_result[0]['message']

    active_count = 0 if isinstance(active_count_result, Exception) else active_count_result
    current_member = None if isinstance(current_member_result, Exception) else current_member_result
    unread_dm_count = 0 if isinstance(unread_dm_result, Exception) else unread_dm_result

    if isinstance(threads_result, Exception):
        print(f"スレッド一覧取得エラー: {threads_result}")
        threads = []
        has_next = False
    else:
        try:
            threads = threads_result
            has_next = len(threads) == per_page

            pinned_ids = [4, 3, 2, 1]
            pinned_threads = []

            # 固定表示は「検索・カテゴリ絞り込み・並び替えなし」かつ1ページ目の時だけ行う
            show_pinned = (not search_query) and (not category) and (not tag) and sort == DEFAULT_THREAD_SORT and page == 1

            if show_pinned:
                for pid in pinned_ids:
                    for i, t in enumerate(threads):
                        if int(t['id']) == pid:
                            pinned_threads.append(threads.pop(i))
                            break

                # 固定スレは最大4件なので、1件ずつSELECTせずIN句で1往復にまとめる。
                # (旧実装は最大4回の追加ラウンドトリップを発生させていた)
                missing_ids = [
                    pid for pid in pinned_ids
                    if not any(int(pt['id']) == pid for pt in pinned_threads)
                ]
                if missing_ids:
                    try:
                        placeholders = ",".join("?" for _ in missing_ids)
                        pinned_res = await _fetch_threads_with_stats(
                            f"WHERE t.id IN ({placeholders})", missing_ids, order_sql
                        )
                        by_id = {int(r['id']): r for r in pinned_res}
                        for pid in missing_ids:
                            if pid in by_id:
                                pinned_threads.append(by_id[pid])
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
                t['tags_list'] = _parse_tags(t.get('tags'))
        except Exception as e:
            print(f"スレッド一覧取得エラー: {e}")
            threads = []
            has_next = False

    is_admin_user = can_manage_board(request)

    response = templates.TemplateResponse(request, 'index.html', {
        'threads': threads,
        'admin_message': admin_message,
        'is_admin_user': is_admin_user,
        'current_member': current_member,
        'unread_dm_count': unread_dm_count,
        'active_count': active_count,
        'current_page': page,
        'has_next': has_next,
        'search_query': search_query,
        'thread_categories': THREAD_CATEGORIES,
        'thread_category_labels': THREAD_CATEGORY_LABELS,
        'thread_category_colors': THREAD_CATEGORY_COLORS,
        'current_category': category,
        'current_tag': tag,
        'thread_sort_options': THREAD_SORT_OPTIONS,
        'current_sort': sort,
        'current_year': datetime.utcnow().year,
        'category_meta_json': json.dumps(
            {key: {'label': label, 'color': color} for key, label, color in THREAD_CATEGORIES},
            ensure_ascii=False
        ),
        'current_member_json': json.dumps(current_member, ensure_ascii=False) if current_member else 'null',
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
            await execute_query("UPDATE admin_messages SET message = ? WHERE id = ?", [message, 1])
        except Exception as e:
            print(f"メッセージ更新エラー: {e}")
    return RedirectResponse(url='/', status_code=303)


@app.post('/create_thread')
async def create_thread(request: Request):
    client_ip = get_client_ip(request)
    if await is_banned_request(request, client_ip):
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

    tags = _sanitize_tags(form.get('tags', ''))
    tags_json = json.dumps(tags, ensure_ascii=False)

    is_admin = can_manage_board(request)
    now = time.time()

    thread_cooldown = 300
    if not is_admin and await is_proxy_or_vpn(client_ip):
        thread_cooldown = 900

    if not is_admin:
        if client_ip in LAST_THREAD_TIMES and now - LAST_THREAD_TIMES[client_ip] < thread_cooldown:
            remaining_time = int(thread_cooldown - (now - LAST_THREAD_TIMES[client_ip]))
            minutes = remaining_time // 60
            seconds = remaining_time % 60
            return json_resp({"error": f"スレッド作成は5分に1回までです。(proxy,VPNは15分）あと {minutes}分 {seconds}秒 お待ちください。"}, 429)

    LAST_THREAD_TIMES[client_ip] = now

    try:
        member_public_id = await get_member_public_id(request)
        await execute_query(
            "INSERT INTO threads (title, ip_address, category, user_id, tags) VALUES (?, ?, ?, ?, ?)",
            [title, client_ip, category, member_public_id, tags_json]
        )
        res = await execute_query("SELECT * FROM threads ORDER BY id DESC LIMIT 1")
        new_thread = res[0] if res else None
        if new_thread and not new_thread.get('category'):
            new_thread['category'] = category
        if new_thread:
            new_thread['tags_list'] = tags
    except Exception as e:
        print(f"スレッド作成エラー: {e}")
        return json_resp({"error": "データベースエラーが発生しました"}, 500)

    return {"success": True, "thread": new_thread}


# --- 「もっと見る」用: 過去のレスを追加読み込みするAPI ---
@app.get('/thread/{thread_id}/get_older_replies')
async def get_older_replies(request: Request, thread_id: int):
    client_ip = get_client_ip(request)
    if await is_banned_request(request, client_ip):
        return json_resp({"success": False, "error": "Banned"}, 403)

    before_id_raw = request.query_params.get('before_id')
    try:
        before_id = int(before_id_raw) if before_id_raw is not None else None
    except (TypeError, ValueError):
        before_id = None
    if not before_id:
        return json_resp({"success": False, "error": "before_idが必要です", "replies": [], "has_more": False}, 400)

    try:
        LOAD_LIMIT = 300
        # 件数カウント・過去レス本体・スレ情報(スレ主判定用)は互いに依存しないので並列に投げる
        count_res, older_res, thread_res = await asyncio.gather(
            execute_query("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ? AND id < ?", [thread_id, before_id]),
            execute_query(
                """SELECT r.*, u.icon_path AS icon_path
                   FROM replies r
                   LEFT JOIN users u ON u.public_id = r.poster_public_id
                   WHERE r.thread_id = ? AND r.id < ?
                   ORDER BY r.id DESC LIMIT ?""",
                [thread_id, before_id, LOAD_LIMIT]
            ),
            execute_query("SELECT ip_address, user_id FROM threads WHERE id = ?", [thread_id]),
        )
        count_before = count_res[0]['cnt'] if count_res else 0
        older_replies = list(reversed(older_res)) if older_res else []
        start_num = count_before - len(older_replies) + 1
        op_user_id = resolve_op_user_id(thread_res[0]) if thread_res else None

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

        return {"success": True, "replies": formatted_replies, "has_more": count_before > len(older_replies)}
    except Exception as e:
        print(f"過去レス取得エラー: {e}")
        return json_resp({"success": False, "error": "データベースエラー", "replies": [], "has_more": False}, 500)


# --- リアルタイム自動更新用API ---
@app.get('/thread/{thread_id}/get_new_replies')
async def get_new_replies(request: Request, thread_id: int):
    client_ip = get_client_ip(request)
    if await is_banned_request(request, client_ip):
        return json_resp({"success": False, "error": "Banned"}, 403)

    try:
        after_id = int(request.query_params.get('after_id', 0))
    except (TypeError, ValueError):
        after_id = 0
    try:
        replies = await execute_query(
            """SELECT r.*, u.icon_path AS icon_path
               FROM replies r
               LEFT JOIN users u ON u.public_id = r.poster_public_id
               WHERE r.thread_id = ? AND r.id > ?
               ORDER BY r.id ASC""",
            [thread_id, after_id]
        )
        if not replies:
            replies = []

        thread_res = await execute_query("SELECT ip_address, user_id FROM threads WHERE id = ?", [thread_id])
        op_user_id = resolve_op_user_id(thread_res[0]) if thread_res else None

        total_count_res = await execute_query("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id])
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

        return {"success": True, "replies": formatted_replies}
    except Exception as e:
        print(f"新着レス取得エラー: {e}")
        return json_resp({"success": False, "error": "データベースエラー", "replies": []}, 500)


@app.api_route('/thread/{thread_id}', methods=['GET', 'POST'])
async def thread_view(request: Request, thread_id: int):
    client_ip = get_client_ip(request)
    if await is_banned_request(request, client_ip):
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
            member_public_id = await get_member_public_id(request)
            user_id = member_public_id if member_public_id else get_daily_user_id(client_ip)

        poster_public_id = await get_member_public_id(request)

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
                orig_ext = os.path.splitext(orig_filename)[1]
                raw_bytes = await upload.read()
                # CPUバウンドな画像処理はイベントループを塞がないようスレッドプールへ逃がす
                processed_bytes, new_ext = await run_in_threadpool(process_reply_image, raw_bytes, orig_ext)
                unique_filename = f"{uuid.uuid4()}{new_ext}"
                content_type = 'image/webp' if new_ext == '.webp' else (upload.content_type or 'application/octet-stream')
                await run_in_threadpool(
                    s3_client.upload_fileobj, io.BytesIO(processed_bytes), R2_BUCKET_NAME, unique_filename,
                    ExtraArgs={'ContentType': content_type}
                )
                image_url = f"{R2_PUBLIC_URL.rstrip('/')}/{unique_filename}"
            except ValueError:
                print("投稿画像処理エラー: 不正な画像ファイルのためアップロードをスキップしました")
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
                await execute_query(
                    """INSERT INTO replies (thread_id, author, content, user_id, is_admin, role, image_url, ip_address, poster_public_id) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [thread_id, author_input, content, user_id, 1 if is_admin else 0, role_to_save, image_url, client_ip, poster_public_id]
                )
                LAST_REPLY_SIGNATURES[reply_signature] = signature_now
                res = await execute_query("SELECT * FROM replies WHERE thread_id = ? ORDER BY id DESC LIMIT 1", [thread_id])
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

                    # スレ主判定用のスレ情報・通し番号用の総数・投稿者アイコンは互いに
                    # 依存しないD1問い合わせ(+セッションキャッシュ)なので並列に投げる。
                    # return_exceptions=Trueで、どれか1つが失敗しても他の結果は活かす
                    # (旧実装の「クエリごとにtry/exceptでフォールバック」と同じ耐障害性を保つ)。
                    new_reply['is_op'] = False
                    new_reply['post_num'] = None
                    new_reply['icon_path'] = None

                    gather_tasks = [
                        execute_query("SELECT ip_address, user_id FROM threads WHERE id = ?", [thread_id]),
                        execute_query("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id]),
                    ]
                    if poster_public_id:
                        gather_tasks.append(get_member_icon_path(request))

                    gather_results = await asyncio.gather(*gather_tasks, return_exceptions=True)

                    thread_res = gather_results[0]
                    if not isinstance(thread_res, Exception) and thread_res:
                        op_user_id = resolve_op_user_id(thread_res[0])
                        new_reply['is_op'] = bool(op_user_id) and new_reply.get('user_id') == op_user_id

                    total_count_res = gather_results[1]
                    if not isinstance(total_count_res, Exception) and total_count_res:
                        new_reply['post_num'] = total_count_res[0]['cnt']

                    # アバターURL: 投稿者が会員ならセッションキャッシュから取得(DB再問い合わせ不要)。
                    # ゲスト/STAFF投稿の場合はNoneのままでフロント側がフォールバック表示する。
                    if poster_public_id and len(gather_results) > 2 and not isinstance(gather_results[2], Exception):
                        new_reply['icon_path'] = gather_results[2]

                    await manager.broadcast(thread_id, new_reply)
                    return {"success": True, "reply": new_reply}
            except Exception as e:
                print(f"レス保存エラー: {e}")
                return json_resp({"success": False, "error": "データベースエラーが発生しました。"}, 500)
        return json_resp({"success": False, "error": "書き込み内容が空です。"}, 400)

    try:
        # スレッド本体・合計レス数・直近レス一覧はお互いに依存しないので、
        # 逐次awaitではなくasyncio.gatherでD1へ並列に投げる。
        # (D1クエリ1本ごとにCloudflare APIへの往復が発生するため、直列だと
        # 待ち時間が単純に足し算になっていた)
        # D1のAPI応答サイズ制限対策として、直近分だけ取得する(古い順に並べ直す)
        # 初回表示はテンプレート描画・転送量・体感速度への影響が大きいため、
        # 必要最小限だけ取得し、残りは「もっと見る」ボタンの追加取得に任せる。
        RECENT_REPLIES_LIMIT = 300
        thread_res, count_res, replies_res = await asyncio.gather(
            execute_query("SELECT * FROM threads WHERE id = ?", [thread_id]),
            execute_query("SELECT COUNT(*) as cnt FROM replies WHERE thread_id = ?", [thread_id]),
            execute_query(
                """SELECT r.*, u.icon_path AS icon_path
                   FROM replies r
                   LEFT JOIN users u ON u.public_id = r.poster_public_id
                   WHERE r.thread_id = ?
                   ORDER BY r.id DESC LIMIT ?""",
                [thread_id, RECENT_REPLIES_LIMIT]
            ),
        )
        if not thread_res:
            return text_resp("スレッドが見つかりません", 404)
        thread = thread_res[0]
        thread['tags_list'] = _parse_tags(thread.get('tags'))

        total_reply_count = count_res[0]['cnt'] if count_res else 0
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
        for r in thread['replies']:
            r['is_op'] = bool(op_user_id) and r.get('user_id') == op_user_id
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
    # 閲覧人数更新・ログイン中会員情報・未読DM件数も互いに依存しないので並列化する
    active_count, current_member, unread_dm_count = await asyncio.gather(
        update_and_get_user_counts(user_token, location_key),
        get_current_member(request),
        get_unread_dm_count(request),
    )

    response = templates.TemplateResponse(request, 'thread.html', {
        'thread': thread,
        'is_admin_user': is_admin_user,
        'active_count': active_count,
        'back_to_board': "/?tab=threads",
        'op_user_id': op_user_id,
        'current_member': current_member,
        'report_reasons': REPORT_REASONS,
        'unread_dm_count': unread_dm_count,
        'current_member_json': json.dumps(current_member, ensure_ascii=False) if current_member else 'null',
    })

    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60 * 60 * 24 * 365, httponly=True)

    return response


@app.post('/thread/{thread_id}/delete_thread')
async def delete_thread(request: Request, thread_id: int):
    if not can_manage_board(request):
        return text_resp("権限がありません", 403)
    try:
        await execute_query("DELETE FROM threads WHERE id = ?", [thread_id])
    except Exception as e:
        print(f"スレッド削除エラー: {e}")
    return RedirectResponse(url='/', status_code=303)


@app.post('/thread/{thread_id}/delete/{reply_id}')
async def delete_reply(request: Request, thread_id: int, reply_id: int):
    if not can_manage_board(request):
        return text_resp("権限がありません", 403)
    try:
        await execute_query(
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
        reply_res = await execute_query("SELECT ip_address, poster_public_id FROM replies WHERE id = ?", [reply_id])
        if reply_res:
            b_ip = reply_res[0].get('ip_address')
            b_public_id = reply_res[0].get('poster_public_id')
            if b_ip:
                await execute_query("INSERT OR IGNORE INTO banned_ips (ip_address) VALUES (?)", [b_ip])
            if b_public_id:
                # ログイン中の会員による投稿の場合は、IPだけでなくアカウント自体もBANする。
                # IPアドレスが変わっても(スマホの回線切り替え等)このアカウントでの投稿はブロックされる。
                await execute_query(
                    "INSERT OR IGNORE INTO banned_members (public_id, reason, banned_at) VALUES (?, ?, ?)",
                    [b_public_id, f'reply_id={reply_id}', datetime.utcnow().isoformat()]
                )
            await execute_query(
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
        thread_res = await execute_query("SELECT ip_address, user_id FROM threads WHERE id = ?", [thread_id])
        if thread_res:
            owner_ip = thread_res[0].get('ip_address')
            owner_public_id = thread_res[0].get('user_id')
            if owner_ip:
                await execute_query("INSERT OR IGNORE INTO banned_ips (ip_address) VALUES (?)", [owner_ip])
            if owner_public_id and owner_public_id != 'STAFF':
                # スレッド作成には必ずログインが必要なため、user_idは常に会員のpublic_id。
                await execute_query(
                    "INSERT OR IGNORE INTO banned_members (public_id, reason, banned_at) VALUES (?, ?, ?)",
                    [owner_public_id, f'thread_id={thread_id}', datetime.utcnow().isoformat()]
                )
            await execute_query("UPDATE threads SET title = ? WHERE id = ?", ['【このスレッドは管理員によってBANされました】', thread_id])
            await execute_query("DELETE FROM replies WHERE thread_id = ?", [thread_id])
            await execute_query(
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
        # D1が遅い=504の予兆。この数値が増え続けたらDB側を疑う。
        "d1_slow_query_total": _d1_slow_query_total,
        "timestamp": time.time()
    }


# =====================================================================
# フォロー / DM / ブロック / 通報
# =====================================================================
# DMは「相互フォローのみ」送信可能。判定は必ずサーバー側で行い、
# フロントのボタン表示だけに頼らない。

DM_MAX_LENGTH = 1000
DM_HISTORY_LIMIT = 200
DM_COOLDOWN_SECONDS = 1
LAST_DM_TIMES: dict[int, float] = {}


_CACHE_CLEANUP_INTERVAL_SECONDS = 120  # 2分おきに掃除する(512MBでは1時間分の蓄積でも危険なため)
_STALE_ENTRY_MAX_AGE_SECONDS = 3600    # どのクールダウンよりも十分長い猶予（1時間）を持たせて破棄する


def _cap_dict_size(d: dict, max_entries: int):
    """辞書が想定外に肥大化しないよう、エントリ数の上限を強制する。
    上限を超えた分は「最も古い(=挿入順で先頭の)」ものから落とす。
    クールダウン判定は数秒〜数十分で無意味になるため、多少落ちても実害はない。
    512MB環境ではこの上限が最後の安全弁になる。"""
    if len(d) <= max_entries:
        return
    overflow = len(d) - max_entries
    for k in list(d.keys())[:overflow]:
        d.pop(k, None)


def _prune_expired(d: dict, now: float, ttl: float, get_timestamp=None):
    """dのうち、タイムスタンプがttl秒より古いエントリを削除する。
    get_timestampを指定すると、値(dict等)からタイムスタンプを取り出す関数として使う。"""
    if get_timestamp is None:
        stale_keys = [k for k, v in d.items() if now - v > ttl]
    else:
        stale_keys = [k for k, v in d.items() if now - get_timestamp(v) > ttl]
    for k in stale_keys:
        d.pop(k, None)


async def _cleanup_memory_caches_loop():
    """LAST_THREAD_TIMES / LAST_REPLY_TIMES / LAST_REPLY_SIGNATURES / LAST_DM_TIMES /
    _PROXY_CHECK_CACHE は、投稿やアクセスのたびにエントリが増える一方で、
    これまで削除される仕組みが無かった。クールダウン判定に使う情報は本来
    数秒〜数時間で不要になるにもかかわらず、ユニークIPや投稿数に比例して
    プロセスのメモリを際限なく消費し続け、長時間稼働させると
    メモリ上限超過でプロセスが落ちる（＝サーバーが頻繁に落ちる）主要因になっていた。
    このタスクは定期的に古いエントリを削除し、辞書のサイズを有界に保つ。"""
    while True:
        await asyncio.sleep(_CACHE_CLEANUP_INTERVAL_SECONDS)
        try:
            now = time.time()
            _prune_expired(LAST_THREAD_TIMES, now, _STALE_ENTRY_MAX_AGE_SECONDS)
            _prune_expired(LAST_REPLY_TIMES, now, _STALE_ENTRY_MAX_AGE_SECONDS)
            _prune_expired(LAST_REPLY_SIGNATURES, now, _STALE_ENTRY_MAX_AGE_SECONDS)
            _prune_expired(LAST_DM_TIMES, now, _STALE_ENTRY_MAX_AGE_SECONDS)
            # 経過時間による掃除に加え、件数上限でも必ず有界にする
            for _d in (LAST_THREAD_TIMES, LAST_REPLY_TIMES, LAST_REPLY_SIGNATURES,
                       LAST_DM_TIMES, _PROXY_CHECK_CACHE, _BANNED_IP_CACHE):
                _cap_dict_size(_d, 20000)
            _prune_expired(
                _PROXY_CHECK_CACHE, now, _PROXY_CACHE_TTL,
                get_timestamp=lambda v: v["checked_at"]
            )
            _prune_expired(_BANNED_IP_CACHE, now, 300)
        except Exception as e:
            print(f"メモリキャッシュ掃除エラー: {e}")

REPORT_REASONS = {
    'spam': 'スパム・宣伝',
    'harassment': '嫌がらせ・誹謗中傷',
    'sexual': '性的・わいせつな内容',
    'violence': '暴力的・危険な内容',
    'personal_info': '個人情報の晒し',
    'other': 'その他',
}


async def _user_id_by_public_id(public_id: str):
    """public_id から users.id を引く。存在しなければ None。"""
    if not public_id:
        return None
    res = await execute_query("SELECT id FROM users WHERE public_id = ?", [public_id])
    return res[0]['id'] if res else None


async def _require_member(request: Request):
    """ログイン必須APIの共通チェック。(member_id, エラーレスポンス) を返す。"""
    if not is_member_logged_in(request):
        return None, json_resp({"success": False, "error": "ログインが必要です。"}, 401)
    if await is_banned_request(request, get_client_ip(request)):
        return None, json_resp({"success": False, "error": "この操作は許可されていません。"}, 403)
    return request.session.get('member_id'), None


async def is_following(follower_id: int, followee_id: int) -> bool:
    res = await execute_query(
        "SELECT 1 AS ok FROM follows WHERE follower_id = ? AND followee_id = ? LIMIT 1",
        [follower_id, followee_id]
    )
    return bool(res)


async def is_mutual_follow(user_a: int, user_b: int) -> bool:
    """相互フォローかどうかを1クエリで判定する。"""
    res = await execute_query(
        "SELECT COUNT(*) AS cnt FROM follows "
        "WHERE (follower_id = ? AND followee_id = ?) OR (follower_id = ? AND followee_id = ?)",
        [user_a, user_b, user_b, user_a]
    )
    return bool(res) and res[0]['cnt'] >= 2


async def is_blocked_between(user_a: int, user_b: int) -> bool:
    """どちらか一方でもブロックしていればTrue（双方向に遮断する）。"""
    res = await execute_query(
        "SELECT COUNT(*) AS cnt FROM blocks "
        "WHERE (blocker_id = ? AND blocked_id = ?) OR (blocker_id = ? AND blocked_id = ?)",
        [user_a, user_b, user_b, user_a]
    )
    return bool(res) and res[0]['cnt'] > 0


async def can_dm(sender_id: int, target_id: int) -> tuple[bool, str]:
    """DM送信可否を判定する。(可否, 不可の理由) を返す。"""
    if sender_id == target_id:
        return False, "自分自身にDMを送ることはできません。"
    if await is_blocked_between(sender_id, target_id):
        return False, "この相手とはやり取りできません。"
    if not await is_mutual_follow(sender_id, target_id):
        return False, "DMは相互フォローの相手にのみ送信できます。"
    return True, ""


async def get_follow_counts(user_id: int) -> dict:
    res = await execute_query(
        "SELECT "
        " (SELECT COUNT(*) FROM follows WHERE followee_id = ?) AS followers, "
        " (SELECT COUNT(*) FROM follows WHERE follower_id = ?) AS following",
        [user_id, user_id]
    )
    if not res:
        return {'followers': 0, 'following': 0}
    return {'followers': res[0]['followers'], 'following': res[0]['following']}


async def get_unread_dm_count(request: Request) -> int:
    """未読DM件数。未ログイン時は0。ヘッダーのバッジ表示に使う。"""
    if not is_member_logged_in(request):
        return 0
    member_id = request.session.get('member_id')
    try:
        res = await execute_query(
            "SELECT COUNT(*) AS cnt FROM dm_messages m "
            "JOIN dm_conversations c ON c.id = m.conversation_id "
            "WHERE (c.user_a_id = ? OR c.user_b_id = ?) "
            "  AND m.sender_id != ? AND m.read_at IS NULL",
            [member_id, member_id, member_id]
        )
        return res[0]['cnt'] if res else 0
    except Exception as e:
        print(f"未読DM件数の取得エラー: {e}")
        return 0


async def _get_or_create_conversation(user_a: int, user_b: int) -> int:
    """2人分の会話IDを返す。無ければ作る。user_a_id < user_b_id で正規化する。"""
    lo, hi = (user_a, user_b) if user_a < user_b else (user_b, user_a)
    res = await execute_query(
        "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", [lo, hi]
    )
    if res:
        return res[0]['id']
    await execute_query(
        "INSERT INTO dm_conversations (user_a_id, user_b_id) VALUES (?, ?)", [lo, hi]
    )
    res = await execute_query(
        "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", [lo, hi]
    )
    return res[0]['id'] if res else None


# ---------------------------------------------------------------------
# DM用 WebSocket（ユーザー単位で接続。どのページにいても着信を受け取れる）
# ---------------------------------------------------------------------
class DMConnectionManager:
    def __init__(self):
        self.connections: dict[int, list[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.connections.setdefault(user_id, []).append(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        conns = self.connections.get(user_id)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                del self.connections[user_id]

    async def send_to_user_local(self, user_id: int, payload: dict):
        """同一プロセス内でこのuser_idに接続しているWebSocketにのみ配信する。"""
        conns = list(self.connections.get(user_id, []))
        dead = []
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)

    async def send_to_user(self, user_id: int, payload: dict):
        """全worker配下の接続に配信する。ConnectionManager.broadcast()と同じ方針。"""
        if redis_client is None:
            await self.send_to_user_local(user_id, payload)
            return
        try:
            await redis_client.publish(REALTIME_CHANNEL, json.dumps({
                "kind": "dm_message", "user_id": user_id, "payload": payload
            }))
        except Exception as e:
            print(f"Redis publish エラー(DM送信): {e}")
            await self.send_to_user_local(user_id, payload)


dm_manager = DMConnectionManager()


@app.websocket('/ws/dm')
async def dm_ws(websocket: WebSocket):
    # SessionMiddlewareはWebSocketにも適用されるため、セッションから本人確認できる。
    member_id = websocket.session.get('member_id')
    if not member_id:
        await websocket.close(code=1008)
        return
    await dm_manager.connect(member_id, websocket)
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == 'ping':
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        dm_manager.disconnect(member_id, websocket)
    except Exception:
        dm_manager.disconnect(member_id, websocket)


# ---------------------------------------------------------------------
# フォロー
# ---------------------------------------------------------------------
@app.post('/api/follow/{public_id}')
async def api_follow_toggle(request: Request, public_id: str):
    member_id, err = await _require_member(request)
    if err:
        return err

    target_id = await _user_id_by_public_id(public_id)
    if not target_id:
        return json_resp({"success": False, "error": "ユーザーが見つかりません。"}, 404)
    if target_id == member_id:
        return json_resp({"success": False, "error": "自分自身はフォローできません。"}, 400)
    if await is_blocked_between(member_id, target_id):
        return json_resp({"success": False, "error": "この相手はフォローできません。"}, 403)

    try:
        if await is_following(member_id, target_id):
            await execute_query(
                "DELETE FROM follows WHERE follower_id = ? AND followee_id = ?",
                [member_id, target_id]
            )
            following = False
        else:
            await execute_query(
                "INSERT INTO follows (follower_id, followee_id) VALUES (?, ?)",
                [member_id, target_id]
            )
            following = True
    except Exception as e:
        print(f"フォロー処理エラー: {e}")
        return json_resp({"success": False, "error": "処理に失敗しました。"}, 500)

    counts = await get_follow_counts(target_id)
    return json_resp({
        "success": True,
        "following": following,
        "mutual": await is_mutual_follow(member_id, target_id),
        "followers_count": counts['followers'],
    })


async def _render_follow_list(request: Request, public_id: str, mode: str):
    """mode: 'followers' or 'following'"""
    target = await execute_query(
        "SELECT id, username, public_id FROM users WHERE public_id = ?", [public_id]
    )
    if not target:
        return text_resp("そのユーザーは見つかりませんでした。", 404)
    target = target[0]

    if mode == 'followers':
        sql = (
            "SELECT u.username, u.public_id, u.icon_path, u.bio "
            "FROM follows f JOIN users u ON u.id = f.follower_id "
            "WHERE f.followee_id = ? ORDER BY f.id DESC LIMIT 200"
        )
    else:
        sql = (
            "SELECT u.username, u.public_id, u.icon_path, u.bio "
            "FROM follows f JOIN users u ON u.id = f.followee_id "
            "WHERE f.follower_id = ? ORDER BY f.id DESC LIMIT 200"
        )
    users = await execute_query(sql, [target['id']]) or []

    return templates.TemplateResponse(request, 'follow_list.html', {
        'profile_user': target,
        'mode': mode,
        'users': users,
        'counts': await get_follow_counts(target['id']),
        'unread_dm_count': await get_unread_dm_count(request),
    })


@app.get('/profile/{public_id}/followers')
async def followers_page(request: Request, public_id: str):
    return await _render_follow_list(request, public_id, 'followers')


@app.get('/profile/{public_id}/following')
async def following_page(request: Request, public_id: str):
    return await _render_follow_list(request, public_id, 'following')


# ---------------------------------------------------------------------
# ブロック
# ---------------------------------------------------------------------
@app.post('/api/block/{public_id}')
async def api_block_toggle(request: Request, public_id: str):
    member_id, err = await _require_member(request)
    if err:
        return err

    target_id = await _user_id_by_public_id(public_id)
    if not target_id:
        return json_resp({"success": False, "error": "ユーザーが見つかりません。"}, 404)
    if target_id == member_id:
        return json_resp({"success": False, "error": "自分自身はブロックできません。"}, 400)

    try:
        existing = await execute_query(
            "SELECT 1 AS ok FROM blocks WHERE blocker_id = ? AND blocked_id = ? LIMIT 1",
            [member_id, target_id]
        )
        if existing:
            await execute_query(
                "DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
                [member_id, target_id]
            )
            blocked = False
        else:
            await execute_query(
                "INSERT INTO blocks (blocker_id, blocked_id) VALUES (?, ?)",
                [member_id, target_id]
            )
            # ブロックしたら双方のフォロー関係も解除する（相互フォロー＝DM可のため）
            await execute_query(
                "DELETE FROM follows WHERE (follower_id = ? AND followee_id = ?) "
                "OR (follower_id = ? AND followee_id = ?)",
                [member_id, target_id, target_id, member_id]
            )
            blocked = True
    except Exception as e:
        print(f"ブロック処理エラー: {e}")
        return json_resp({"success": False, "error": "処理に失敗しました。"}, 500)

    return json_resp({"success": True, "blocked": blocked})


# ---------------------------------------------------------------------
# DM
# ---------------------------------------------------------------------
@app.get('/messages')
async def dm_list(request: Request):
    if not is_member_logged_in(request):
        return RedirectResponse(url='/login')
    member_id = request.session.get('member_id')

    conversations, mutuals, unread_dm_count = await asyncio.gather(
        execute_query(
            "SELECT c.id AS conversation_id, "
            "       u.username, u.public_id, u.icon_path, "
            "       c.last_message_at, "
            "       (SELECT m.content FROM dm_messages m WHERE m.conversation_id = c.id "
            "          ORDER BY m.id DESC LIMIT 1) AS last_content, "
            "       (SELECT COUNT(*) FROM dm_messages m2 WHERE m2.conversation_id = c.id "
            "          AND m2.sender_id != ? AND m2.read_at IS NULL) AS unread_count "
            "FROM dm_conversations c "
            "JOIN users u ON u.id = CASE WHEN c.user_a_id = ? THEN c.user_b_id ELSE c.user_a_id END "
            "WHERE (c.user_a_id = ? OR c.user_b_id = ?) AND c.last_message_at IS NOT NULL "
            "ORDER BY c.last_message_at DESC LIMIT 100",
            [member_id, member_id, member_id, member_id]
        ),
        # 相互フォロー（＝DMを新規に送れる相手）の一覧
        execute_query(
            "SELECT u.username, u.public_id, u.icon_path FROM follows f1 "
            "JOIN follows f2 ON f2.follower_id = f1.followee_id AND f2.followee_id = f1.follower_id "
            "JOIN users u ON u.id = f1.followee_id "
            "WHERE f1.follower_id = ? ORDER BY u.username LIMIT 200",
            [member_id]
        ),
        get_unread_dm_count(request),
    )

    return templates.TemplateResponse(request, 'messages.html', {
        'conversations': conversations or [],
        'mutuals': mutuals or [],
        'unread_dm_count': unread_dm_count,
    })


@app.get('/messages/{public_id}')
async def dm_conversation(request: Request, public_id: str):
    if not is_member_logged_in(request):
        return RedirectResponse(url='/login')
    member_id = request.session.get('member_id')

    partner_res, unread_dm_count = await asyncio.gather(
        execute_query("SELECT id, username, public_id, icon_path FROM users WHERE public_id = ?", [public_id]),
        get_unread_dm_count(request),
    )
    if not partner_res:
        return text_resp("そのユーザーは見つかりませんでした。", 404)
    partner = partner_res[0]

    messages = []
    (allowed, reason), conv_res = await asyncio.gather(
        can_dm(member_id, partner['id']),
        execute_query(
            "SELECT id FROM dm_conversations WHERE (user_a_id = ? AND user_b_id = ?) "
            "OR (user_a_id = ? AND user_b_id = ?)",
            [member_id, partner['id'], partner['id'], member_id]
        ),
    )
    conversation_id = conv_res[0]['id'] if conv_res else None

    if conversation_id:
        raw = await execute_query(
            "SELECT id, sender_id, content, created_at, read_at FROM dm_messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            [conversation_id, DM_HISTORY_LIMIT]
        ) or []
        messages = list(reversed(raw))
        for m in messages:
            m['is_mine'] = (m['sender_id'] == member_id)
            m['created_at'] = _to_jst_string(m.get('created_at'))
            # contentは送信時点で既にhtml.escape済みのため、URLだけをリンク化する
            # (WebSocket/ポーリング経由の表示と同じ挙動に揃える)
            if m.get('content'):
                m['content'] = re.sub(
                    r'(https?://[^\s<>]+)',
                    r'<a href="\1" target="_blank" rel="noopener noreferrer nofollow">\1</a>',
                    str(m['content'])
                )

        # 開いた時点で相手からの未読を既読にする。
        # 表示するmessagesは既に組み立て済みなので、この既読UPDATEの完了を
        # 待つ必要はなく、バックグラウンドで実行してレスポンスを速く返す。
        asyncio.create_task(execute_query(
            "UPDATE dm_messages SET read_at = datetime('now') "
            "WHERE conversation_id = ? AND sender_id != ? AND read_at IS NULL",
            [conversation_id, member_id]
        ))

    return templates.TemplateResponse(request, 'dm_conversation.html', {
        'partner': partner,
        'messages': messages,
        'can_send': allowed,
        'deny_reason': reason,
        'report_reasons': REPORT_REASONS,
        'unread_dm_count': unread_dm_count,
    })


def _to_jst_string(value):
    """DBのUTC文字列を日本時間の表示用文字列に変換する。失敗時は元の値を返す。"""
    if not value:
        return ''
    try:
        dt_utc = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (dt_utc + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return str(value)


@app.post('/api/dm/{public_id}/send')
async def api_dm_send(request: Request, public_id: str):
    member_id, err = await _require_member(request)
    if err:
        return err

    body = await get_json_silent(request)
    content = (body.get('content') or '').strip() if body else ''
    if not content:
        return json_resp({"success": False, "error": "本文が空です。"}, 400)
    if len(content) > DM_MAX_LENGTH:
        return json_resp({"success": False, "error": f"本文は{DM_MAX_LENGTH}文字以内にしてください。"}, 400)

    target_id = await _user_id_by_public_id(public_id)
    if not target_id:
        return json_resp({"success": False, "error": "ユーザーが見つかりません。"}, 404)

    allowed, reason = await can_dm(member_id, target_id)
    if not allowed:
        return json_resp({"success": False, "error": reason}, 403)

    now = time.time()
    last = LAST_DM_TIMES.get(member_id)
    if last is not None and now - last < DM_COOLDOWN_SECONDS:
        return json_resp({"success": False, "error": "送信が速すぎます。少し待ってください。"}, 429)
    LAST_DM_TIMES[member_id] = now

    # 本文はここでエスケープして保存し、表示側では自動エスケープを切って
    # リンク化だけ行う（掲示板本体のレスと同じ方針）。
    safe_content = html.escape(content)

    try:
        conversation_id = await _get_or_create_conversation(member_id, target_id)
        if not conversation_id:
            return json_resp({"success": False, "error": "会話の作成に失敗しました。"}, 500)

        await execute_query(
            "INSERT INTO dm_messages (conversation_id, sender_id, content) VALUES (?, ?, ?)",
            [conversation_id, member_id, safe_content]
        )
        await execute_query(
            "UPDATE dm_conversations SET last_message_at = datetime('now') WHERE id = ?",
            [conversation_id]
        )
        res = await execute_query(
            "SELECT id, sender_id, content, created_at FROM dm_messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            [conversation_id]
        )
        new_message = res[0] if res else None
    except Exception as e:
        print(f"DM送信エラー: {e}")
        return json_resp({"success": False, "error": "送信に失敗しました。"}, 500)

    if new_message:
        new_message['created_at'] = _to_jst_string(new_message.get('created_at'))

        sender_public_id = await get_member_public_id(request)
        # 受信側へプッシュ（相手がどのページを開いていてもバッジを更新できる）
        await dm_manager.send_to_user(target_id, {
            "type": "dm_new",
            "conversation_id": conversation_id,
            "from_public_id": sender_public_id,
            "from_username": request.session.get('member_username'),
            "message": {**new_message, "is_mine": False},
        })
        # 送信者の他タブにも反映
        await dm_manager.send_to_user(member_id, {
            "type": "dm_sent",
            "conversation_id": conversation_id,
            "to_public_id": public_id,
            "message": {**new_message, "is_mine": True},
        })

    return json_resp({"success": True, "message": {**new_message, "is_mine": True}})


@app.get('/api/dm/{public_id}/poll')
async def api_dm_poll(request: Request, public_id: str):
    """
    WebSocketでのリアルタイム配信が届かない環境(複数インスタンス構成や、
    WebSocketがブロックされるホスティング環境など)でも新着メッセージを
    取り逃さないための保険。指定したメッセージID以降の新着だけを返す。
    フロント側は数秒おきにこれを叩き、WebSocketの受信と重複しないよう
    メッセージIDで重複排除する。
    """
    member_id, err = await _require_member(request)
    if err:
        return err

    after_id_raw = request.query_params.get('after_id')
    try:
        after_id = int(after_id_raw) if after_id_raw is not None else 0
    except (TypeError, ValueError):
        after_id = 0

    target_id = await _user_id_by_public_id(public_id)
    if not target_id:
        return json_resp({"success": False, "error": "ユーザーが見つかりません。"}, 404)

    conv_res = await execute_query(
        "SELECT id FROM dm_conversations WHERE (user_a_id = ? AND user_b_id = ?) "
        "OR (user_a_id = ? AND user_b_id = ?)",
        [member_id, target_id, target_id, member_id]
    )
    if not conv_res:
        return {"success": True, "messages": []}
    conversation_id = conv_res[0]['id']

    try:
        rows = await execute_query(
            "SELECT id, sender_id, content, created_at FROM dm_messages "
            "WHERE conversation_id = ? AND id > ? ORDER BY id ASC LIMIT 50",
            [conversation_id, after_id]
        ) or []

        if rows:
            # ポーリングで取得した = 開いて見ているとみなし、相手からの分は既読にする
            await execute_query(
                "UPDATE dm_messages SET read_at = datetime('now') "
                "WHERE conversation_id = ? AND sender_id != ? AND read_at IS NULL",
                [conversation_id, member_id]
            )

        messages = []
        for r in rows:
            content = str(r.get('content') or '')
            content = re.sub(
                r'(https?://[^\s<>]+)',
                r'<a href="\1" target="_blank" rel="noopener noreferrer nofollow">\1</a>',
                content
            )
            messages.append({
                "id": r['id'],
                "content": content,
                "created_at": _to_jst_string(r.get('created_at')),
                "is_mine": r['sender_id'] == member_id,
            })
        return {"success": True, "messages": messages}
    except Exception as e:
        print(f"DMポーリングエラー: {e}")
        return json_resp({"success": False, "error": "取得に失敗しました。"}, 500)


@app.get('/api/dm/unread_count')
async def api_dm_unread_count(request: Request):
    return json_resp({"count": await get_unread_dm_count(request)})


# ---------------------------------------------------------------------
# 通報
# ---------------------------------------------------------------------
@app.post('/api/report')
async def api_report(request: Request):
    member_id, err = await _require_member(request)
    if err:
        return err

    body = await get_json_silent(request) or {}
    target_type = (body.get('target_type') or '').strip()
    target_id = str(body.get('target_id') or '').strip()
    reason = (body.get('reason') or '').strip()
    detail = (body.get('detail') or '').strip()[:500]

    if target_type not in ('user', 'thread', 'reply', 'dm'):
        return json_resp({"success": False, "error": "通報対象が不正です。"}, 400)
    if not target_id:
        return json_resp({"success": False, "error": "通報対象が指定されていません。"}, 400)
    if reason not in REPORT_REASONS:
        return json_resp({"success": False, "error": "通報理由が不正です。"}, 400)

    try:
        # 同一対象への重複通報を防ぐ（未対応のものが既にあれば受け付けたことにする）
        dup = await execute_query(
            "SELECT id FROM reports WHERE reporter_id = ? AND target_type = ? "
            "AND target_id = ? AND status = 'open' LIMIT 1",
            [member_id, target_type, target_id]
        )
        if dup:
            return json_resp({"success": True, "already": True})

        await execute_query(
            "INSERT INTO reports (reporter_id, target_type, target_id, reason, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            [member_id, target_type, target_id, reason, html.escape(detail)]
        )
    except Exception as e:
        print(f"通報登録エラー: {e}")
        return json_resp({"success": False, "error": "通報の送信に失敗しました。"}, 500)

    return json_resp({"success": True})


@app.get('/admin')
async def admin_dashboard(request: Request):
    if not can_manage_board(request):
        return text_resp("権限がありません。", 403)

    status = request.query_params.get('status', 'open')
    if status not in ('open', 'resolved', 'rejected'):
        status = 'open'

    reports = await execute_query(
        "SELECT r.*, u.username AS reporter_name, u.public_id AS reporter_public_id "
        "FROM reports r LEFT JOIN users u ON u.id = r.reporter_id "
        "WHERE r.status = ? ORDER BY r.created_at DESC LIMIT 200",
        [status]
    ) or []
    for r in reports:
        r['reason_label'] = REPORT_REASONS.get(r.get('reason'), r.get('reason'))
        r['created_at'] = _to_jst_string(r.get('created_at'))

        # レス通報は target_id が replies.id なので、管理画面から該当スレッドの該当レスへ
        # ジャンプできるよう、所属スレッドIDとスレッド内の通し番号(#post-N用)を解決しておく。
        if r.get('target_type') == 'reply':
            r['reply_thread_id'] = None
            r['reply_post_num'] = None
            r['reply_content'] = None
            r['reply_author'] = None
            r['reply_poster_public_id'] = None
            try:
                reply_id = int(r.get('target_id'))
            except (TypeError, ValueError):
                reply_id = None
            if reply_id:
                reply_res = await execute_query(
                    "SELECT thread_id, content, author, poster_public_id FROM replies WHERE id = ?",
                    [reply_id]
                )
                if reply_res:
                    reply_row = reply_res[0]
                    r_thread_id = reply_row['thread_id']
                    pos_res = await execute_query(
                        "SELECT COUNT(*) AS cnt FROM replies WHERE thread_id = ? AND id <= ?",
                        [r_thread_id, reply_id]
                    )
                    r['reply_thread_id'] = r_thread_id
                    r['reply_post_num'] = pos_res[0]['cnt'] if pos_res else None
                    r['reply_content'] = reply_row.get('content')
                    r['reply_author'] = reply_row.get('author')
                    r['reply_poster_public_id'] = reply_row.get('poster_public_id')

    open_count_res = await execute_query("SELECT COUNT(*) AS cnt FROM reports WHERE status = 'open'")

    try:
        trend = await execute_query(
            "SELECT substr(created_at, 1, 10) AS day, "
            "COUNT(*) AS pv, COUNT(DISTINCT visitor_token) AS uniques "
            "FROM page_views WHERE created_at >= datetime('now', '-14 days') "
            "GROUP BY day ORDER BY day ASC"
        ) or []

        summary_res = await execute_query(
            "SELECT COUNT(*) AS pv, COUNT(DISTINCT visitor_token) AS uniques "
            "FROM page_views WHERE created_at >= datetime('now', '-7 days')"
        )
        summary = summary_res[0] if summary_res else {'pv': 0, 'uniques': 0}

        popular_threads = await execute_query(
            "SELECT pv.thread_id AS thread_id, COUNT(*) AS views, t.title AS title "
            "FROM page_views pv JOIN threads t ON t.id = pv.thread_id "
            "WHERE pv.thread_id IS NOT NULL AND pv.created_at >= datetime('now', '-7 days') "
            "GROUP BY pv.thread_id ORDER BY views DESC LIMIT 10"
        ) or []

        referrers = await execute_query(
            "SELECT CASE WHEN referrer_host IS NULL OR referrer_host = '' THEN '(direct / 直接アクセス)' "
            "ELSE referrer_host END AS host, COUNT(*) AS cnt "
            "FROM page_views WHERE created_at >= datetime('now', '-7 days') "
            "GROUP BY host ORDER BY cnt DESC LIMIT 10"
        ) or []

        devices = await execute_query(
            "SELECT device, COUNT(*) AS cnt FROM page_views "
            "WHERE created_at >= datetime('now', '-7 days') GROUP BY device ORDER BY cnt DESC"
        ) or []

        browsers = await execute_query(
            "SELECT browser, COUNT(*) AS cnt FROM page_views "
            "WHERE created_at >= datetime('now', '-7 days') GROUP BY browser ORDER BY cnt DESC"
        ) or []
    except Exception as e:
        print(f"アクセス解析集計エラー: {e}")
        trend, summary, popular_threads, referrers, devices, browsers = [], {'pv': 0, 'uniques': 0}, [], [], [], []

    def _with_pct(rows, key='cnt'):
        total = sum(r.get(key, 0) or 0 for r in rows) or 1
        for r in rows:
            r['pct'] = round((r.get(key, 0) or 0) * 100 / total, 1)
        return rows

    return templates.TemplateResponse(request, 'admin.html', {
        'reports': reports,
        'status': status,
        'open_count': open_count_res[0]['cnt'] if open_count_res else 0,
        'trend': trend,
        'summary': summary,
        'popular_threads': popular_threads,
        'referrers': _with_pct(referrers),
        'devices': _with_pct(devices),
        'browsers': _with_pct(browsers),
    })


# 旧URL（ブックマーク・過去のリンク対策として残しておく）
@app.get('/admin/reports')
async def admin_reports(request: Request):
    qs = request.url.query
    return RedirectResponse(url=('/admin' + (f'?{qs}' if qs else '')), status_code=301)


@app.get('/admin/analytics')
async def admin_analytics(request: Request):
    return RedirectResponse(url='/admin', status_code=301)


@app.post('/api/admin/thread/{thread_id}/delete')
async def api_admin_delete_thread(request: Request, thread_id: int):
    """管理画面(通報一覧)から直接スレッドを削除するためのAPI。既存の
    /thread/{id}/delete_thread と異なりリダイレクトせずJSONを返す。"""
    if not can_manage_board(request):
        return json_resp({"success": False, "error": "権限がありません。"}, 403)
    try:
        await execute_query("DELETE FROM threads WHERE id = ?", [thread_id])
        await execute_query("DELETE FROM replies WHERE thread_id = ?", [thread_id])
    except Exception as e:
        print(f"スレッド削除エラー(admin): {e}")
        return json_resp({"success": False, "error": "削除に失敗しました。"}, 500)
    return json_resp({"success": True})


@app.post('/api/admin/reply/{reply_id}/delete')
async def api_admin_delete_reply(request: Request, reply_id: int):
    """管理画面(通報一覧)から直接レスを削除(あぼーん化)するためのAPI。
    既存の /thread/{tid}/delete/{rid} と異なりthread_idを必要とせず、
    リダイレクトせずJSONを返す。"""
    if not can_manage_board(request):
        return json_resp({"success": False, "error": "権限がありません。"}, 403)
    try:
        await execute_query(
            """UPDATE replies SET author = ?, content = ?, user_id = ?, is_admin = ?, image_url = ? 
               WHERE id = ?""",
            ['あぼーん', 'この書き込みは管理員によって削除されました。', '???', 0, '', reply_id]
        )
    except Exception as e:
        print(f"レス削除エラー(admin): {e}")
        return json_resp({"success": False, "error": "削除に失敗しました。"}, 500)
    return json_resp({"success": True})


@app.post('/api/admin/reports/{report_id}/status')
async def api_admin_report_status(request: Request, report_id: int):
    if not can_manage_board(request):
        return json_resp({"success": False, "error": "権限がありません。"}, 403)

    body = await get_json_silent(request) or {}
    new_status = (body.get('status') or '').strip()
    if new_status not in ('open', 'resolved', 'rejected'):
        return json_resp({"success": False, "error": "不正なステータスです。"}, 400)

    try:
        await execute_query(
            "UPDATE reports SET status = ?, handled_by = ?, handled_at = datetime('now') WHERE id = ?",
            [new_status, request.session.get('member_id'), report_id]
        )
    except Exception as e:
        print(f"通報ステータス更新エラー: {e}")
        return json_resp({"success": False, "error": "更新に失敗しました。"}, 500)

    return json_resp({"success": True, "status": new_status})


# =========================
# リバースプロキシ(Cloudflare)配下でのスキーム認識
# =========================
# gunicorn + UvicornWorker構成では、uvicorn.run()に渡すproxy_headers/forwarded_allow_ips
# (下のif __name__=='__main__'ブロック内)は効かない。そのままだとアプリからは全リクエストが
# 「http」に見えてしまい、request.url_for()等が生成する絶対URLがhttp://になる。
# HTTPSページからそのURLへfetch()すると、ブラウザにMixed Contentとしてブロックされる
# (このバグで将棋の部屋作成が壊れていた)。
# ProxyHeadersMiddlewareでX-Forwarded-Proto等を信頼させることで、gunicorn経由でも
# アプリが「https」を正しく認識できるようにする。
# trusted_hosts='127.0.0.1'としているのは、gunicornが127.0.0.1:8000にbindしており、
# 手前のCloudflare/プロキシからの接続はローカルから来るため。
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    # 512MB環境では worker は1つに固定する(増やすと1プロセスあたりの常駐メモリで即OOM)。
    # limit_concurrency により、過負荷時は「無応答で504」ではなく
    # 「即座に503」を返して早く失敗させる(ユーザー体験・復旧速度の両方が改善する)。
    uvicorn.run(
        app,
        host='0.0.0.0',
        port=port,
        workers=1,
        limit_concurrency=64,
        timeout_keep_alive=65,   # Cloudflare のキープアライブを再利用する
        backlog=128,
        proxy_headers=True,
        forwarded_allow_ips='127.0.0.1',
        timeout_graceful_shutdown=10,
    )
