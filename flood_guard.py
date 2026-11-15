"""書き込みの「行動と中身」を見て、田代砲・AIの連投を抑える(security_guard.py の次の層)。

security_guard.py は「リクエストの量・大きさ」を見る層。こちらは「書き込みの中身と振る舞い」を見る層。
IPは「1人」として数えない(Webプロキシ・学校・モバイル回線では、1つのIPを大勢が共有するため)。

■ 考え方: いきなり止めない。段階を踏んで、普通の人には気づかれないようにする
  0. 通常          … 何も起きない。
  1. 待ってもらう  … 短時間に多すぎる/同じ内容の連投 → その端末だけ「あと◯秒」(429)。
  2. 1回だけ確認   … ボットらしい動き・攻撃中の新規端末 → Cloudflare Turnstileを1回通せば30分は免除。
                      Turnstile未設定なら「ゆっくりなら書ける」スローレーン(20秒に1回)に落とすだけで、締め出さない。
  3. 一時ミュート  … 確認を通らず悪質な行動を繰り返す端末だけ10分。BANはしない(BANは既存機能の役割)。

■ 見ているもの
  ・端末ごとの投稿ペース(cookie 2本 user_bbs_token / bbs_dev の両方を別々に数える)
  ・同じ内容/ほぼ同じ内容の繰り返し(記号・数字・URLだけ変えるコピペ対策)
  ・複数の端末から同じ文面が一斉に来る「一斉コピペ」(田代砲の典型)。短い文面(「草」など)は対象外
  ・投稿間隔が機械的に一定(AIや自動化ツールの連投)
  ・サイト全体の投稿量が危険な水準(30秒に45件超)になったら「攻撃モード」。このとき確認を求められるのは「常連でない端末」だけ。常連・会員は影響なし
  ・同じIPから使い捨てcookieで大量の新規端末が投稿してくる(cookie作り直し対策)。確認済みの端末は巻き込まない
  ・ハニーポット欄(人間には見えない欄に入力がある)、ページ表示から1秒未満での送信
  ・WebSocketの同時接続数(端末ごと・IPごと)

app.py からの使い方は同梱の PATCH_NOTES.md を参照。
"""
import asyncio
import hashlib
import hmac
import html
import math
import os
import re
import time
import unicodedata
from collections import deque, namedtuple

import httpx

import security_guard

# =========================================================
# 設定値(まずはこのまま。運用しながら調整する)
# =========================================================
TURNSTILE_SECRET = os.environ.get('TURNSTILE_SECRET', '')
TURNSTILE_SITEKEY = os.environ.get('TURNSTILE_SITEKEY', '')
FORM_SECRET = (os.environ.get('FLOOD_GUARD_SECRET') or os.environ.get('FLASK_SECRET_KEY') or 'change-me').encode()

# --- 投稿ペース(端末ごと) ---
REPLY_MIN_INTERVAL = 2.0                    # 同じ端末の連続投稿の最短間隔(秒)。従来の「IPごと3秒」を端末ごとに置き換え
REPLY_LIMITS = ((60.0, 12), (600.0, 60))    # (窓の秒数, 上限回数): 1分12回・10分60回
ANON_LIMITS = ((60.0, 8), (600.0, 30))      # cookieなし(IP単位の匿名枠)。cookieは最初のアクセスで配られるので、持たない投稿は自動化の疑い
# --- 同じ内容の繰り返し ---
DUP_WINDOW, DUP_PER_ID, DUP_WAIT = 600.0, 3, 60     # 10分に同じ内容が3回目 → 60秒待ち
SIMILAR_THRESHOLD = 0.85                             # 文字2-gramの類似度。これ以上は「ほぼ同じ」
MIN_NORM_LEN = 4                                     # 正規化後これ未満(「草」「うん」等)は内容チェックの対象外
# --- 一斉コピペ(複数端末から同じ文面) ---
CAMPAIGN_WINDOW, CAMPAIGN_IDS, CAMPAIGN_BLOCK, CAMPAIGN_MIN_LEN = 300.0, 4, 600.0, 6
# --- 機械的な間隔 ---
ROBOTIC_POSTS, ROBOTIC_MAX_MEAN, ROBOTIC_MAX_CV = 8, 25.0, 0.12   # 直近8投稿、平均25秒未満、ばらつき(変動係数)12%未満
# --- 攻撃モード(サイト全体) ---
ATTACK_POSTS_30S = 45                      # サイト全体で30秒に45投稿(=1.5件/秒)を超えたら。サーバー負荷そのものを見る。
                                           # 「新規ユーザーが急に増えた」だけでは発動しない(バズ・再起動直後・学校の一斉アクセスを攻撃扱いしないため)
