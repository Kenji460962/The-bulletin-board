"""対戦ゲーム(オセロ・チェス・将棋・人狼)の共通サービス。

カジノ(casino_service.py + casino_games/)と同じ構造:

    game_service.py          … 共通部品(プレイヤー識別・Elo・ポイント・観戦チャット・ゲーム一覧)とレジストリ
    battle_games/__init__.py … register_all() に import + register_game を並べる
    battle_games/<game>.py   … ゲームごとのロジックとルート。末尾で GAME = Game(...) を公開する

新しいゲームを足すには、battle_games/ にファイルを作って GAME を公開し、
__init__.py に「import」と「register_game(xxx.GAME)」を1行ずつ足すだけでよい。

app.py 側:

    import game_service, battle_games
    game_service.init(globals())               # app.pyのヘルパー(DB・認証など)を渡す
    battle_games.register_all(game_service)    # 各ゲームを登録
    game_service.mount(app)                    # ルート登録
    # startupフック内: await game_service.ensure_schemas()

app.py を import しないので、`python app.py`(__main__)で起動しても二重実行にならない。
"""
import asyncio
import html
import json
import points_service
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import unquote
from typing import Callable, Optional
from dataclasses import dataclass

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

# =========================
# app.py から init() で受け取るもの
# =========================
# 値のコピーではなく「呼び出し時に取り出す」形にしてあるので、app.py側で後から
# 差し替わるもの(redis_client など)にも追従できる。
_APP_DEPS = (
    'REALTIME_CHANNEL', '_db_lock', '_get_db_conn', '_thread_ws_identity',
    'execute_query', 'filter_ng_words', 'get_current_member',
    'get_json_silent', 'json_resp', 'templates',
)
_deps = {}


def init(app_globals: dict):
    """app.py の globals() を渡す。"""
    _deps.update({n: app_globals[n] for n in _APP_DEPS})
    _deps['_g'] = app_globals


async def execute_query(sql, params=None):
    return await _deps['execute_query'](sql, params)


async def _get_db_conn():
    return await _deps['_get_db_conn']()


async def get_current_member(request):
    return await _deps['get_current_member'](request)


async def get_json_silent(request):
    return await _deps['get_json_silent'](request)


async def _thread_ws_identity(websocket):
    return await _deps['_thread_ws_identity'](websocket)


def filter_ng_words(text):
    return _deps['filter_ng_words'](text)


def json_resp(content, status_code: int = 200):
    return _deps['json_resp'](content, status_code)


class _LockProxy:
    """app.py の _db_lock をそのまま使う(`async with _db_lock:` で書ける)。"""
    async def __aenter__(self):
        return await _deps['_db_lock'].__aenter__()

    async def __aexit__(self, *exc):
        return await _deps['_db_lock'].__aexit__(*exc)


_db_lock = _LockProxy()


class _TemplatesProxy:
    def TemplateResponse(self, *args, **kwargs):
        return _deps['templates'].TemplateResponse(*args, **kwargs)


templates = _TemplatesProxy()


def _redis():
    # redis_client は app.py の startup で再代入されるので、使う時点で毎回取り直す
    return _deps['_g'].get('redis_client')


# =========================
# ゲームのレジストリ(casino_service.register_game に相当)
# =========================
router = APIRouter()   # 共通ルート(/games, /api/games/live, /ws/gamechat/...)


@dataclass
class Game:
    key: str                                  # 'othello' など。URL・ポイント・観戦で使う識別子
    label: str                                # 表示名
    router: APIRouter                         # そのゲームのルート
    lobby_path: str                           # ロビーのURL('/game' など)
    room_meta: Optional[dict] = None          # 2人対戦ゲームだけ: 観戦・降参処理用(table, a, b, path, label_a, label_b)
    has_in_check_col: bool = False            # 部屋テーブルに in_check 列があるか(降参時に NULL へ戻す)
    ensure_schema: Optional[Callable] = None  # 起動時に呼ぶテーブル作成(async)


_GAMES = {}


def register_game(game: Game):
    if game.key in _GAMES:
        raise ValueError(f'game already registered: {game.key}')
    _GAMES[game.key] = game
    GAME_LABELS[game.key] = game.label
    if game.room_meta:
        _GAME_ROOM_META[game.key] = game.room_meta
        _RESIGNABLE_TABLES[game.room_meta['table']] = game.has_in_check_col


