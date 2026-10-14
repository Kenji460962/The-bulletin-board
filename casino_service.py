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
                 min_bet, max_bet, play, describe_result=None,
                 template=None, rules="", category=None):
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
                    request_id: Optional[str]) -> dict:
    """1ベットを原子的に処理する。全ゲームがここを通る。

    戻り値:
      {'success': bool, 'duplicate': bool, 'detail': dict, 'payout': int,
       'net': int, 'balance': int, 'error': str|None}
    error: 'unknown_game' / 'invalid_bet' / 'insufficient_balance' /
           'daily_limit' / 'cooldown' / 'duplicate_request'
    """
    _check_initialized()

    game = get_game(game_key)
    if not game:
        return {'success': False, 'error': 'unknown_game', 'detail': {}, 'payout': 0,
                'net': 0, 'balance': 0, 'duplicate': False}

    verr = game.validate_bet(bet)
    if verr:
        return {'success': False, 'error': verr, 'detail': {}, 'payout': 0, 'net': 0,
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
            detail, payout = game.play(bet)
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