ATTACK_SECONDS = 300
TRUST_MIN_POSTS, TRUST_MIN_AGE = 5, 600.0  # 5投稿以上かつ10分以上前から見ている端末 / ログイン会員は「常連」
# --- 使い捨てcookie ---
CHURN_IDS, CHURN_WINDOW, CHURN_RATIO, CHURN_RATE_60S = 30, 600.0, 0.5, 60   # 同じIPから10分に30台以上の「未確認の端末」が投稿し、かつ投稿の半数以上が「その端末の初投稿」
                                                        # さらに、その未確認端末の投稿が同じIPから1分に60件以上(=1秒に1件)。
                                                        # (cookieを毎回作り直す荒らしは投稿のほぼ全部が初投稿で速い。共有IPの普通の人たちは1人が何回も書く・ゆっくり書くので当たらない。
                                                        #  再起動直後や学校の一斉アクセスで「全員が初めて」に見えても、速度が足りないので当たらない)
# --- 違反の積み上げ ---
STRIKE_WINDOW = 600.0
STRIKES_FOR_CHALLENGE, STRIKES_FOR_MUTE, MUTE_SECONDS = 3, 8, 600
# --- Turnstile ---
VERIFY_TTL = 1800.0                        # 一度確認を通ったら30分は免除
VERIFY_CALLS_PER_MIN = 8                   # 確認APIを叩く回数の上限(端末ごと)。確認機能を踏み台にされないため
FALLBACK_WAIT = 20                         # Turnstile未設定時のスローレーン: 端末は20秒に1回
FALLBACK_IP_POSTS = 6                      #   ・同じIPの未確認端末は、全員合わせて20秒に6回(攻撃/使い捨てcookieのときだけ)
FALLBACK_GLOBAL_POSTS = 15                 #   ・攻撃モード中の未確認端末は、サイト全体で10秒に15回
# --- ハニーポット/フォームトークン ---
FORM_MIN_FILL = 1.0                        # ページ表示からこの秒数未満の送信は自動化とみなす
FORM_TOKEN_TTL = 6 * 3600
# --- WebSocket ---
WS_PER_TOKEN, WS_PER_IP, WS_PER_ANON_IP, WS_TOTAL = 8, 150, 30, 1500
TRACK_MAX_KEYS = 50000

Decision = namedtuple('Decision', 'action wait reason message')   # action: ok / wait / challenge / reject
_OK = Decision('ok', 0, '', '')


def _now() -> float:           # テストで差し替えられるように分けてある
    return time.monotonic()


# =========================================================
# 状態(すべてメモリ上。再起動すると「全員が初めて」に戻る点に注意 → 攻撃モードの誤爆を防ぐため判定は急増ベース)
# =========================================================
_posts = {}          # key -> deque[時刻]
_hist = {}           # key -> deque[(時刻, 正規化hash, 2-gram集合)]
_strikes = {}        # key -> deque[時刻]
_muted = {}          # key -> 解除時刻
_verified = {}       # key -> 免除の期限
_verify_calls = {}   # key -> deque[時刻]
_seen = {}           # key -> [初めて見た時刻, 成功投稿数]
_campaign = {}       # 正規化hash -> deque[(時刻, key)]
_campaign_block = {} # 正規化hash -> 解除時刻
_ip_ids = {}         # ipk -> deque[(時刻, key)]  未確認の新規端末の投稿
_site_posts = deque()
_lane_ip = {}         # ipk -> deque[時刻]  スローレーン(Turnstile未設定時)のIP単位の枠
_lane_global = deque()  # 攻撃モード中の「未確認端末」全体の枠
_attack_until = 0.0
_ws_count = {}
_ws_total = 0
_last_prune = 0.0