def mount(app):
    """共通ルート → 各ゲームのルートの順に FastAPI へ登録する。"""
    app.include_router(router)
    for game in _GAMES.values():
        app.include_router(game.router)


async def ensure_schemas():
    for game in _GAMES.values():
        if game.ensure_schema:
            await game.ensure_schema()




GAME_POINTS_WIN = 20
GAME_POINTS_LOSS = 5
GAME_LABELS = {}  # register_game() が埋める {ゲームkey: 表示名}
GAME_RESIGN_PENALTY = 10  # 降参したログイン会員から引くポイント(残高が足りなければ残高の範囲で)


async def _award_game_result_points(game: str, room_code: str,
                                     member_id_a, result_a: float,
                                     member_id_b, result_b: float):
    """対局終了時、ログイン会員である対局者にポイントを付与する。

    - ゲスト(member_idがNone)はusersテーブルの行を持たないため付与対象外。
    - 引き分け(result=0.5)は今回の付与ルール(勝利+20/敗北+5)の対象外として
      仕様上未定義のため、ポイントは付与しない(オセロ・チェスの持将棋/千日手等)。
    - idempotency_key に room_code(対局ごとに使い捨てで再利用されない一意な
      部屋コード)とuser_idを含めることで、着手の同時実行等により勝敗確定処理が
      複数回走っても同じユーザーへの二重付与を防ぐ。
    """
    label = GAME_LABELS.get(game, game)
    for member_id, result in ((member_id_a, result_a), (member_id_b, result_b)):
        if not member_id:
            continue
        try:
            user_id = int(member_id)
        except (TypeError, ValueError):
            continue
        if result == 1:
            delta, reason, desc = GAME_POINTS_WIN, 'game_win', f'{label}で勝利'
        elif result == 0:
            delta, reason, desc = GAME_POINTS_LOSS, 'game_loss', f'{label}で敗北'
        else:
            continue  # 引き分け: 付与ルール未定義のためスキップ
        try:
            await points_service.add_points(
                user_id, delta, reason=reason, description=desc, source='game',
                idempotency_key=f'game:{game}:{room_code}:{user_id}',
            )
        except Exception as e:
            print(f"ゲームポイント付与エラー({game}, room={room_code}, user={user_id}): {e}")


_RESIGNABLE_TABLES = {}  # register_game() が埋める {テーブル名: in_check列の有無}


async def _finish_room_if_playing(table: str, room_code: str, winner: str) -> bool:
    """部屋が「対局中」のときだけ終了(finished)にして勝者を記録する。更新できたら True。
    execute_query は書き込みの結果(更新件数)を返さないため、ここでは接続を直接使って
    rowcount を確認する。降参の二重送信や、相手の着手との競合で二重処理されないための関門。"""
    if table not in _RESIGNABLE_TABLES:
        return False
    has_in_check = _RESIGNABLE_TABLES[table]
    conn = await _get_db_conn()
    async with _db_lock:
        cur = await conn.execute(
            f"UPDATE {table} SET status = 'finished', winner = ?, "
            + ("in_check = NULL, " if has_in_check else "")
            + "updated_at = ? WHERE room_code = ? AND status = 'playing'",
            [winner, datetime.utcnow().isoformat(), room_code],
        )
        changed = cur.rowcount
        await cur.close()
        await conn.commit()
    return changed > 0


async def _award_game_resign_result(game: str, room_code: str,
                                    resigner_member_id, winner_member_id):
    """降参による対局終了時のポイント処理。

    - 降参した側: 敗北の+5は付けず、代わりに GAME_RESIGN_PENALTY ポイントを引く
      (残高がそれ未満なら残高ぶんだけ。ゲスト・残高0の人は対象外)。
    - 降参された側(勝者): 通常の勝利と同じ(+20)。
    レーティングと勝敗は、通常の勝ち負けと同じく apply_game_result で更新される。
    """
    label = GAME_LABELS.get(game, game)
    if resigner_member_id:
        try:
            uid = int(resigner_member_id)
            balance = await points_service.get_balance(uid)
            penalty = min(GAME_RESIGN_PENALTY, balance)
            if penalty > 0:
                await points_service.add_points(
                    uid, -penalty, reason='game_resign', description=f'{label}で降参(ペナルティ)',
                    source='game', idempotency_key=f'game:{game}:{room_code}:{uid}',
                )
        except Exception as e:
            print(f"降参ペナルティ処理エラー({game}, room={room_code}): {e}")
    if winner_member_id:
        try:
            uid = int(winner_member_id)
            await points_service.add_points(
                uid, GAME_POINTS_WIN, reason='game_win', description=f'{label}で勝利',
                source='game', idempotency_key=f'game:{game}:{room_code}:{uid}',
            )
        except Exception as e:
            print(f"降参勝利ポイント付与エラー({game}, room={room_code}): {e}")


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


