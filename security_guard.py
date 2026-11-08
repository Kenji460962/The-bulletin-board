"""DoS(過負荷)対策と「画像砲」(巨大画像・画像の連投でサーバーを落とす攻撃)対策。

app.py から次のように使う(app.py 側の変更は同梱の app.py に反映済み):

    import security_guard
    security_guard.set_abuse_handler(_guard_auto_ban)                  # 悪質な送信を既存のBAN機能につなぐ
    app.add_middleware(security_guard.GuardMiddleware)                 # 全リクエストの入口
    img = security_guard.open_image_safely(raw_bytes, draft_to=(1600, 1600))   # Image.open の代わり
    await security_guard.run_image_job(process_reply_image, raw, ext)   # 画像処理の同時実行数を制限
    wait = security_guard.image_rate_check(ip, token)                   # 画像投稿の頻度制限

■ 既存のBAN機能との関係
  ・動く順番: GuardMiddleware(この中)→ enforce_ban_and_issue_ids(BAN判定・cookie発行)→ ルート。
    BANの判定・cookieの発行には一切触れない。BANテーブル(banned_*)も読み書きしない(自動BANだけは下記のとおり既存関数を呼ぶ)。
  ・IPの取り方は既存の get_client_ip(CF-Connecting-IP)・ban_ip_key(IPv6は/64)と同じ。
  ・BAN済みの相手がcookieを作り直して来た場合も、この入口の制限は(BANとは無関係に)かかる。
  ・自動BAN: 「明らかに悪意のある送信」(解凍爆弾クラスの画像、桁違いに巨大な本文)を24時間に3回行った端末だけを、
    既存の _bind_banned_identity でBANする。BANするのは cookie 2本(user_bbs_token / bbs_dev)だけで、IP・端末特徴は対象にしない
    (共有回線の他人を巻き込まないため。既存の apply_strong_ban の「その人だけを対象にする」方針と同じ)。
    頻度制限に引っかかっただけではBANしない(一時ブロックの段階的な延長までで止める)。

■ 制限の単位(IPを「1人」として数えない)
  学校・会社・モバイル回線では、1つのIPを何百人も共有する。IPで厳しく制限すると、無関係な人まで巻き添えで止まる。
  そこで制限の主役を「端末ごと」にし、IPは「桁違いに多いときだけ働く、ゆるい上限」にした。

    ① 端末ごと … cookie 2本(user_bbs_token と bbs_dev)の両方を別々に数える。片方を作り直しても、残る1本で積み上がる。
       違反したときに止まるのはその端末だけ。繰り返すほど長くなる(5分→30分→6時間→24時間)。
    ② cookieなし / 偽造cookie … IP(IPv6は/64)単位の「匿名枠」でまとめて数える。止まるのは匿名枠だけ(普通の利用者は影響を受けない)。
    ③ IP全体 … ①の約10倍のゆるい上限。大量のcookieを偽造してくる攻撃を、最後にここで受け止める。
    ④ cookieの使い捨て対策 … 同じIPから「初めて見るcookie」が1分に一定数を超えたら、新しいcookieは匿名枠として扱う。
    ⑤ 同時処理数の公平化 … 端末ごと12・IPごと32・匿名枠12まで。uvicornの同時処理数(64)を、1人で使い切れないようにする。
    ⑥ 過負荷時の選別 … 同時処理が48を超えたら、cookieなし・初めて見る端末を先に断り(503)、いつもの利用者を優先して通す。

守っているもの(アプリ層でできる範囲):
  1. 巨大なリクエストを、読み込む前・読み込み途中で打ち切る(413)。URLが長すぎるもの(414)・変わったメソッド(405)も弾く
  2. 上の①〜⑥の頻度制限・同時処理制限と、繰り返す相手の段階的なブロック(429/503)
  3. 解凍爆弾(小さいファイルが展開すると数億画素になる画像)・フレーム数の多いGIFを、デコード前に弾く
  4. 画像処理の同時実行数を制限(512MB環境で同時に何枚もデコードしてOOMになるのを防ぐ)
  5. 画像の連投を、端末ごと・IP全体(ゆるめ)・サイト全体で制限
  6. 悪質な送信を繰り返した端末の自動BAN(既存のBAN機能に連携)

守れないもの(Cloudflare / nginx 側の役割): 大量の接続を張る攻撃(Slowloris)、回線を埋め尽くす攻撃(L3/L4のDDoS)。

信頼するIP(ヘルスチェック・自社サーバー等)は、環境変数 GUARD_TRUSTED_IPS にカンマ区切りで書くと、この入口の制限を受けない。
"""
import asyncio
import contextvars
import io
import json
import os
import time
import warnings
from collections import deque

