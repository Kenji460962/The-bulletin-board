"""
points_service.py
==================

talk-ch のポイント残高・ポイント履歴（元帳）を一元管理するモジュール。

■ 設計方針: 広告機能からの独立
--------------------------------
AdGemのpostback処理や、将来追加されるかもしれない他の付与経路
（ログインボーナス、スレッド投稿ボーナス、管理者による手動付与など）は、
すべてこのモジュールの `add_points()` / `spend_points()` だけを呼び出せばよい。
「ポイントをどう貯める/使わせるか」という各機能固有のロジックと、
「どう安全に残高を増減させ、履歴を記録するか」という共通処理を分離することで、
将来の付与経路の追加・変更が本モジュールにも既存の他機能にも影響しない。

このモジュールはapp_49.py（メインアプリ）を一切importしない。
DB接続の取得方法と排他ロックは起動時に `init()` で外から注入してもらう
（依存性注入）。これにより循環importを避けつつ、既存アプリのDB接続の
仕組み（単一のaiosqlite接続 + asyncio.Lockによる直列化）をそのまま再利用する。

■ 二重付与・同時実行による不整合の防止
--------------------------------------
1. 残高の増減は「1本のUPDATE文」で行い、減算時に残高が0未満にならないか
   という条件までWHERE句に含める:
       UPDATE users SET points = points + ? WHERE id = ? AND points + ? >= 0
   これにより「残高を確認してから更新する」方式につきまとう、
   確認と更新の間に別のリクエストが割り込んでしまうリスクを構造的に排除する。

2. `idempotency_key` を指定すると、同じキーでの呼び出しは実際には
   1回しか反映されない（point_history.idempotency_key にUNIQUE制約）。
   AdGemのpostback再送、二重クリック、ネットワーク再試行によるAPIの
   多重リクエストなど「同じ加算/減算指示が複数回届く」ケース全般に有効。

3. アプリ全体で使っているSQLite接続は1本（単一プロセス・単一コネクション）で、
   すべての書き込みは共有の asyncio.Lock で直列化されている
  （app_49.py の `_db_lock` を参照）。本モジュールはその接続とロックを
   `init()` で受け取り、「残高更新 → 履歴INSERT → commit」を
   ロックを1回だけ取得した区間の中で行う。これにより、他の同時実行中の
   リクエストがこの一連の処理の途中に割り込むことはない。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

_get_conn: Optional[Callable[[], Awaitable[Any]]] = None
_lock: Optional[asyncio.Lock] = None


def init(get_conn: Callable[[], Awaitable[Any]], lock: asyncio.Lock) -> None:
    """呼び出し元（app_49.py）が持つDBコネクション取得関数と排他ロックを登録する。

    アプリ起動時に一度だけ呼び出すこと。例:

        import points_service
        points_service.init(_get_db_conn, _db_lock)

        @app.on_event("startup")
        async def _startup():
            ...
            await points_service.ensure_schema()
    """
    global _get_conn, _lock
    _get_conn = get_conn
    _lock = lock


def _check_initialized() -> None:
    if _get_conn is None or _lock is None:
        raise RuntimeError(
            "points_service.init(get_conn, lock) が呼ばれていません。"
            "アプリ起動時に一度だけ初期化してください。"
        )


async def ensure_schema() -> None:
    """ポイント履歴テーブルを起動時に用意する（冪等）。

    users.points 自体は既存のAdGem連携が用意するカラムをそのまま
    「現在の残高」として使い続ける（残高の二重管理を避けるため）。
    本モジュールが追加するのは、その増減の証跡を残す point_history のみ。
    ALTER TABLEは、users.points がまだ存在しない環境（本モジュール単体導入時）
    への保険であり、既に存在する場合のエラーは無視してよい。
    """
    _check_initialized()
    conn = await _get_conn()

    try:
        await conn.execute("ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS point_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            reason TEXT NOT NULL,
            description TEXT,
            source TEXT NOT NULL DEFAULT 'system',
            idempotency_key TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_point_history_user_id "
        "ON point_history(user_id, id DESC)"
    )
    # idempotency_key が同じ行は1つしか存在できない = 同一操作の二重反映を防ぐ一意制約。
    # NULL/空文字は対象外（idempotency_keyを付けない単発の手動処理などは複数回あってよい）。
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_point_history_idempotency_key "
        "ON point_history(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL AND idempotency_key != ''"
    )
    await conn.commit()


async def get_balance(user_id: int) -> int:
    """現在のポイント残高を返す。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cursor = await conn.execute("SELECT points FROM users WHERE id = ?", [user_id])
        row = await cursor.fetchone()
        await cursor.close()
    return int(row[0]) if row else 0


