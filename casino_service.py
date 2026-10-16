"""
casino_service.py
==================

talk-ch のカジノ（ポイント賭け）の共通エンジン。

■ 役割
- 賭け金の引き落とし・残高チェック・1日の損失上限・クールダウン・
  二重実行防止・履歴記録を「全ゲーム共通」で行う。
- 各ゲームは casino_games/ 以下に「結果と配当を返す純粋関数」だけを書けばよい。

■ 設計方針(points_service / market_service と同じ)
- app本体を import しない。DB接続取得関数と排他ロックは init() で注入。
- 残高(users.points)・履歴(point_history)は points_service の台帳を共有。
- 1ベット = 1トランザクション(BEGIN IMMEDIATE)で原子的に処理。
- request_id による冪等キーで二重実行を防ぐ。
- 乱数はサーバー側で生成。クライアントからは一切受け取らない。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

_get_conn: Optional[Callable[[], Awaitable[Any]]] = None
_lock: Optional[asyncio.Lock] = None

# 全ゲーム共通の安全弁
GLOBAL_BET_MAX = 100000           # 1ベットの絶対上限
DAILY_LOSS_LIMIT = 50000          # 1日の純損失上限(全カジノ合計、JST基準)
BET_COOLDOWN_SECONDS = 0.3
LAST_BET_TIMES: dict[int, float] = {}

_GAMES: dict[str, "Game"] = {}


def init(get_conn, lock) -> None:
    global _get_conn, _lock
    _get_conn = get_conn
    _lock = lock


def _check_initialized() -> None:
    if _get_conn is None or _lock is None:
        raise RuntimeError("casino_service.init(get_conn, lock) が呼ばれていません。")


def register_game(game: "Game") -> None:
    _GAMES[game.key] = game


def get_game(key: str) -> Optional["Game"]:
    return _GAMES.get(key)


def all_games() -> list["Game"]:
    return list(_GAMES.values())


class Game:
    """カジノゲーム1つ分の定義。
    play は「賭け金を渡すと (detail_dict, payout) を返す純粋関数」。
    残高の増減・履歴・エスクローは一切関知しない(casino_service がやる)。
    """

    def __init__(self, key, label, icon, description,
                 min_bet, max_bet, play=None, describe_result=None,
                 template=None, rules="", category=None,
                 interactive=False, start=None, resolve=None, public_view=None,
                 accepts_choice=False, validate_choice=None):
        self.key = key
        self.label = label
        self.icon = icon
        self.description = description
        self.min_bet = min_bet
        self.max_bet = max_bet
        self.play = play
        self.describe_result = describe_result or (lambda detail: "")
        self.template = template or f"casino_{key}.html"
        self.rules = rules
        self.category = category
        # --- 2段階で進むゲーム(ビデオポーカー等)用 ---
        # interactive=True のゲームは place_bet ではなく start_interactive / resolve_interactive で進める。
        #   start(bet)                  -> (state, view)        配る。state はサーバー側だけで保持(山札など)
        #   resolve(bet, state, action) -> (detail, payout, view)  プレイヤーの選択で結果を確定。不正な入力は ValueError
        #   public_view(state)          -> view                 再開時にプレイヤーへ見せてよい情報だけを返す
        # --- 賭け方(選択)つきの1回完結ゲーム(ルーレット等)用 ---
        # accepts_choice=True のゲームは、play(bet, choice) が呼ばれる。choice はクライアントから届いた
        # 賭け方(例: 赤/黒/数字)。validate_choice(choice) が False を返したら賭けを受け付けない。
        self.accepts_choice = accepts_choice
        self.validate_choice = validate_choice or (lambda choice: True)
        self.interactive = interactive
        self.start = start
        self.resolve = resolve
        self.public_view = public_view or (lambda state: {})

    def validate_bet(self, bet) -> Optional[str]:
        if not isinstance(bet, int) or isinstance(bet, bool):
            return 'invalid_bet'
        if bet < self.min_bet or bet > self.max_bet:
            return 'invalid_bet'
        if bet > GLOBAL_BET_MAX:
            return 'invalid_bet'
        return None


async def ensure_schema() -> None:
    """カジノ全体で1つの履歴テーブルを共有する。ゲーム固有の結果は detail に JSON で入れる。"""
    _check_initialized()
    conn = await _get_conn()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS casino_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            bet INTEGER NOT NULL,
            payout INTEGER NOT NULL,
            net INTEGER NOT NULL,
            detail TEXT,
            request_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_bets_request_id "
        "ON casino_bets(request_id) WHERE request_id IS NOT NULL AND request_id != ''"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_casino_bets_user ON casino_bets(user_id, id DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_casino_bets_game ON casino_bets(game, id DESC)"
    )
    # 2段階ゲームの進行中データ。山札などはここ(サーバー側)にだけ置き、クライアントへは出さない。
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS casino_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            bet INTEGER NOT NULL,
            state TEXT NOT NULL,
            bet_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # 同じユーザー・同じゲームで「進行中」は1つだけ(二重に配れない)
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_sessions_open "
        "ON casino_sessions(user_id, game) WHERE status = 'open'"
    )
    await conn.commit()


async def _daily_net(user_id: int, conn) -> int:
    """今日(JST)の純損益合計。全カジノ合計で判定する。"""
    cur = await conn.execute(
        "SELECT COALESCE(SUM(net), 0) AS net FROM casino_bets "
        "WHERE user_id = ? AND date(created_at, '+9 hours') = date('now', '+9 hours')",
        [user_id],
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row[0]) if row else 0


async def place_bet(user_id: int, game_key: str, bet: int,
                    request_id: Optional[str], choice: Optional[dict] = None) -> dict:
    """1ベットを原子的に処理する。全ゲームがここを通る。

    戻り値:
      {'success': bool, 'duplicate': bool, 'detail': dict, 'payout': int,
       'net': int, 'balance': int, 'error': str|None}
    error: 'unknown_game' / 'invalid_bet' / 'invalid_choice' / 'insufficient_balance' /
           'daily_limit' / 'cooldown' / 'duplicate_request'
    """
    _check_initialized()

    game = get_game(game_key)
    if not game:
        return {'success': False, 'error': 'unknown_game', 'detail': {}, 'payout': 0,
                'net': 0, 'balance': 0, 'duplicate': False}

    if game.interactive:
        # ビデオポーカーなどは「配る→選ぶ」の2段階。専用の start_interactive を使う。
        return {'success': False, 'error': 'unknown_game', 'detail': {}, 'payout': 0,
                'net': 0, 'balance': 0, 'duplicate': False}

    verr = game.validate_bet(bet)
    if verr:
        return {'success': False, 'error': verr, 'detail': {}, 'payout': 0, 'net': 0,
                'balance': 0, 'duplicate': False}

    if game.accepts_choice and not game.validate_choice(choice):
        return {'success': False, 'error': 'invalid_choice', 'detail': {}, 'payout': 0, 'net': 0,
                'balance': 0, 'duplicate': False}

    now = time.time()
    last = LAST_BET_TIMES.get(user_id)
    if last is not None and now - last < BET_COOLDOWN_SECONDS:
        return {'success': False, 'error': 'cooldown', 'detail': {}, 'payout': 0, 'net': 0,
                'balance': 0, 'duplicate': False}
    LAST_BET_TIMES[user_id] = now

    conn = await _get_conn()
    async with _lock:
        try:
            await conn.execute("BEGIN IMMEDIATE")

            # --- 冪等チェック ---
            if request_id:
                cur = await conn.execute(
                    "SELECT detail, payout, net FROM casino_bets "
                    "WHERE request_id = ? AND user_id = ?",
                    [request_id, user_id],
                )
                prev = await cur.fetchone()
                await cur.close()
                if prev:
                    cur = await conn.execute("SELECT points FROM users WHERE id = ?", [user_id])
                    b = await cur.fetchone()
                    await cur.close()
                    await conn.rollback()
                    return {'success': True, 'duplicate': True,
                            'detail': json.loads(prev[0] or '{}'),
                            'payout': int(prev[1]), 'net': int(prev[2]),
                            'balance': int(b[0]) if b else 0, 'error': None}

            # --- 1日の損失上限チェック ---
            net_today = await _daily_net(user_id, conn)
            if net_today - bet < -DAILY_LOSS_LIMIT:
                await conn.rollback()
                cur = await conn.execute("SELECT points FROM users WHERE id = ?", [user_id])
                b = await cur.fetchone()
                await cur.close()
                return {'success': False, 'error': 'daily_limit', 'detail': {}, 'payout': 0,
                        'net': 0, 'balance': int(b[0]) if b else 0, 'duplicate': False}

            # --- 残高チェック+賭け金消費を1本のSQLで ---
            cur = await conn.execute(
                "UPDATE users SET points = points - ? WHERE id = ? AND points >= ?",
                [bet, user_id, bet],
            )
            if cur.rowcount == 0:
                await cur.close()
                await conn.rollback()
                cur = await conn.execute("SELECT points FROM users WHERE id = ?", [user_id])
                b = await cur.fetchone()
                await cur.close()
                return {'success': False, 'error': 'insufficient_balance', 'detail': {},
                        'payout': 0, 'net': 0, 'balance': int(b[0]) if b else 0, 'duplicate': False}
            await cur.close()

            # --- ゲーム固有ロジック(純粋関数) ---
            detail, payout = game.play(bet, choice) if game.accepts_choice else game.play(bet)
            payout = max(0, int(payout))
            net = payout - bet

            if payout > 0:
                cur = await conn.execute(
                    "UPDATE users SET points = points + ? WHERE id = ?",
                    [payout, user_id],
                )
                await cur.close()

            cur = await conn.execute("SELECT points FROM users WHERE id = ?", [user_id])
            row = await cur.fetchone()
            await cur.close()
            balance = int(row[0]) if row else 0

            await conn.execute(
                "INSERT INTO casino_bets (user_id, game, bet, payout, net, detail, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [user_id, game_key, bet, payout, net,
                 json.dumps(detail, ensure_ascii=False), request_id or None],
            )

            if net != 0:
                desc = game.describe_result(detail) or ""
                full_desc = (f"{game.label}: {desc} / 賭け{bet}pt / 払戻{payout}pt" if desc
                             else f"{game.label}: 賭け{bet}pt / 払戻{payout}pt")
                await conn.execute(
                    "INSERT INTO point_history "
                    "(user_id, delta, balance_after, reason, description, source, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [user_id, net, balance, f"casino_{game_key}", full_desc, "casino",
                     f"casino:{request_id}" if request_id else None],
                )

            await conn.commit()
            return {'success': True, 'duplicate': False, 'detail': detail,
                    'payout': payout, 'net': net, 'balance': balance, 'error': None}

        except aiosqlite.IntegrityError:
            await conn.rollback()
            return {'success': False, 'error': 'duplicate_request', 'detail': {},
                    'payout': 0, 'net': 0, 'balance': 0, 'duplicate': False}
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


# =========================
# 2段階で進むゲーム(ビデオポーカー等)
# =========================
# 流れ:
#   1) start_interactive  : 賭け金を引き落として配る。この時点で casino_bets に「賭け金ぶん負け」として記録する
#                           (途中で離脱しても賭け金は戻らない / 1日の損失上限にも最初から算入される)
#   2) resolve_interactive: プレイヤーの選択(残すカード等)を受けて結果を確定し、払い戻しを加算して
#                           casino_bets を最終結果に更新する
# どちらも1リクエスト=1トランザクション。山札などの秘密情報は casino_sessions にだけ保存する。

def _err(error: str, **extra) -> dict:
    base = {'success': False, 'error': error, 'duplicate': False, 'balance': 0}
    base.update(extra)
    return base


async def _balance_of(conn, user_id: int) -> int:
    cur = await conn.execute("SELECT points FROM users WHERE id = ?", [user_id])
    row = await cur.fetchone()
    await cur.close()
    return int(row[0]) if row else 0


async def get_open_session(user_id: int, game_key: str) -> Optional[dict]:
    """進行中のセッション(あれば)。ページを開き直しても続きから遊べるようにする。"""
    _check_initialized()
    game = get_game(game_key)
    if not game or not game.interactive:
        return None
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT id, bet, state FROM casino_sessions WHERE user_id = ? AND game = ? AND status = 'open'",
            [user_id, game_key],
        )
        row = await cur.fetchone()
        await cur.close()
    if not row:
        return None
    return {'session_id': int(row[0]), 'bet': int(row[1]),
            'view': game.public_view(json.loads(row[2]))}


async def start_interactive(user_id: int, game_key: str, bet: int,
                            request_id: Optional[str]) -> dict:
    """賭け金を引き落として配る。成功すると session_id と、プレイヤーに見せてよい view を返す。
    error: 'unknown_game' / 'invalid_bet' / 'cooldown' / 'insufficient_balance' / 'daily_limit' /
           'pending_session'(進行中あり。session_id と view を添えて返す) / 'duplicate_request'"""
    _check_initialized()
    game = get_game(game_key)
    if not game or not game.interactive or not game.start:
        return _err('unknown_game')
    if game.validate_bet(bet):
        return _err('invalid_bet')

    now = time.time()
    last = LAST_BET_TIMES.get(user_id)
    if last is not None and now - last < BET_COOLDOWN_SECONDS:
        return _err('cooldown')
    LAST_BET_TIMES[user_id] = now

    conn = await _get_conn()
    async with _lock:
        try:
            await conn.execute("BEGIN IMMEDIATE")

            # 進行中のセッションがあれば、新しくは配らず続きから(再開)にする
            cur = await conn.execute(
                "SELECT id, bet, state FROM casino_sessions "
                "WHERE user_id = ? AND game = ? AND status = 'open'", [user_id, game_key])
            open_row = await cur.fetchone()
            await cur.close()
            if open_row:
                balance = await _balance_of(conn, user_id)
                await conn.rollback()
                return _err('pending_session', session_id=int(open_row[0]), bet=int(open_row[1]),
                            view=game.public_view(json.loads(open_row[2])), balance=balance)

            # 二重送信: 同じ request_id が処理済みなら新しく配らない
            if request_id:
                cur = await conn.execute(
                    "SELECT 1 FROM casino_bets WHERE request_id = ? AND user_id = ?", [request_id, user_id])
                dup = await cur.fetchone()
                await cur.close()
                if dup:
                    balance = await _balance_of(conn, user_id)
                    await conn.rollback()
                    return _err('duplicate_request', balance=balance)

            net_today = await _daily_net(user_id, conn)
            if net_today - bet < -DAILY_LOSS_LIMIT:
                balance = await _balance_of(conn, user_id)
                await conn.rollback()
                return _err('daily_limit', balance=balance)

            cur = await conn.execute(
                "UPDATE users SET points = points - ? WHERE id = ? AND points >= ?",
                [bet, user_id, bet],
            )
            debited = cur.rowcount
            await cur.close()
            if debited == 0:
                balance = await _balance_of(conn, user_id)
                await conn.rollback()
                return _err('insufficient_balance', balance=balance)

            state, view = game.start(bet)
            balance = await _balance_of(conn, user_id)

            # 賭け金ぶんの負けとして先に記録(最終結果は resolve で更新する)
            cur = await conn.execute(
                "INSERT INTO casino_bets (user_id, game, bet, payout, net, detail, request_id) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                [user_id, game_key, bet, -bet, json.dumps({'phase': 'dealt'}, ensure_ascii=False),
                 request_id or None],
            )
            bet_id = cur.lastrowid
            await cur.close()

            await conn.execute(
                "INSERT INTO point_history "
                "(user_id, delta, balance_after, reason, description, source, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [user_id, -bet, balance, f"casino_{game_key}_bet", f"{game.label}: 賭け{bet}pt", "casino",
                 f"casino:{request_id}:bet" if request_id else None],
            )
            cur = await conn.execute(
                "INSERT INTO casino_sessions (user_id, game, bet, state, bet_id) VALUES (?, ?, ?, ?, ?)",
                [user_id, game_key, bet, json.dumps(state, ensure_ascii=False), bet_id],
            )
            session_id = cur.lastrowid
            await cur.close()

            await conn.commit()
            return {'success': True, 'error': None, 'duplicate': False,
                    'session_id': int(session_id), 'bet': bet, 'view': view, 'balance': balance}

        except aiosqlite.IntegrityError:
            await conn.rollback()
            return _err('pending_session')
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


async def resolve_interactive(user_id: int, game_key: str, session_id: int, action: dict) -> dict:
    """プレイヤーの選択を受けて結果を確定する。
    error: 'unknown_game' / 'no_session'(存在しない・自分のものでない・すでに確定済み) / 'invalid_action'"""
    _check_initialized()
    game = get_game(game_key)
    if not game or not game.interactive or not game.resolve:
        return _err('unknown_game')

    conn = await _get_conn()
    async with _lock:
        try:
            await conn.execute("BEGIN IMMEDIATE")

            cur = await conn.execute(
                "SELECT bet, state, bet_id FROM casino_sessions "
                "WHERE id = ? AND user_id = ? AND game = ? AND status = 'open'",
                [session_id, user_id, game_key],
            )
            row = await cur.fetchone()
            await cur.close()
            if not row:
                balance = await _balance_of(conn, user_id)
                await conn.rollback()
                return _err('no_session', balance=balance)
            bet, state_json, bet_id = int(row[0]), row[1], int(row[2])

            try:
                detail, payout, view = game.resolve(bet, json.loads(state_json), action or {})
            except ValueError:
                balance = await _balance_of(conn, user_id)
                await conn.rollback()
                return _err('invalid_action', balance=balance)
            payout = max(0, int(payout))
            net = payout - bet

            # 確定済みにする(二重実行を防ぐ関門)。更新できなければ他のリクエストが先に確定した。
            cur = await conn.execute(
                "UPDATE casino_sessions SET status = 'closed' WHERE id = ? AND status = 'open'", [session_id])
            closed = cur.rowcount
            await cur.close()
            if closed == 0:
                balance = await _balance_of(conn, user_id)
                await conn.rollback()
                return _err('no_session', balance=balance)

            if payout > 0:
                cur = await conn.execute(
                    "UPDATE users SET points = points + ? WHERE id = ?", [payout, user_id])
                await cur.close()
            balance = await _balance_of(conn, user_id)

            await conn.execute(
                "UPDATE casino_bets SET payout = ?, net = ?, detail = ? WHERE id = ?",
                [payout, net, json.dumps(detail, ensure_ascii=False), bet_id],
            )
            if payout > 0:
                desc = game.describe_result(detail) or ""
                await conn.execute(
                    "INSERT INTO point_history "
                    "(user_id, delta, balance_after, reason, description, source, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [user_id, payout, balance, f"casino_{game_key}_win",
                     f"{game.label}: {desc} / 払戻{payout}pt" if desc else f"{game.label}: 払戻{payout}pt",
                     "casino", f"casino_session:{session_id}:payout"],
                )

            await conn.commit()
            return {'success': True, 'error': None, 'duplicate': False, 'detail': detail,
                    'view': view, 'bet': bet, 'payout': payout, 'net': net, 'balance': balance}

        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


async def recent_bets(user_id: int, game: Optional[str] = None, limit: int = 20) -> list[dict]:
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        if game:
            cur = await conn.execute(
                "SELECT game, bet, payout, net, detail, created_at FROM casino_bets "
                "WHERE user_id = ? AND game = ? ORDER BY id DESC LIMIT ?",
                [user_id, game, limit],
            )
        else:
            cur = await conn.execute(
                "SELECT game, bet, payout, net, detail, created_at FROM casino_bets "
                "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                [user_id, limit],
            )
        rows = await cur.fetchall()
        await cur.close()
    return [dict(r) for r in rows]


async def stats_today(user_id: int) -> dict:
    """今日の成績(全ゲーム合計)。ハブに表示する。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT COUNT(*) AS spins, COALESCE(SUM(bet),0) AS total_bet, "
            "       COALESCE(SUM(payout),0) AS total_payout, COALESCE(SUM(net),0) AS net "
            "FROM casino_bets "
            "WHERE user_id = ? AND date(created_at, '+9 hours') = date('now', '+9 hours')",
            [user_id],
        )
        row = await cur.fetchone()
        await cur.close()
    return dict(row) if row else {'spins': 0, 'total_bet': 0, 'total_payout': 0, 'net': 0}