from PIL import Image
from starlette.concurrency import run_in_threadpool

# =========================================================
# 設定値
# =========================================================
# --- リクエスト ---
MAX_BODY_BYTES = 12 * 1024 * 1024         # multipart(画像付き投稿)の本文上限
MAX_JSON_BODY_BYTES = 1 * 1024 * 1024     # それ以外(JSON・フォーム)の本文上限
MAX_INTERNAL_BODY_BYTES = 64 * 1024 * 1024  # /internal/ (移行などの管理用)は大きいデータを送るため別枠
# 端末ごと(cookie)。個人の利用ならまず届かない値
ID_GET_LIMIT, ID_GET_WINDOW = 150, 10.0           # 閲覧: 10秒に150回(1秒15回。ゲームのポーリングや画像読み込みの余裕込み)
ID_POST_LIMIT, ID_POST_WINDOW = 120, 60.0         # 書き込み系: 1分に120回
# 匿名枠(cookieなし/偽造cookie)。IP(IPv6は/64)単位。初回アクセスは誰でもcookieなしなので、共有IPでも困らない余裕を持たせる
ANON_GET_LIMIT, ANON_GET_WINDOW = 60, 10.0
ANON_POST_LIMIT, ANON_POST_WINDOW = 20, 60.0
# IP全体(ゆるい上限)。共有IPの全員分を合わせても届かない値
IP_GET_LIMIT, IP_GET_WINDOW = 1500, 10.0
IP_POST_LIMIT, IP_POST_WINDOW = 600, 60.0
NEW_TOKENS_PER_IP_PER_MIN = 500                   # 同じIPから1分に「初めて見るcookie」が500個を超えたら、新しいcookieは匿名枠扱い
                                                  # (再起動直後は全員が「初めて」に見えるので、学校など共有IPが巻き込まれない余裕を持たせる。
                                                  #  使い捨てcookieの総量はIP全体の上限でも抑えられる)
VIOLATIONS_TO_BLOCK, VIOLATION_WINDOW = 10, 60.0  # 60秒に10回制限に引っかかったら…
ANON_BLOCK_SECONDS = 60                           # 匿名枠(cookieなし)は1分だけ。初めて来た人が巻き込まれる時間を短くする
IP_BLOCK_SECONDS = 120                            # IP全体の上限を超えたときは2分ブロック
TOKEN_RE_MIN, TOKEN_RE_MAX = 16, 64               # cookieとして受け付ける長さ(これ以外は偽造扱い)
BLOCK_STEPS = (300, 1800, 21600, 86400)           # 端末ごとのブロックは、繰り返すたびに 5分→30分→6時間→24時間 と延びる
STRIKE_MEMORY = 86400.0                           # 「繰り返し」を数える期間(24時間)
HEAVY_WEIGHT = 3                                  # 悪質な送信(巨大本文・解凍爆弾)は、通常の違反3回分として数える(3回目の記録でBAN依頼が先に走る)
INFLIGHT_PER_ID, INFLIGHT_PER_ANON, INFLIGHT_PER_IP = 12, 12, 32   # 同時に処理中にできるリクエスト数(uvicornの上限64を独占させない)
LOAD_SHED_INFLIGHT = 48                           # 同時処理がこれを超えたら、cookieなし・初めて見る端末を先に断る
MAX_URL_BYTES = 8192                              # パス+クエリの上限
ALLOWED_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}
AUTO_BAN_ENABLED = True                           # 悪質な送信を繰り返した端末を既存のBANに連携するか
AUTO_BAN_THRESHOLD = 3                            # 24時間にこの回数の「悪質な送信」で自動BAN
TRUSTED_IPS = {x.strip() for x in os.environ.get('GUARD_TRUSTED_IPS', '').split(',') if x.strip()}
TRACK_MAX_KEYS = 50000                    # 追跡するキー数の上限(メモリ保護)