def _cookie_response(request: Request, resp, token):
    if not request.cookies.get('game_player_token'):
        resp.set_cookie('game_player_token', token, max_age=60 * 60 * 24 * 365, httponly=True, samesite='Lax')
    return resp


@router.get('/games')
async def games_hub(request: Request):
    games = [{'key': g.key, 'label': g.label, 'path': g.lobby_path} for g in _GAMES.values()]
    return templates.TemplateResponse(request, 'games_hub.html', {'games': games})


# =========================
# 観戦機能(プレイ中ゲーム一覧) + 観戦チャット
# =========================
# ・対戦が始まった(status='playing')部屋は /api/games/live の一覧に載り、誰でも観戦できる。
#   観戦者は対局の部屋URLをそのまま開くだけ(席が埋まっていれば自動的に観戦者になる)。
# ・ゲーム画面の右側に、対戦者と観戦者が話せるチャットを置く(/ws/gamechat/{game}/{code})。
# ・チャットは対局が終わった瞬間に削除する(履歴を消し、接続中の全員へ closed を通知)。
#   履歴はRedisがあればRedisのリスト(TTL付き)、無ければプロセス内メモリに置く。

_GAME_ROOM_META = {}  # register_game() が埋める {ゲームkey: 部屋テーブル・席名・URL等}
GAME_LIVE_WINDOW = timedelta(hours=6)    # 最後の着手からこれより経った「対局中」は放置とみなして一覧に出さない
GAME_LIVE_LIST_LIMIT = 40
GAME_CHAT_MAX_LEN = 200
GAME_CHAT_HISTORY_MAX = 100
GAME_CHAT_TTL_SEC = 6 * 3600             # Redis上の履歴の保険用TTL(通常は対局終了時に即削除)
GAME_CHAT_MIN_INTERVAL = 1.0             # 秒。連投防止
GAME_CHAT_IDLE_TIMEOUT = 70              # 秒。クライアントは約25秒ごとにpingを送る
GAME_CHAT_MEM_ROOMS_MAX = 500


