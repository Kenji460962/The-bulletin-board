"""ブロック崩し(1人用スコアアタック + ランキング)。登録は battle_games/__init__.py の register_all() から。

ゲームの動きはブラウザ側(templates/breakout.html)。サーバーはページを返し、スコアをランキング用に受け取って保存する。
    GET  /breakout                    ゲーム画面
    POST /api/breakout/start          プレイ開始(セッションID発行)
    POST /api/breakout/submit         スコア送信(サーバー側で検証してベストを更新)
    GET  /api/breakout/ranking        ランキング(上位30)
    top_scores(n)                    /rankings ページ用(app.py の rankings() から呼ぶ)

スコアはブラウザからの申告なので完全には防げない。そこで
  ・start で発行したセッションIDが無いと送信できない(1セッション1回だけ)
  ・サーバー側で測った経過時間に対し、1秒あたりの最大スコアを超えたものは却下
  ・最短プレイ時間・スコア上限・同じプレイヤーの連続送信に間隔制限
で「明らかな不正」を弾く。ポイントは付与しない(不正の旨味を作らないため)。
セッションはメモリ保持なので、worker数1が前提。DoS対策(頻度制限など)は入口の security_guard が担当。
"""
import secrets
import time
from datetime import datetime
from urllib.parse import unquote

from fastapi import APIRouter, Request

import game_service
from game_service import Game, execute_query, get_current_member, get_json_silent, json_resp, templates

KEY = 'breakout'
LABEL = 'ブロック崩し'
TABLE = 'breakout_best'

MAX_PER_SEC = 200          # 経過1秒あたりに出せる現実的な最大スコア(超えたら不正扱い)
MAX_SCORE = 500000           # 絶対上限
MIN_SEC = 5                    # これより短いプレイでスコアが付くのは不正扱い
SESSION_TTL_SEC = 3 * 3600
SESSION_MAX = 5000
SUBMIT_MIN_INTERVAL_SEC = 3.0
RANKING_LIMIT = 30

_sessions = {}                 # {session_id: {'key', 't0'(monotonic), 'used'}}
_last_submit = {}              # {player_key: monotonic}

router = APIRouter()