# --- 画像 ---
MAX_UPLOAD_BYTES = 10 * 1024 * 1024       # 画像1枚の上限
MAX_ANIMATION_BYTES = 5 * 1024 * 1024     # アニメ画像(そのまま保存される)の上限
MAX_JPEG_PIXELS = 100_000_000             # JPEGは縮小デコード(draft)ができるので大きめでも安全
MAX_OTHER_PIXELS = 25_000_000             # PNG/WebP/GIF(5000x5000相当)。これ以上はデコードしない
MAX_ANIMATION_PIXELS = 2_000_000          # アニメ画像の1フレームあたり
MAX_ANIMATION_FRAMES = 120
ALLOWED_FORMATS = {'JPEG', 'PNG', 'GIF', 'WEBP'}
IMAGE_CONCURRENCY = 2                     # 同時にデコードできる枚数
IMAGE_MAX_WAITING = 6                     # 待ち行列がこれを超えたら「混み合っています」で断る
IMG_PER_MIN_TOKEN, IMG_PER_HOUR_TOKEN = 4, 30   # 端末(cookie)ごとの画像投稿: 1分4枚・1時間30枚(主役)
IMG_PER_MIN_IP, IMG_PER_HOUR_IP = 30, 200        # IP全体(ゆるい上限。共有IPの全員分を想定): 1分30枚・1時間200枚
IMG_PER_MIN_ANON, IMG_PER_HOUR_ANON = 2, 10      # cookieなし/偽造cookieの匿名枠(IP単位): 1分2枚・1時間10枚
IMG_PER_MIN_GLOBAL = 120                         # サイト全体(分散して撃ってくる攻撃対策)

# PillowのDecompressionBombWarning(上限超え)を警告ではなく例外にする。上限そのものも保険として設定。
Image.MAX_IMAGE_PIXELS = MAX_JPEG_PIXELS
warnings.filterwarnings('error', category=Image.DecompressionBombWarning)


# =========================================================
# 1) 画像
# =========================================================
def open_image_safely(raw_bytes: bytes, draft_to=None):
    """Image.open の安全版。デコードする前にヘッダーだけを見て、危険な画像は ValueError で弾く。
    draft_to=(w, h) を渡すと、JPEGはそのサイズ付近まで縮小しながらデコードする(メモリ・時間を大幅に節約)。"""
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError('too_large')
    try:
        img = Image.open(io.BytesIO(raw_bytes))        # ヘッダーのみ。ここではまだデコードしない
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        _flag_image(True)                              # Pillow自身が解凍爆弾と判定(1億画素超)= 悪質として記録
        raise ValueError('too_many_pixels')
    except Exception:
        raise ValueError('invalid_image')

    if img.format not in ALLOWED_FORMATS:
        raise ValueError('bad_format')
    w, h = img.size
    if w <= 0 or h <= 0:
        raise ValueError('invalid_image')
    limit = MAX_JPEG_PIXELS if img.format == 'JPEG' else MAX_OTHER_PIXELS
    if w * h > limit:
        _flag_image(w * h > limit * 4)       # 上限の4倍超(=解凍爆弾クラス)は悪質として記録
        raise ValueError('too_many_pixels')

    try:
        animated = bool(getattr(img, 'is_animated', False))
    except Exception:
        raise ValueError('invalid_image')
    if animated:
        if w * h > MAX_ANIMATION_PIXELS or len(raw_bytes) > MAX_ANIMATION_BYTES:
            _flag_image(w * h > MAX_ANIMATION_PIXELS * 4)
            raise ValueError('animation_too_large')
        n = 1                                          # フレーム数は上限までしか数えない
        try:
            while True:
                img.seek(n)
                n += 1
                if n > MAX_ANIMATION_FRAMES:
                    _flag_image(False)
                    raise ValueError('too_many_frames')
        except EOFError:
            pass
        except ValueError:
            raise
        except Exception:
            raise ValueError('invalid_image')
        img.seek(0)
    elif draft_to and img.format == 'JPEG':
        img.draft('RGB', draft_to)                     # 縮小デコードの指定(load前に呼ぶ必要がある)
    return img


def _flag_image(malicious: bool):
    flag_abuse('image_bomb' if malicious else 'image_oversize')


class ImageBusy(Exception):
    """画像処理が混み合っていて、これ以上受け付けられない。"""


_img_sem = asyncio.Semaphore(IMAGE_CONCURRENCY)
_img_waiting = 0