async def get_history(user_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
    """ポイント履歴を新しい順に返す（一覧ページのページネーション用）。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cursor = await conn.execute(
            "SELECT id, delta, balance_after, reason, description, source, created_at "
            "FROM point_history WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            [user_id, limit, offset]
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


async def count_history(user_id: int) -> int:
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM point_history WHERE user_id = ?", [user_id]
        )
        row = await cursor.fetchone()
        await cursor.close()
    return int(row[0]) if row else 0


async def add_points(
    user_id: int,
    delta: int,
    reason: str,
    description: str = "",
    source: str = "system",
    idempotency_key: Optional[str] = None,
) -> dict:
    """ポイントを増減させ、履歴を1件残す。全ての増減はこの関数を経由すること。

    引数:
        user_id: 対象ユーザーのusers.id
        delta: 正の値で加算、負の値で減算（残高が不足する減算は失敗する）
        reason: 機械可読な理由コード。例: 'adgem_offer', 'admin_grant', 'thread_bonus'
        description: 履歴ページに表示する日本語の説明文
        source: どの機能からの操作かを表す分類タグ。例: 'ad', 'admin', 'system'
                広告機能はここに 'ad' を渡すだけでよく、本モジュール内部の実装
                （テーブル構造やロック方式）には一切依存しない。
        idempotency_key: 同一操作を一意に識別するキー。同じキーで複数回呼んでも
                実際に反映されるのは1回だけ（二重付与防止）。
                例: f"adgem:{request_id}", f"daily_login:{user_id}:{date_str}"

    戻り値:
        {
            'success': bool,      # 反映されたか
            'duplicate': bool,    # idempotency_key重複によりスキップされたか
            'balance': int,       # 処理後（またはスキップ時点）の残高
            'error': str | None,  # 'insufficient_balance' / 'invalid_delta' など
        }
    """
    _check_initialized()

    if not isinstance(delta, int) or delta == 0:
        return {
            'success': False, 'duplicate': False,
            'balance': await get_balance(user_id), 'error': 'invalid_delta',
        }

    conn = await _get_conn()
    async with _lock:
        try:
            # 明示的にトランザクションを開始する。sqlite3/aiosqliteは
            # isolation_level設定によっては「最初のDML文実行時に自動でBEGIN」
            # という暗黙的な挙動に依存することになるが、それだと接続設定が
            # 変わった場合に原子性の保証が崩れうる。ここでは残高更新(UPDATE)と
            # 履歴追加(INSERT)を明示的に1つのトランザクションとして開始し、
            # 最後に必ずcommit/rollbackすることで、「ポイントだけ増えて履歴が
            # 残らない」「履歴だけ残って残高が変わらない」という中間状態が
            # 決して観測されないことを保証する。
            await conn.execute("BEGIN IMMEDIATE")

            # 残高チェックと更新を1本のSQL文で行う。「減算後にマイナスにならない」
            # という条件をWHERE句に含めることで、SELECTしてから判断する方式に
            # つきまとう「チェックと更新の間に別処理が割り込む」余地を作らない。
            cursor = await conn.execute(
                "UPDATE users SET points = points + ? WHERE id = ? AND points + ? >= 0",
                [delta, user_id, delta]
            )
            updated = cursor.rowcount
            await cursor.close()

            if updated == 0:
                # 残高不足、またはuser_id自体が存在しない。
                await conn.rollback()
                current_balance = await _read_balance_nolock(conn, user_id)
                return {
                    'success': False, 'duplicate': False,
                    'balance': current_balance, 'error': 'insufficient_balance',
                }

            new_balance = await _read_balance_nolock(conn, user_id)

            try:
                await conn.execute(
                    "INSERT INTO point_history "
                    "(user_id, delta, balance_after, reason, description, source, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [user_id, delta, new_balance, reason, description, source, idempotency_key or None]
                )
            except aiosqlite.IntegrityError:
                # idempotency_key のUNIQUE制約違反 = 同一操作の二重実行。
                # usersへのUPDATEも含めて丸ごとロールバックし、「何も起きなかった」
                # 状態に戻す（ポイントだけ増えて履歴が残らない、という不整合を防ぐ）。
                await conn.rollback()
                current_balance = await _read_balance_nolock(conn, user_id)
                return {
                    'success': False, 'duplicate': True,
                    'balance': current_balance, 'error': None,
                }

            await conn.commit()
            return {'success': True, 'duplicate': False, 'balance': new_balance, 'error': None}

        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


async def spend_points(
    user_id: int,
    amount: int,
    reason: str,
    description: str = "",
    source: str = "system",
    idempotency_key: Optional[str] = None,
) -> dict:
    """ポイントを消費する（add_pointsのマイナス方向のラッパー）。amountは正の値で指定する。"""
    if not isinstance(amount, int) or amount <= 0:
        return {
            'success': False, 'duplicate': False,
            'balance': await get_balance(user_id), 'error': 'invalid_amount',
        }
    return await add_points(
        user_id, -amount, reason,
        description=description, source=source, idempotency_key=idempotency_key,
    )


async def _read_balance_nolock(conn, user_id: int) -> int:
    """既にロックを保持している呼び出し元専用の内部ヘルパー（再ロックしない）。"""
    cursor = await conn.execute("SELECT points FROM users WHERE id = ?", [user_id])
    row = await cursor.fetchone()
    await cursor.close()
    return int(row[0]) if row else 0