class GameChatManager:
    def __init__(self):
        # {room_key: {websocket: who}}  room_key = "othello:ABC123"
        self.rooms: dict[str, dict[WebSocket, dict]] = {}
        # Redisが使えないときの履歴置き場
        self._mem_history: dict[str, list] = {}

    @staticmethod
    def key(game: str, code: str) -> str:
        return f"{game}:{code}"

    async def connect(self, key: str, websocket: WebSocket, who: dict):
        await websocket.accept()
        self.rooms.setdefault(key, {})[websocket] = who

    def disconnect(self, key: str, websocket: WebSocket):
        room = self.rooms.get(key)
        if room is None or websocket not in room:
            return
        del room[websocket]
        if not room:
            del self.rooms[key]
        else:
            try:
                asyncio.get_running_loop().create_task(self.broadcast_presence(key))
            except RuntimeError:
                pass

    def presence_payload(self, key: str) -> dict:
        players, spectators = set(), set()
        for who in self.rooms.get(key, {}).values():
            (players if who['role'] == 'player' else spectators).add(who['uid'])
        return {"type": "presence", "players": len(players), "spectators": len(spectators)}

    async def broadcast_presence(self, key: str):
        await self._send_local(key, self.presence_payload(key))

    async def _send_local(self, key: str, payload: dict):
        dead = []
        for ws in list(self.rooms.get(key, {})):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(key, ws)

    async def broadcast_local(self, key: str, payload: dict):
        # 同一プロセス内の接続にだけ配信する(全worker分はpublish()がRedis経由で呼び合う)
        await self._send_local(key, payload)
        if payload.get('type') == 'closed':
            # 対局終了: 接続を畳む(クライアントも closed を受けたら再接続しない)
            for ws in list(self.rooms.pop(key, {})):
                try:
                    await ws.close()
                except Exception:
                    pass

    async def publish(self, key: str, payload: dict):
        if _redis() is None:
            await self.broadcast_local(key, payload)
            return
        try:
            await _redis().publish(_deps['REALTIME_CHANNEL'], json.dumps(
                {"kind": "game_chat", "key": key, "payload": payload}, ensure_ascii=False))
        except Exception as e:
            print(f"Redis publish エラー(game chat): {e}")
            await self.broadcast_local(key, payload)

    async def history(self, key: str) -> list:
        if _redis() is not None:
            try:
                raw = await _redis().lrange(f"gamechat:{key}", 0, -1)
                return [json.loads(x) for x in raw]
            except Exception as e:
                print(f"ゲームチャット履歴取得エラー: {e}")
        return list(self._mem_history.get(key, []))

    async def add_history(self, key: str, msg: dict):
        if _redis() is not None:
            try:
                rk = f"gamechat:{key}"
                await _redis().rpush(rk, json.dumps(msg, ensure_ascii=False))
                await _redis().ltrim(rk, -GAME_CHAT_HISTORY_MAX, -1)
                await _redis().expire(rk, GAME_CHAT_TTL_SEC)
                return
            except Exception as e:
                print(f"ゲームチャット履歴保存エラー: {e}")
        lst = self._mem_history.setdefault(key, [])
        lst.append(msg)
        del lst[:-GAME_CHAT_HISTORY_MAX]
        while len(self._mem_history) > GAME_CHAT_MEM_ROOMS_MAX:
            self._mem_history.pop(next(iter(self._mem_history)), None)

    async def clear_history(self, key: str):
        self._mem_history.pop(key, None)
        if _redis() is not None:
            try:
                await _redis().delete(f"gamechat:{key}")
            except Exception as e:
                print(f"ゲームチャット履歴削除エラー: {e}")

    async def close_room(self, key: str):
        # 対局終了: 履歴を消し、全員にチャット終了を知らせる
        await self.clear_history(key)
        await self.publish(key, {"type": "closed"})


game_chat_manager = GameChatManager()


async def _game_chat_close(game: str, room_code: str):
    # 対局が終わった時に呼ぶ。失敗しても対局の終了処理は止めない。
    try:
        await game_chat_manager.close_room(game_chat_manager.key(game, room_code))
    except Exception as e:
        print(f"ゲームチャット終了処理エラー({game}, {room_code}): {e}")


@router.get('/api/games/live')
async def api_games_live(request: Request):
    # 現在プレイ中(マッチ成立済み)のゲーム一覧。観戦リンクは path + room_code。
    cutoff = (datetime.utcnow() - GAME_LIVE_WINDOW).isoformat()

    async def fetch(game: str, meta: dict):
        a, b = meta['a'], meta['b']
        try:
            rows = await execute_query(
                f"SELECT room_code, {a}_name AS a_name, {b}_name AS b_name, updated_at "
                f"FROM {meta['table']} WHERE status = 'playing' AND updated_at >= ? "
                f"ORDER BY updated_at DESC LIMIT {GAME_LIVE_LIST_LIMIT}",
                [cutoff],
            )
        except Exception as e:
            print(f"観戦一覧取得エラー({game}): {e}")
            return []
        out = []
        for r in rows or []:
            code = r['room_code']
            pres = game_chat_manager.presence_payload(game_chat_manager.key(game, code))
            out.append({
                'game': game,
                'game_label': GAME_LABELS.get(game, game),
                'room_code': code,
                'url': meta['path'] + code,
                'a_label': meta['label_a'], 'b_label': meta['label_b'],
                # 名前は保存時にHTMLエスケープ済みなので戻して返す(表示側はtextContentで出す)
                'a_name': html.unescape(r.get('a_name') or '名無しさん'),
                'b_name': html.unescape(r.get('b_name') or '名無しさん'),
                'spectators': pres['spectators'],
                'updated_at': r.get('updated_at') or '',
            })
        return out

    results = await asyncio.gather(*(fetch(g, m) for g, m in _GAME_ROOM_META.items()))
    games = [g for sub in results for g in sub]
    games.sort(key=lambda g: g['updated_at'], reverse=True)
    resp = json_resp({'success': True, 'games': games[:GAME_LIVE_LIST_LIMIT]})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@router.websocket('/ws/gamechat/{game}/{room_code}')