async def run_image_job(func, *args):
    """画像処理(CPU・メモリを使う)を、同時実行数を制限して実行する。行列が長すぎるときは ImageBusy。"""
    global _img_waiting
    if _img_waiting >= IMAGE_MAX_WAITING:
        raise ImageBusy()
    _img_waiting += 1
    try:
        async with _img_sem:
            return await run_in_threadpool(func, *args)
    finally:
        _img_waiting -= 1


# =========================================================
# 識別(IP・端末)
# =========================================================
import ipaddress
import re

_TOKEN_OK = re.compile(r'^[A-Za-z0-9_\-]+$')


def ip_key(ip: str) -> str:
    """IPv6は /64 単位にまとめる(1人が /64 内の無数のアドレスを使い分けても、同一扱いにするため)。"""
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            if addr.ipv4_mapped:
                return str(addr.ipv4_mapped)
            return str(ipaddress.ip_network(f'{ip}/64', strict=False).network_address) + '/64'
        return str(addr)
    except ValueError:
        return ip[:64]


def valid_token(token) -> bool:
    return bool(token) and TOKEN_RE_MIN <= len(token) <= TOKEN_RE_MAX and bool(_TOKEN_OK.match(token))


def _cookie(scope, name: bytes):
    for k, v in scope.get('headers', []):
        if k == b'cookie':
            for part in v.decode('latin-1').split(';'):
                n, _, val = part.strip().partition('=')
                if n.encode('latin-1') == name:
                    return val
    return None


# =========================================================
# 悪用の記録(リクエストごとの記録帳。画像処理やルートから flag_abuse で書き込み、ミドルウェアが集計する)
# =========================================================
class _Ctx:
    __slots__ = ('flags',)

    def __init__(self):
        self.flags = []


_ctx = contextvars.ContextVar('security_guard_ctx', default=None)
_abuse_handler = None


def flag_abuse(kind: str):
    """kind: 'image_rate'(画像連投) / 'image_oversize'(上限超えの画像) / 'image_bomb'(解凍爆弾クラス=悪質)"""
    c = _ctx.get()
    if c is not None:
        c.flags.append(kind)


def set_abuse_handler(fn):
    """悪質な送信を繰り返した端末の自動BANに使う非同期関数 fn(info: dict) を登録する。
    info = {'token', 'dev', 'ip', 'reason'}。app.py から既存のBAN機能(_bind_banned_identity)につなぐ。"""
    global _abuse_handler
    _abuse_handler = fn


# ---- 画像投稿の頻度制限(スライディングウィンドウ) ----
_img_hits = {}                # key -> deque[timestamp]
_img_global = deque()


def _slide(dq: deque, now: float, window: float):
    while dq and now - dq[0] > window:
        dq.popleft()


def image_rate_check(ip, token=None) -> int:
    """画像を投稿してよいなら0を返して1回分を消費する。多すぎるときは「あと何秒待てばよいか」を返す。
    ・cookieを持つ端末 : 端末ごとの制限(主役) + IP全体のゆるい上限
    ・cookieなし/偽造   : IP(IPv6は/64)単位の厳しい匿名枠  → 共有IPの他の端末は巻き込まない"""
    now = time.monotonic()
    ipk = ip_key(ip) if ip else None
    limits = []                                   # (key, limit, window)
    if valid_token(token):
        limits += [(('tk-m', token), IMG_PER_MIN_TOKEN, 60.0), (('tk-h', token), IMG_PER_HOUR_TOKEN, 3600.0)]
        if ipk:
            limits += [(('ip-m', ipk), IMG_PER_MIN_IP, 60.0), (('ip-h', ipk), IMG_PER_HOUR_IP, 3600.0)]
    elif ipk:
        limits += [(('an-m', ipk), IMG_PER_MIN_ANON, 60.0), (('an-h', ipk), IMG_PER_HOUR_ANON, 3600.0)]

    waits = []
    for key, limit, window in limits:
        dq = _img_hits.setdefault(key, deque())
        _slide(dq, now, window)
        if len(dq) >= limit:
            waits.append(int(window - (now - dq[0])) + 1)
    _slide(_img_global, now, 60.0)
    if len(_img_global) >= IMG_PER_MIN_GLOBAL:
        waits.append(int(60.0 - (now - _img_global[0])) + 1)
    if waits:
        flag_abuse('image_rate')
        return max(waits)
    for key, _l, _w in limits:
        _img_hits[key].append(now)
    _img_global.append(now)
    if len(_img_hits) > TRACK_MAX_KEYS:                # メモリ保護: 古いものを掃除
        for k in [k for k, dq in _img_hits.items() if not dq or now - dq[-1] > 3600]:
            _img_hits.pop(k, None)
    return 0