# =========================================================
# 補助
# =========================================================
def _slide(dq, now, window):
    while dq and now - dq[0] > window:
        dq.popleft()


def _slide_t(dq, now, window):          # (時刻, ...) のタプルを持つdeque用
    while dq and now - dq[0][0] > window:
        dq.popleft()


def _keys(ip, token, dev):
    """端末を表すキー。cookie 2本を別々に数える(片方を作り直しても、残る1本で積み上がる)。"""
    ks = []
    if security_guard.valid_token(token):
        ks.append(('t', token))
    if security_guard.valid_token(dev):
        ks.append(('d', dev))
    ipk = security_guard.ip_key(ip) if ip else 'unknown'
    if not ks:
        return [('a', ipk)], ipk, True       # cookieなし/偽造 = IP単位の匿名枠
    return ks, ipk, False


def _norm(text: str) -> str:
    t = unicodedata.normalize('NFKC', html.unescape(text or '')).lower()
    t = re.sub(r'>>\d+', '', t)                    # アンカーは無視
    t = re.sub(r'https?://\S+', 'u', t)            # URLは同一視
    t = re.sub(r'[\W\d_]+', '', t)                 # 空白・記号・数字・絵文字を除去(かな・漢字・英字は残る)
    t = re.sub(r'(.)\1{2,}', r'\1\1', t)           # 「wwwww」「草草草草」→ 短く
    return t[:300]


def _grams(t: str):
    return frozenset(t[i:i + 2] for i in range(len(t) - 1)) if len(t) > 1 else frozenset()


def _jaccard(a, b) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _strike(keys, now, weight=1):
    for k in keys:
        dq = _strikes.setdefault(k, deque())
        _slide(dq, now, STRIKE_WINDOW)
        for _ in range(weight):
            dq.append(now)


def _strike_count(keys, now) -> int:
    n = 0
    for k in keys:
        dq = _strikes.get(k)
        if dq:
            _slide(dq, now, STRIKE_WINDOW)
            n = max(n, len(dq))
    return n


def _is_verified(keys, now) -> bool:
    return any(_verified.get(k, 0) > now for k in keys)


def _is_trusted(keys, is_member, now) -> bool:
    if is_member:
        return True
    for k in keys:
        s = _seen.get(k)
        if s and s[1] >= TRUST_MIN_POSTS and now - s[0] >= TRUST_MIN_AGE:
            return True
    return False


def _pace_wait(keys, now, limits, min_interval):
    """戻り値: (待ち秒数, 窓の上限に当たったか)。最短間隔だけの待ちは違反として数えない(連打した人を罰しないため)。"""
    wait, hard = 0, False
    longest = max(w for w, _ in limits)
    for k in keys:
        dq = _posts.setdefault(k, deque())
        _slide(dq, now, longest)
        if dq and now - dq[-1] < min_interval:
            wait = max(wait, math.ceil(min_interval - (now - dq[-1])))
        for w, m in limits:
            recent = [t for t in dq if now - t <= w]
            if len(recent) >= m:
                wait = max(wait, math.ceil(w - (now - recent[0])))
                hard = True
    return wait, hard


def _is_robotic(keys, now) -> bool:
    for k in keys:
        dq = _posts.get(k)
        if not dq or len(dq) < ROBOTIC_POSTS:
            continue
        ts = list(dq)[-ROBOTIC_POSTS:]
        iv = [b - a for a, b in zip(ts, ts[1:])]
        mean = sum(iv) / len(iv)
        if mean <= 0 or mean >= ROBOTIC_MAX_MEAN:
            continue
        var = sum((x - mean) ** 2 for x in iv) / len(iv)
        if math.sqrt(var) / mean < ROBOTIC_MAX_CV:
            return True
    return False


def attack_mode(now=None) -> bool:
    return (now if now is not None else _now()) < _attack_until


# =========================================================
# フォームトークン(ハニーポットと組み合わせる。無い/古いトークンは「違反」にしない=キャッシュしたページや旧クライアントを巻き込まない)
# =========================================================
def issue_form_token(ts=None) -> str:
    ts = str(int(ts if ts is not None else time.time()))
    sig = hmac.new(FORM_SECRET, ts.encode(), hashlib.sha256).hexdigest()[:20]
    return f'{ts}.{sig}'