async def ensure_schema():
    await execute_query(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        " player_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,"
        " best_score INTEGER NOT NULL DEFAULT 0, best_level INTEGER NOT NULL DEFAULT 1,"
        " plays INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)"
    )
    await execute_query(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_rank ON {TABLE} (best_score DESC)")


async def top_scores(n: int = RANKING_LIMIT):
    """ランキング(ベストスコアの高い順)。会員は users と結合してプロフィールリンク用の public_id を付ける。"""
    return await execute_query(
        f"SELECT b.display_name, b.best_score, b.best_level, b.plays, "
        f"       u.public_id AS public_id, u.icon_path AS icon_path "
        f"FROM {TABLE} b LEFT JOIN users u ON b.player_key = ('member:' || u.id) "
        f"WHERE b.best_score > 0 ORDER BY b.best_score DESC, b.updated_at ASC LIMIT ?", [n]
    ) or []


async def _identify(request: Request):
    """(player_key, 表示名, 端末token)。会員は会員ID、ゲストは端末cookie。表示名は生の文字列で保存し、表示側(Jinja)でエスケープする。"""
    token = game_service._game_token(request)
    member = await get_current_member(request)
    if member:
        return f"member:{member['id']}", (str(member.get('username') or '名無しさん').strip() or '名無しさん')[:20], token
    name = '名無しさん'
    raw = request.cookies.get('bbs_saved_author')
    if raw:
        try:
            name = unquote(raw)
        except Exception:
            name = raw
    name = ''.join(ch for ch in name if ch.isprintable()).strip()[:20] or '名無しさん'
    return f"guest:{token}", name, token


def _prune():
    now = time.monotonic()
    for sid in [s for s, v in _sessions.items() if v['used'] or now - v['t0'] > SESSION_TTL_SEC]:
        _sessions.pop(sid, None)
    while len(_sessions) > SESSION_MAX:
        _sessions.pop(next(iter(_sessions)), None)
    for k in [k for k, t in _last_submit.items() if now - t > 600]:
        _last_submit.pop(k, None)


def _security_headers(nonce: str) -> dict:
    return {
        # 許可するのは「このページのnonce付きの<script>/<style>」と、同じサイトへの通信(スコア送信)だけ
        'Content-Security-Policy': (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
        ),
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'same-origin',
        'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
        'Cross-Origin-Opener-Policy': 'same-origin',
    }


@router.get(f'/{KEY}')
async def page(request: Request):
    nonce = secrets.token_urlsafe(16)   # 毎回変わる使い捨ての値。攻撃者は予測できない
    resp = templates.TemplateResponse(request, f'{KEY}.html', {'nonce': nonce})
    resp.headers.update(_security_headers(nonce))
    return game_service._cookie_response(request, resp, game_service._game_token(request))


@router.post(f'/api/{KEY}/start')
async def start(request: Request):
    pkey, _name, token = await _identify(request)   # tokenは1回だけ決める(cookie未発行でも、セッションとcookieが食い違わないように)
    _prune()
    sid = secrets.token_urlsafe(16)
    _sessions[sid] = {'key': pkey, 't0': time.monotonic(), 'used': False}
    return game_service._cookie_response(request, json_resp({'success': True, 'session': sid}), token)


@router.post(f'/api/{KEY}/submit')
async def submit(request: Request):
    pkey, name, _token = await _identify(request)
    data = await get_json_silent(request)
    if not isinstance(data, dict):
        data = {}

    sess = _sessions.get(str(data.get('session') or ''))
    if not sess or sess['used'] or sess['key'] != pkey:
        return json_resp({'success': False, 'error': 'セッションが無効です。もう一度プレイしてください'}, 400)
    sess['used'] = True  # 成否に関わらず1セッション1回

    try:
        score = int(data.get('score'))
        level = max(1, min(int(data.get('level') or 1), 999))
    except (TypeError, ValueError):
        return json_resp({'success': False, 'error': 'スコアが不正です'}, 400)

    elapsed = time.monotonic() - sess['t0']   # サーバー側で測った時間だけを信用する
    if score < 0 or score > MAX_SCORE:
        return json_resp({'success': False, 'error': 'スコアが不正です'}, 400)
    if score > 0 and (elapsed < MIN_SEC or score > elapsed * MAX_PER_SEC + 200):
        print(f"{LABEL}不正疑い({pkey}): score={score} elapsed={elapsed:.1f}s")
        return json_resp({'success': False, 'error': 'スコアを記録できませんでした'}, 400)

    now_m = time.monotonic()
    if now_m - _last_submit.get(pkey, 0) < SUBMIT_MIN_INTERVAL_SEC:
        return json_resp({'success': False, 'error': '少し間をあけてください'}, 429)
    _last_submit[pkey] = now_m

    now = datetime.utcnow().isoformat()
    row = await execute_query(f"SELECT best_score FROM {TABLE} WHERE player_key = ?", [pkey])
    if not row:
        await execute_query(
            f"INSERT OR IGNORE INTO {TABLE} (player_key, display_name, best_score, best_level, plays, updated_at) "
            f"VALUES (?, ?, 0, 1, 0, ?)", [pkey, name, now])
        prev_best = 0
    else:
        prev_best = row[0]['best_score']

    is_best = score > prev_best
    if is_best:
        await execute_query(
            f"UPDATE {TABLE} SET best_score = ?, best_level = ?, plays = plays + 1, display_name = ?, updated_at = ? "
            f"WHERE player_key = ?", [score, level, name, now, pkey])
    else:
        await execute_query(
            f"UPDATE {TABLE} SET plays = plays + 1, display_name = ? WHERE player_key = ?", [name, pkey])

    best = max(score, prev_best)
    rank_row = await execute_query(f"SELECT COUNT(*) AS c FROM {TABLE} WHERE best_score > ?", [best])
    rank = (rank_row[0]['c'] + 1) if rank_row else None
    return json_resp({'success': True, 'is_best': is_best, 'best': best, 'rank': rank})


@router.get(f'/api/{KEY}/ranking')
async def ranking():
    rows = await top_scores(RANKING_LIMIT)
    resp = json_resp({'success': True, 'ranking': [
        {'name': r['display_name'], 'score': r['best_score'], 'level': r['best_level'],
         'public_id': r.get('public_id')} for r in rows]})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


GAME = Game(key=KEY, label=LABEL, router=router, lobby_path=f'/{KEY}', ensure_schema=ensure_schema)