# =========================================================
# 2) リクエストの入口(ASGIミドルウェア)
# =========================================================
class _BodyTooLarge(Exception):
    pass


def _client_ip(scope) -> str:
    for k, v in scope.get('headers', []):
        if k == b'cf-connecting-ip':
            return v.decode('latin-1').strip()[:64]
    c = scope.get('client')
    return c[0] if c else 'unknown'


class GuardMiddleware:
    """全HTTPリクエストの最初に動く。DBにも本文にも触れる前に、巨大・大量のリクエストを断る。"""

    def __init__(self, app):
        self.app = app
        self._hits = {}            # (種別, 値, 'g'|'p') -> deque[timestamp]
        self._violations = {}      # (種別, 値) -> deque[timestamp]
        self._blocked_until = {}   # (種別, 値) -> monotonic時刻
        self._strikes = {}         # (種別, 値) -> deque[ブロックされた時刻](段階的に延ばすため)
        self._token_seen = {}      # cookie値 -> 最後に見た時刻(「初めて見るcookie」の判定用)
        self._new_tokens = {}      # ip_key -> deque[「初めて見たcookie」の時刻]
        self._malicious = {}       # cookie値 -> deque[悪質な送信の時刻]
        self._ban_sent = set()     # 自動BANを依頼済みのcookie値
        self._inflight = {}        # (種別, 値) -> 処理中のリクエスト数
        self._inflight_total = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':            # WebSocket等はそのまま通す
            return await self.app(scope, receive, send)
        method, path = scope['method'], scope['path']
        if method not in ALLOWED_METHODS:
            return await self._reject(send, 405, '許可されていないメソッドです。', None)
        if len(scope.get('raw_path') or path.encode()) + len(scope.get('query_string', b'')) > MAX_URL_BYTES:
            return await self._reject(send, 414, 'URLが長すぎます。', None)
        if method in ('HEAD', 'OPTIONS') or path.startswith('/static/'):
            return await self.app(scope, receive, send)

        ip = _client_ip(scope)
        if ip in TRUSTED_IPS:
            return await self.app(scope, receive, send)
        now = time.monotonic()
        ipk = ip_key(ip)
        kind, idents, token, dev, known = self._identify(scope, ipk, now)
        keys = idents + [('ip', ipk)]

        # ---- ブロック中なら即429。止まるのは「その端末(cookieのどちらか)」か「匿名枠」。同じIPのほかの人には影響しない ----
        for k in keys:
            until = self._blocked_until.get(k)
            if until:
                if until > now:
                    return await self._reject(send, 429, 'アクセスが集中しています。しばらく待ってからお試しください。', int(until - now) + 1)
                self._blocked_until.pop(k, None)

        # ---- 頻度制限(端末ごと / 匿名枠 / IP全体) ----
        is_get = method == 'GET'
        sfx = 'g' if is_get else 'p'
        for k in keys:
            if k[0] == 'ip':
                limit, window = (IP_GET_LIMIT, IP_GET_WINDOW) if is_get else (IP_POST_LIMIT, IP_POST_WINDOW)
            elif k[0] == 'anon':
                limit, window = (ANON_GET_LIMIT, ANON_GET_WINDOW) if is_get else (ANON_POST_LIMIT, ANON_POST_WINDOW)
            else:
                limit, window = (ID_GET_LIMIT, ID_GET_WINDOW) if is_get else (ID_POST_LIMIT, ID_POST_WINDOW)
            dq = self._hits.setdefault((k[0], k[1], sfx), deque())
            _slide(dq, now, window)
            if len(dq) >= limit:
                self._violate(idents if k[0] != 'ip' else [k], now)
                return await self._reject(send, 429, 'リクエストが多すぎます。少し待ってからお試しください。', int(window - (now - dq[0])) + 1)
            dq.append(now)
        if len(self._hits) > TRACK_MAX_KEYS:
            self._prune(now)

        # ---- 同時処理数: 過負荷時の選別と、1人(1IP)が枠を独占するのを防ぐ ----
        if self._inflight_total >= LOAD_SHED_INFLIGHT and (kind == 'anon' or not known):
            return await self._reject(send, 503, 'ただいま混み合っています。少し待ってからもう一度お試しください。', 3)
        for k in keys:
            cap = INFLIGHT_PER_IP if k[0] == 'ip' else (INFLIGHT_PER_ANON if k[0] == 'anon' else INFLIGHT_PER_ID)
            if self._inflight.get(k, 0) >= cap:
                return await self._reject(send, 429, '同時に送信できる数を超えています。少し待ってからお試しください。', 2)

        ctx = _Ctx()
        ctx_token = _ctx.set(ctx)
        for k in keys:
            self._inflight[k] = self._inflight.get(k, 0) + 1
        self._inflight_total += 1
        try:
            if method in ('POST', 'PUT', 'PATCH'):
                return await self._dispatch_with_body_limit(scope, receive, send, path, idents, ctx)
            return await self.app(scope, receive, send)
        finally:
            for k in keys:
                n = self._inflight.get(k, 0) - 1
                if n > 0:
                    self._inflight[k] = n
                else:
                    self._inflight.pop(k, None)
            self._inflight_total -= 1
            _ctx.reset(ctx_token)
            self._settle(ctx, idents, token, dev, ip, time.monotonic())

    # ---- 本文サイズの上限 ----
    async def _dispatch_with_body_limit(self, scope, receive, send, path, idents, ctx):
        headers = {k: v for k, v in scope.get('headers', [])}
        ctype = headers.get(b'content-type', b'').decode('latin-1').lower()
        if path.startswith('/internal/'):
            max_body = MAX_INTERNAL_BODY_BYTES
        elif 'multipart/form-data' in ctype:
            max_body = MAX_BODY_BYTES
        else:
            max_body = MAX_JSON_BODY_BYTES
        try:
            declared = int(headers.get(b'content-length', b'0') or 0)
        except ValueError:
            declared = 0
        if declared > max_body:
            # 普通のミス(少し大きい画像)は軽く、桁違い(上限の3倍超)は悪質として記録
            ctx.flags.append('oversize_heavy' if declared > max_body * 3 else 'oversize')
            return await self._reject(send, 413, 'データが大きすぎます。', None)

        received = 0
        started = False
        too_large = False

        async def limited_receive():
            nonlocal received, too_large
            msg = await receive()
            if msg['type'] == 'http.request':
                received += len(msg.get('body', b''))
                if received > max_body:            # Content-Lengthを偽る/チャンク送信での巨大本文もここで止める
                    too_large = True
                    raise _BodyTooLarge()
            return msg

        async def tracking_send(msg):
            nonlocal started
            if too_large:                          # アプリ側のエラー応答(500)は捨てて、こちらで413を返す
                if not started:
                    started = True
                    await self._reject(send, 413, 'データが大きすぎます。', None)
                return
            if msg['type'] == 'http.response.start':
                started = True
            await send(msg)

        try:
            return await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            ctx.flags.append('oversize_heavy')     # 長さを偽る/申告なしで巨大本文を送るのは悪質とみなす
            if not started:
                started = True
                return await self._reject(send, 413, 'データが大きすぎます。', None)
            return

    # ---- 内部 ----
    def _identify(self, scope, ipk, now):
        """戻り値: (種別, 識別キーのリスト, token, dev, 既知か)
        種別 'id'   = cookieを持つ端末。キーは ('id', token) と ('dev', bbs_dev) の両方(片方を作り直しても残りで積み上がる)
        種別 'anon' = cookieなし・偽造・使い捨ての疑い。キーは ('anon', IP)"""
        token = _cookie(scope, b'user_bbs_token')
        dev = _cookie(scope, b'bbs_dev')
        t_ok, d_ok = valid_token(token), valid_token(dev)
        if not (t_ok or d_ok):
            return 'anon', [('anon', ipk)], None, None, False
        known = (t_ok and token in self._token_seen) or (d_ok and dev in self._token_seen)
        if not known:
            # 初めて見るcookie。同じIPから短時間に大量に現れるなら、使い捨てcookieとみなして匿名枠へ
            dq = self._new_tokens.setdefault(ipk, deque())
            _slide(dq, now, 60.0)
            if len(dq) >= NEW_TOKENS_PER_IP_PER_MIN:
                return 'anon', [('anon', ipk)], None, None, False
            dq.append(now)
        idents = []
        if t_ok:
            self._token_seen[token] = now
            idents.append(('id', token))
        if d_ok:
            self._token_seen[dev] = now
            idents.append(('dev', dev))
        return 'id', idents, (token if t_ok else None), (dev if d_ok else None), known

    def _violate(self, idents, now, weight=1):
        for k in idents:
            self._record_violation(k, now, weight)

    def _record_violation(self, ident, now, weight=1):
        dq = self._violations.setdefault(ident, deque())
        _slide(dq, now, VIOLATION_WINDOW)
        for _ in range(weight):
            dq.append(now)
        if len(dq) < VIOLATIONS_TO_BLOCK:
            return
        dq.clear()
        kind = ident[0]
        if kind == 'ip':
            secs = IP_BLOCK_SECONDS
        elif kind == 'anon':
            secs = ANON_BLOCK_SECONDS
        else:                                       # 端末: 繰り返すほど長く(5分→30分→6時間→24時間)
            st = self._strikes.setdefault(ident, deque())
            _slide(st, now, STRIKE_MEMORY)
            st.append(now)
            secs = BLOCK_STEPS[min(len(st), len(BLOCK_STEPS)) - 1]
        self._blocked_until[ident] = max(self._blocked_until.get(ident, 0), now + secs)
        print(f"[security_guard] {kind}:{str(ident[1])[:12]} を {secs} 秒ブロック(制限超過の繰り返し)")

    def _settle(self, ctx, idents, token, dev, ip, now):
        """リクエスト終了後に、処理中に記録された悪用フラグを集計する。"""
        if not ctx.flags:
            return
        heavy = False
        for f in ctx.flags:
            if f in ('image_bomb', 'oversize_heavy'):
                self._violate(idents, now, HEAVY_WEIGHT)
                heavy = True
            else:                                   # image_rate / image_oversize / oversize: 通常の違反1回分
                self._violate(idents, now, 1)
        who = token or dev
        if heavy and who:
            dq = self._malicious.setdefault(who, deque())
            _slide(dq, now, STRIKE_MEMORY)
            dq.append(now)
            if (AUTO_BAN_ENABLED and _abuse_handler and len(dq) >= AUTO_BAN_THRESHOLD and who not in self._ban_sent):
                self._ban_sent.add(who)
                info = {'token': token, 'dev': dev, 'ip': ip, 'reason': f"{[f for f in ctx.flags if f in ('image_bomb','oversize_heavy')][0]} x{len(dq)}"}
                try:
                    asyncio.get_running_loop().create_task(self._run_handler(info))
                except RuntimeError:
                    pass

    @staticmethod
    async def _run_handler(info):
        try:
            await _abuse_handler(info)
            print(f"[security_guard] 悪質な送信の繰り返しにより自動BANを依頼: {info.get('reason')}")
        except Exception as e:
            print(f"[security_guard] 自動BANの依頼に失敗: {e}")

    def _prune(self, now):
        for table, ttl in ((self._hits, 120), (self._violations, 120), (self._new_tokens, 120)):
            for k in [k for k, dq in table.items() if not dq or now - dq[-1] > ttl]:
                table.pop(k, None)
        for k in [k for k, t in self._blocked_until.items() if t < now]:
            self._blocked_until.pop(k, None)
        for k in [k for k, t in self._token_seen.items() if now - t > 3600]:
            self._token_seen.pop(k, None)
        for table in (self._strikes, self._malicious):
            for k in [k for k, dq in table.items() if not dq or now - dq[-1] > STRIKE_MEMORY]:
                table.pop(k, None)

    @staticmethod
    async def _reject(send, status, message, retry_after):
        body = json.dumps({'success': False, 'error': message}, ensure_ascii=False).encode('utf-8')
        headers = [(b'content-type', b'application/json; charset=utf-8'),
                   (b'content-length', str(len(body)).encode()),
                   (b'cache-control', b'no-store'),
                   (b'connection', b'close')]
        if retry_after:
            headers.append((b'retry-after', str(retry_after).encode()))
        await send({'type': 'http.response.start', 'status': status, 'headers': headers})
        await send({'type': 'http.response.body', 'body': body})