def _form_age(tok):
    """署名が正しければ「ページ表示からの経過秒」。それ以外はNone(判定材料にしない)。"""
    try:
        ts, sig = (tok or '').split('.', 1)
        good = hmac.new(FORM_SECRET, ts.encode(), hashlib.sha256).hexdigest()[:20]
        if not hmac.compare_digest(sig, good):
            return None
        age = time.time() - int(ts)
        return age if 0 <= age <= FORM_TOKEN_TTL else None
    except Exception:
        return None


# =========================================================
# Turnstile
# =========================================================
_http = None


async def _verify_turnstile(response: str, ip) -> bool:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=4.0)
    try:
        r = await _http.post('https://challenges.cloudflare.com/turnstile/v0/siteverify',
                             data={'secret': TURNSTILE_SECRET, 'response': response, 'remoteip': ip or ''})
        return bool(r.json().get('success'))
    except Exception as e:
        print(f'[flood_guard] Turnstile確認に失敗: {e}')
        return False


async def _gate(keys, ipk, ip, now, reasons, turnstile_token, campaign_wait=0):
    """確認が必要な理由(reasons)があるとき、確認を通ったか調べる。通れば None、止めるなら Decision を返す。
    Turnstileが使えない/未設定のときは、締め出さずに「スローレーン」へ落とす(通る人は通り、量だけが絞られる)。"""
    if not reasons or _is_verified(keys, now):
        return None
    rs = ','.join(reasons)
    if TURNSTILE_SECRET and turnstile_token:
        dq = _verify_calls.setdefault(keys[0], deque())
        _slide(dq, now, 60.0)
        if len(dq) >= VERIFY_CALLS_PER_MIN:
            return Decision('wait', 30, 'verify_rate', '確認の試行が多すぎます。少し待ってからお試しください。')
        dq.append(now)
        if await _verify_turnstile(turnstile_token, ip):
            for k in keys:
                _verified[k] = now + VERIFY_TTL
                _strikes.pop(k, None)
                _muted.pop(k, None)
            return None
        return Decision('challenge', 0, 'turnstile_failed', '確認に失敗しました。もう一度お試しください。')
    if TURNSTILE_SECRET:
        return Decision('challenge', 0, rs, 'ボットでないことを確認してください。')

    # ---- Turnstile未設定: スローレーン ----
    if campaign_wait:
        return Decision('wait', campaign_wait, rs, f'同じ文面が多数の端末から送られています。{campaign_wait}秒お待ちください。')
    dq = _posts.get(keys[0])
    if dq and now - dq[-1] < FALLBACK_WAIT:
        w = math.ceil(FALLBACK_WAIT - (now - dq[-1]))
        return Decision('wait', w, rs, f'投稿が集中しています。{w}秒お待ちください。')
    population = 'attack' in reasons or 'churn' in reasons
    if population:
        idq = _lane_ip.setdefault(ipk, deque())
        _slide(idq, now, FALLBACK_WAIT)
        if len(idq) >= FALLBACK_IP_POSTS:
            w = math.ceil(FALLBACK_WAIT - (now - idq[0]))
            return Decision('wait', w, rs, f'投稿が集中しています。{w}秒お待ちください。')
        if 'attack' in reasons:
            _slide(_lane_global, now, 10.0)
            if len(_lane_global) >= FALLBACK_GLOBAL_POSTS:
                return Decision('wait', 5, rs, '投稿が集中しています。5秒お待ちください。')
            _lane_global.append(now)
        idq.append(now)
    return None