async def game_chat_ws(websocket: WebSocket, game: str, room_code: str):
    meta = _GAME_ROOM_META.get(game)
    code = (room_code or '').upper()
    if not meta or not re.fullmatch(r'[A-Z0-9]{6}', code):
        await websocket.close(code=1008)
        return
    a, b = meta['a'], meta['b']
    sql = (f"SELECT status, {a}_token AS a_token, {b}_token AS b_token, "
           f"{a}_name AS a_name, {b}_name AS b_name FROM {meta['table']} WHERE room_code = ? LIMIT 1")
    rows = await execute_query(sql, [code])
    if not rows:
        await websocket.close(code=1008)
        return
    room = rows[0]
    key = game_chat_manager.key(game, code)

    # 対局が終わった部屋のチャットは存在しない(履歴が残っていれば念のため消す)
    if room.get('status') == 'finished':
        await websocket.accept()
        await game_chat_manager.clear_history(key)
        try:
            await websocket.send_json({"type": "closed"})
            await websocket.close()
        except Exception:
            pass
        return

    # 対戦者か観戦者か。名前は必ずサーバー側で決める(なりすまし防止のためクライアントからは受け取らない)。
    token = websocket.cookies.get('game_player_token') or websocket.cookies.get('user_bbs_token')
    ident = await _thread_ws_identity(websocket)
    if token and token == room.get('a_token'):
        who = {'uid': f"p:{token}", 'role': 'player', 'side': meta['label_a'],
               'name': html.unescape(room.get('a_name') or '名無しさん')}
    elif token and token == room.get('b_token'):
        who = {'uid': f"p:{token}", 'role': 'player', 'side': meta['label_b'],
               'name': html.unescape(room.get('b_name') or '名無しさん')}
    else:
        name = ident.get('username') or '名無しさん'
        if ident.get('guest') and ident.get('guest_id'):
            name = f"{name} (ID:{str(ident['guest_id'])[:6]})"
        who = {'uid': f"s:{ident['id']}", 'role': 'spectator', 'side': '', 'name': name}
    who['last'] = 0.0

    await game_chat_manager.connect(key, websocket, who)
    try:
        await websocket.send_json({
            "type": "init",
            "history": await game_chat_manager.history(key),
            "me": {"role": who['role'], "name": who['name'], "side": who['side']},
            **game_chat_manager.presence_payload(key),
        })
    except Exception:
        game_chat_manager.disconnect(key, websocket)
        return
    await game_chat_manager.broadcast_presence(key)

    try:
        while True:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=GAME_CHAT_IDLE_TIMEOUT)
            if raw == 'ping':
                await websocket.send_text('{"type":"pong"}')
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if not isinstance(data, dict) or data.get('type') != 'msg':
                continue

            text = ' '.join(str(data.get('text') or '').split())[:GAME_CHAT_MAX_LEN]
            if not text:
                continue
            now = time.monotonic()
            if now - who['last'] < GAME_CHAT_MIN_INTERVAL:
                await websocket.send_json({"type": "error", "message": "少し間をあけて送信してください"})
                continue
            who['last'] = now

            cur = await execute_query(f"SELECT status FROM {meta['table']} WHERE room_code = ? LIMIT 1", [code])
            status = cur[0].get('status') if cur else 'finished'
            if status == 'finished':
                await game_chat_manager.close_room(key)
                break
            if status != 'playing':
                await websocket.send_json({"type": "error", "message": "対戦が始まるとチャットできます"})
                continue

            msg = {
                "type": "msg", "id": uuid.uuid4().hex[:8], "ts": int(time.time()),
                "role": who['role'], "side": who['side'], "name": who['name'],
                "text": filter_ng_words(text),
            }
            await game_chat_manager.add_history(key, msg)
            await game_chat_manager.publish(key, msg)
    except asyncio.TimeoutError:
        try:
            await websocket.close()
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"ゲームチャットWSエラー: {e}")
    finally:
        game_chat_manager.disconnect(key, websocket)