# =========================================================
# 本体: レスの投稿チェック
# =========================================================
async def check_post(ip, token, dev, content, thread_id=0, *, is_member=False,
                     form_token=None, honeypot=None, turnstile_token=None) -> Decision:
    """投稿してよいか判定する。'ok' のときだけ1回分を記録する。運営(staff)には呼ばないこと。"""
    global _attack_until
    now = _now()
    if ip in security_guard.TRUSTED_IPS:
        return _OK
    _maybe_prune(now)
    keys, ipk, anon = _keys(ip, token, dev)
    primary = keys[0]

    # --- 自動化の痕跡 ---
    if honeypot:
        _strike(keys, now, 3)
        return Decision('reject', 0, 'honeypot', '投稿できませんでした。')
    age = _form_age(form_token)
    if age is not None and age < FORM_MIN_FILL:
        _strike(keys, now, 2)
        return Decision('wait', 3, 'too_fast', '操作が速すぎます。少し待ってからお試しください。')

    verified = _is_verified(keys, now)
    if not verified and any(_muted.get(k, 0) > now for k in keys):
        w = int(max(_muted.get(k, 0) for k in keys) - now) + 1
        return Decision('wait', w, 'muted', f'投稿が集中しています。{w}秒ほどお待ちください。')

    # --- ペース ---
    wait, hard = _pace_wait(keys, now, ANON_LIMITS if anon else REPLY_LIMITS, REPLY_MIN_INTERVAL)
    if wait:
        if hard:
            _strike(keys, now)
        return Decision('wait', wait, 'pace', f'連続投稿はできません。{wait}秒お待ちください。')

    # --- 内容 ---
    n = _norm(content)
    g = _grams(n)
    h = hashlib.sha1(n.encode()).hexdigest()[:16] if len(n) >= MIN_NORM_LEN else None
    reasons = []
    if h:
        for k in keys:
            dq = _hist.get(k)
            if not dq:
                continue
            _slide_t(dq, now, DUP_WINDOW)
            same = sum(1 for _t, hh, gg in dq if hh == h or (len(n) >= 10 and _jaccard(g, gg) >= SIMILAR_THRESHOLD))
            if same >= DUP_PER_ID - 1:
                _strike(keys, now)
                return Decision('wait', DUP_WAIT, 'duplicate', f'同じ内容の連続投稿はできません。{DUP_WAIT}秒お待ちください。')
        if len(n) >= CAMPAIGN_MIN_LEN:
            cdq = _campaign.setdefault(h, deque())
            _slide_t(cdq, now, CAMPAIGN_WINDOW)
            if len({kk for _t, kk in cdq} | {primary}) >= CAMPAIGN_IDS:
                _campaign_block[h] = now + CAMPAIGN_BLOCK
            if _campaign_block.get(h, 0) > now:
                reasons.append('campaign')

    # --- 確認が必要か ---
    trusted = _is_trusted(keys, is_member, now)
    if _strike_count(keys, now) >= STRIKES_FOR_CHALLENGE:
        reasons.append('strikes')
    if _is_robotic(keys, now):
        reasons.append('robotic')
    _slide(_site_posts, now, 30.0)
    if len(_site_posts) >= ATTACK_POSTS_30S:
        if not attack_mode(now):
            print('[flood_guard] 攻撃モードに入りました(新規端末の投稿に確認を求めます)')
        _attack_until = now + ATTACK_SECONDS
    if attack_mode(now) and not trusted:
        reasons.append('attack')
    if not trusted and not verified:
        idq = _ip_ids.setdefault(ipk, deque())
        _slide_t(idq, now, CHURN_WINDOW)
        ids = {kk for _t, kk in idq} | {primary}
        fast = sum(1 for _t, _k in idq if now - _t <= 60.0)
        if len(ids) >= CHURN_IDS and len(ids) / (len(idq) + 1) >= CHURN_RATIO and fast >= CHURN_RATE_60S:
            reasons.append('churn')

    camp_wait = int(_campaign_block.get(h, 0) - now) + 1 if (h and 'campaign' in reasons) else 0
    blocked = await _gate(keys, ipk, ip, now, reasons, turnstile_token, camp_wait)
    if blocked:
        if blocked.action == 'challenge' and _strike_count(keys, now) >= STRIKES_FOR_MUTE:
            for k in keys:
                _muted[k] = now + MUTE_SECONDS
            print(f'[flood_guard] {primary[0]}:{str(primary[1])[:10]} を {MUTE_SECONDS}秒ミュート(確認を通らず悪質な行動を繰り返した)')
        return blocked

    # --- 記録(ここまで通った投稿だけ) ---
    for k in keys:
        _posts.setdefault(k, deque()).append(now)
        _hist.setdefault(k, deque(maxlen=8)).append((now, h or '', g))
        s = _seen.get(k)
        if s is None:
            _seen[k] = [now, 1]
        else:
            s[1] += 1
    _site_posts.append(now)
    if h and len(n) >= CAMPAIGN_MIN_LEN:
        _campaign.setdefault(h, deque()).append((now, primary))
    if not trusted and not verified:
        _ip_ids.setdefault(ipk, deque()).append((now, primary))
    return _OK


def decision_response(dec: Decision):
    """app.py の json_resp に渡す (本文dict, HTTPステータス)。"""
    if dec.action == 'wait':
        return {'success': False, 'error': dec.message, 'retry_after': dec.wait}, 429
    if dec.action == 'challenge':
        return {'success': False, 'challenge': 'turnstile', 'sitekey': TURNSTILE_SITEKEY, 'error': dec.message}, 403
    return {'success': False, 'error': dec.message or '投稿できませんでした。'}, 400


def snapshot() -> dict:
    """管理画面やログで状況を見る用。"""
    now = _now()
    return {'attack_mode': attack_mode(now), 'tracked_keys': len(_posts), 'verified': sum(1 for v in _verified.values() if v > now),
            'muted': sum(1 for v in _muted.values() if v > now), 'ws_total': _ws_total,
            'campaign_blocked': sum(1 for v in _campaign_block.values() if v > now)}


# =========================================================
# WebSocket の同時接続数
# =========================================================
def ws_acquire(ip, token) -> bool:
    """接続してよいなら True を返して1本分を数える。切断時は必ず ws_release を呼ぶ。"""
    global _ws_total
    if _ws_total >= WS_TOTAL:
        return False
    ipk = security_guard.ip_key(ip) if ip else 'unknown'
    if security_guard.valid_token(token):
        keys = [(('wt', token), WS_PER_TOKEN), (('wi', ipk), WS_PER_IP)]
    else:
        keys = [(('wa', ipk), WS_PER_ANON_IP)]
    if any(_ws_count.get(k, 0) >= cap for k, cap in keys):
        return False
    for k, _cap in keys:
        _ws_count[k] = _ws_count.get(k, 0) + 1
    _ws_total += 1
    return True


def ws_release(ip, token):
    global _ws_total
    ipk = security_guard.ip_key(ip) if ip else 'unknown'
    keys = [('wt', token), ('wi', ipk)] if security_guard.valid_token(token) else [('wa', ipk)]
    for k in keys:
        n = _ws_count.get(k, 0) - 1
        if n > 0:
            _ws_count[k] = n
        else:
            _ws_count.pop(k, None)
    _ws_total = max(0, _ws_total - 1)


# =========================================================
# メモリ掃除(check_post から1分に1回だけ走る)
# =========================================================
def _maybe_prune(now):
    global _last_prune
    if now - _last_prune < 60.0:
        return
    _last_prune = now
    for table, ttl in ((_posts, 700.0), (_strikes, STRIKE_WINDOW + 60), (_verify_calls, 120.0)):
        for k in [k for k, dq in table.items() if not dq or now - dq[-1] > ttl]:
            table.pop(k, None)
    for table in (_hist, _campaign, _ip_ids):
        for k in [k for k, dq in table.items() if not dq or now - dq[-1][0] > 700.0]:
            table.pop(k, None)
    for k in [k for k, dq in _lane_ip.items() if not dq or now - dq[-1] > 60.0]:
        _lane_ip.pop(k, None)
    for table in (_muted, _verified, _campaign_block):
        for k in [k for k, t in table.items() if t < now]:
            table.pop(k, None)
    for k in [k for k, s in _seen.items() if now - s[0] > 86400.0]:
        _seen.pop(k, None)
    if len(_seen) > TRACK_MAX_KEYS:                       # 最後の砦: 古い順に捨てる
        for k in sorted(_seen, key=lambda x: _seen[x][0])[:len(_seen) - TRACK_MAX_KEYS // 2]:
            _seen.pop(k, None)
