"""
market_service.py
==================

talk-ch「ポイント取引所」の商品・購入・評価を一元管理するモジュール。

■ 設計方針(points_service.py と同じ)
------------------------------------
- このモジュールは app 本体(app_52.py)を一切 import しない。
  DB接続の取得方法と排他ロックは起動時に `init()` で外から注入してもらう
  (依存性注入)。循環importを避けつつ、既存アプリの単一aiosqlite接続 +
  asyncio.Lock による直列化の仕組みをそのまま再利用する。
- ポイント残高(users.points)と履歴(point_history)は points_service が
  既に用意している台帳をそのまま共有する(残高の二重管理を避けるため)。

■ 二重購入・ポイント二重消費・URL漏洩の防止
--------------------------------------------
1. 購入処理は1つのトランザクション(BEGIN IMMEDIATE)の中で
   「商品が有効か」「自分の出品でないか」「未購入か」「残高は足りるか」を
   すべて判定し、減算は
       UPDATE users SET points = points - ? WHERE id = ? AND points >= ?
   の1本のSQLで残高条件まで同時に判定する(確認と更新の間に別リクエストが
   割り込む余地を構造的に排除)。
2. market_purchases(item_id, buyer_id) に UNIQUE 制約。
   同一ユーザーの二重購入は制約違反となり丸ごとロールバックされる。
3. point_history.idempotency_key に market:buy:{item_id}:{buyer_id} 等の
   一意キーを付けて記録する。既存のUNIQUE制約により、ネットワーク再送・
   二重クリック等で購入処理が多重実行されてもポイントの二重消費/二重付与は
   起きない。
4. Webプロキシ商品のURL(secret_url)は、一覧・詳細の通常クエリでは
   一切SELECTしない。購入者本人・出品者本人・管理者の場合だけ、
   権限確認後に別クエリで取得する(SQLインジェクション耐性のため
   全クエリはプレースホルダ使用)。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

_get_conn: Optional[Callable[[], Awaitable[Any]]] = None
_lock: Optional[asyncio.Lock] = None

OFFICIAL_SELLER_NAME = "Talk-ch公式"
ITEM_TYPES = ("font", "wallpaper", "proxy", "other")
ITEM_TYPE_LABELS = {
    "font": "フォント",
    "wallpaper": "壁紙",
    "proxy": "Webプロキシ",
    "other": "その他",
}


def init(get_conn: Callable[[], Awaitable[Any]], lock: asyncio.Lock) -> None:
    """アプリ起動時に一度だけ呼び出すこと。例:

        import market_service
        market_service.init(_get_db_conn, _db_lock)
    """
    global _get_conn, _lock
    _get_conn = get_conn
    _lock = lock


def _check_initialized() -> None:
    if _get_conn is None or _lock is None:
        raise RuntimeError(
            "market_service.init(get_conn, lock) が呼ばれていません。"
        )


async def ensure_schema() -> None:
    """取引所用テーブルを起動時に用意する(冪等)。既存テーブルには触れない。"""
    _check_initialized()
    conn = await _get_conn()

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS market_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_user_id INTEGER,              -- 公式商品はNULL(管理者個人を出品者にしない)
            is_official INTEGER NOT NULL DEFAULT 0,
            item_type TEXT NOT NULL,             -- font / wallpaper / proxy / other
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price INTEGER NOT NULL,
            image_url TEXT,
            secret_url TEXT,                     -- proxy商品のURL(購入者以外には返さない)
            status TEXT NOT NULL DEFAULT 'active',  -- active / removed(論理削除)
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_items_status "
        "ON market_items(status, is_official, id DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_items_seller "
        "ON market_items(seller_user_id, status)"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS market_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            price_paid INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # (item_id, buyer_id) の一意制約 = 同一ユーザーの二重購入を構造的に防止
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_market_purchases_unique "
        "ON market_purchases(item_id, buyer_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_purchases_buyer "
        "ON market_purchases(buyer_id, id DESC)"
    )

    # 壁紙・フォントの「適用」状態を保存する users のカラム。
    # 適用中の商品(= market_items.id)への参照を保持する。ALTER TABLE は
    # 既にカラムがある場合に失敗するが、それは「追加済み」を意味するので無視する。
    for ddl in (
        "ALTER TABLE users ADD COLUMN equipped_wallpaper_item_id INTEGER",
        "ALTER TABLE users ADD COLUMN equipped_font_item_id INTEGER",
        # フォント商品のフォントファイル(woff2等)のURL。image_url はプレビュー画像、
        # asset_url がフォントファイル本体。購入者のみが利用できる。
        "ALTER TABLE market_items ADD COLUMN asset_url TEXT",
    ):
        try:
            await conn.execute(ddl)
        except Exception:
            pass

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS market_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,             -- 1..5
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # 1購入者につき1商品1レビュー(更新はUPSERTで行う)
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_market_reviews_unique "
        "ON market_reviews(item_id, buyer_id)"
    )

    await conn.commit()


def _with_seller_name(row: dict) -> dict:
    """LEFT JOIN の結果に表示用の出品者名を付ける。公式商品は 'Talk-ch公式'。"""
    if row.get("is_official"):
        row["seller_name"] = OFFICIAL_SELLER_NAME
        row["seller_public_id"] = None
    else:
        row["seller_name"] = row.get("seller_username") or "不明なユーザー"
    return row


# =========================
# 商品 CRUD
# =========================

async def create_item(
    seller_user_id: Optional[int],
    is_official: bool,
    item_type: str,
    title: str,
    description: str,
    price: int,
    image_url: Optional[str] = None,
    secret_url: Optional[str] = None,
    asset_url: Optional[str] = None,
) -> int:
    """商品を登録してitem_idを返す。公式商品は seller_user_id=None を渡すこと。
    asset_url はフォント商品のフォントファイルURLを想定。"""
    _check_initialized()
    if item_type not in ITEM_TYPES:
        raise ValueError("invalid item_type")
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "INSERT INTO market_items "
            "(seller_user_id, is_official, item_type, title, description, price, image_url, secret_url, asset_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [seller_user_id, 1 if is_official else 0, item_type,
             title, description, price, image_url, secret_url, asset_url],
        )
        item_id = cur.lastrowid
        await cur.close()
        await conn.commit()
    return int(item_id)


async def update_item(
    item_id: int,
    title: str,
    description: str,
    price: int,
    image_url: Optional[str] = None,
    secret_url: Optional[str] = None,
    clear_secret: bool = False,
    asset_url: Optional[str] = None,
) -> None:
    """商品情報を更新する(image_url/secret_url/asset_url は None なら変更しない)。"""
    _check_initialized()
    conn = await _get_conn()
    sets = ["title = ?", "description = ?", "price = ?", "updated_at = datetime('now')"]
    params: list = [title, description, price]
    if image_url is not None:
        sets.append("image_url = ?")
        params.append(image_url)
    if clear_secret:
        sets.append("secret_url = NULL")
    elif secret_url is not None:
        sets.append("secret_url = ?")
        params.append(secret_url)
    if asset_url is not None:
        sets.append("asset_url = ?")
        params.append(asset_url)
    params.append(item_id)
    async with _lock:
        await conn.execute(f"UPDATE market_items SET {', '.join(sets)} WHERE id = ?", params)
        await conn.commit()


async def set_status(item_id: int, status: str) -> None:
    """商品ステータスを変更する。削除は status='removed' の論理削除で行い、
    購入済みユーザーのインベントリ・ポイント履歴との整合性を保つ。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        await conn.execute(
            "UPDATE market_items SET status = ?, updated_at = datetime('now') WHERE id = ?",
            [status, item_id],
        )
        await conn.commit()


async def get_item(item_id: int) -> Optional[dict]:
    """商品を1件取得する。secret_url は絶対に含まない(漏洩防止)。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT i.id, i.seller_user_id, i.is_official, i.item_type, i.title, "
            "       i.description, i.price, i.image_url, i.status, i.created_at, "
            "       u.username AS seller_username, u.public_id AS seller_public_id "
            "FROM market_items i LEFT JOIN users u ON u.id = i.seller_user_id "
            "WHERE i.id = ?",
            [item_id],
        )
        row = await cur.fetchone()
        await cur.close()
    if not row:
        return None
    return _with_seller_name(dict(row))


async def get_item_full(item_id: int) -> Optional[dict]:
    """secret_url 込みで取得する。出品者本人・管理者による編集画面など、
    呼び出し側で必ず権限確認を行ってから使うこと。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT i.*, u.username AS seller_username, u.public_id AS seller_public_id "
            "FROM market_items i LEFT JOIN users u ON u.id = i.seller_user_id "
            "WHERE i.id = ?",
            [item_id],
        )
        row = await cur.fetchone()
        await cur.close()
    if not row:
        return None
    return _with_seller_name(dict(row))


async def list_items(item_type: Optional[str] = None, limit: int = 60, offset: int = 0) -> list[dict]:
    """出品中の商品一覧。secret_url は取得しない。公式商品を先頭に表示する。"""
    _check_initialized()
    conn = await _get_conn()
    sql = (
        "SELECT i.id, i.is_official, i.item_type, i.title, i.description, i.price, "
        "       i.image_url, i.created_at, i.seller_user_id, "
        "       u.username AS seller_username, u.public_id AS seller_public_id "
        "FROM market_items i LEFT JOIN users u ON u.id = i.seller_user_id "
        "WHERE i.status = 'active'"
    )
    params: list = []
    if item_type in ITEM_TYPES:
        sql += " AND i.item_type = ?"
        params.append(item_type)
    sql += " ORDER BY i.is_official DESC, i.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    async with _lock:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
    return [_with_seller_name(dict(r)) for r in rows]


async def shop_items(seller_user_id: int, limit: int = 100) -> list[dict]:
    """ユーザーショップページ用: 特定出品者の出品中商品。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT id, is_official, item_type, title, description, price, image_url, created_at "
            "FROM market_items WHERE seller_user_id = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT ?",
            [seller_user_id, limit],
        )
        rows = await cur.fetchall()
        await cur.close()
    return [dict(r) for r in rows]


# =========================
# 購入(最重要: 原子性・冪等性)
# =========================

async def purchase_item(item_id: int, buyer_id: int) -> dict:
    """商品を購入する。以下を1トランザクションで原子的に行う:

      1. 商品が有効(active)か
      2. 自分の出品でないか
      3. 未購入か(market_purchases の UNIQUE 制約)
      4. 残高が足りるか(条件付きUPDATE 1本で減算)
      5. 出品者への売上加算(公式商品は seller 不在のため加算なし=運営回収)
      6. 購入レコード・ポイント履歴(冪等キー付き)の記録

    戻り値: {'success': bool, 'error': str|None, 'price': int, 'balance': int}
    error: 'not_found' / 'own_item' / 'already_purchased' /
           'insufficient_balance' / 'server_error'
    """
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        try:
            await conn.execute("BEGIN IMMEDIATE")

            cur = await conn.execute(
                "SELECT id, seller_user_id, is_official, title, price, status "
                "FROM market_items WHERE id = ?",
                [item_id],
            )
            item = await cur.fetchone()
            await cur.close()
            if not item or item["status"] != "active":
                await conn.rollback()
                return {"success": False, "error": "not_found", "price": 0, "balance": 0}

            seller_id = item["seller_user_id"]
            if seller_id is not None and int(seller_id) == int(buyer_id):
                await conn.rollback()
                return {"success": False, "error": "own_item", "price": 0, "balance": 0}

            price = int(item["price"])

            # 残高チェックと減算を1本のSQLで行う(points_service と同じ方式)。
            cur = await conn.execute(
                "UPDATE users SET points = points - ? WHERE id = ? AND points >= ?",
                [price, buyer_id, price],
            )
            if cur.rowcount == 0:
                await cur.close()
                await conn.rollback()
                return {"success": False, "error": "insufficient_balance", "price": price, "balance": 0}
            await cur.close()

            # 出品者へ売上を加算(公式商品は出品者がいないのでスキップ)。
            if seller_id is not None:
                cur = await conn.execute(
                    "UPDATE users SET points = points + ? WHERE id = ?",
                    [price, seller_id],
                )
                await cur.close()

            # 購入レコード。(item_id, buyer_id) UNIQUE 制約により二重購入はここで失敗。
            try:
                cur = await conn.execute(
                    "INSERT INTO market_purchases (item_id, buyer_id, price_paid) VALUES (?, ?, ?)",
                    [item_id, buyer_id, price],
                )
                await cur.close()
            except aiosqlite.IntegrityError:
                await conn.rollback()
                return {"success": False, "error": "already_purchased", "price": price, "balance": 0}

            # ポイント履歴(既存 point_history テーブルに冪等キー付きで記録)。
            cur = await conn.execute("SELECT points FROM users WHERE id = ?", [buyer_id])
            row = await cur.fetchone()
            await cur.close()
            buyer_balance = int(row[0]) if row else 0
            await conn.execute(
                "INSERT INTO point_history "
                "(user_id, delta, balance_after, reason, description, source, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [buyer_id, -price, buyer_balance, "market_purchase",
                 f"取引所で購入: {item['title']}", "market",
                 f"market:buy:{item_id}:{buyer_id}"],
            )
            if seller_id is not None:
                cur = await conn.execute("SELECT points FROM users WHERE id = ?", [seller_id])
                row = await cur.fetchone()
                await cur.close()
                seller_balance = int(row[0]) if row else 0
                await conn.execute(
                    "INSERT INTO point_history "
                    "(user_id, delta, balance_after, reason, description, source, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [seller_id, price, seller_balance, "market_sale",
                     f"取引所で売却: {item['title']}", "market",
                     f"market:sell:{item_id}:{buyer_id}"],
                )

            await conn.commit()
            return {"success": True, "error": None, "price": price, "balance": buyer_balance}

        except aiosqlite.IntegrityError as e:
            # IntegrityError は UNIQUE だけでなく NOT NULL 等でも発生するため、
            # 原因を必ずログに残し、本当に購入済みの場合だけ already_purchased を返す。
            # (購入レコードが無いのに「購入済み」と出る誤表示を防ぐ)
            print(f"market purchase IntegrityError (item={item_id}, buyer={buyer_id}): {e}")
            try:
                await conn.rollback()
            except Exception:
                pass
            actually_purchased = False
            try:
                cur = await conn.execute(
                    "SELECT 1 FROM market_purchases WHERE item_id = ? AND buyer_id = ? LIMIT 1",
                    [item_id, buyer_id],
                )
                actually_purchased = (await cur.fetchone()) is not None
                await cur.close()
            except Exception:
                pass
            return {
                "success": False,
                "error": "already_purchased" if actually_purchased else "server_error",
                "price": 0, "balance": 0,
            }
        except Exception as e:
            try:
                await conn.rollback()
            except Exception:
                pass
            print(f"market purchase error: {e}")
            return {"success": False, "error": "server_error", "price": 0, "balance": 0}


async def has_purchased(item_id: int, buyer_id: int) -> bool:
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT 1 FROM market_purchases WHERE item_id = ? AND buyer_id = ? LIMIT 1",
            [item_id, buyer_id],
        )
        row = await cur.fetchone()
        await cur.close()
    return row is not None


async def get_item_for_view(item_id: int, viewer_id: Optional[int], viewer_is_admin: bool) -> Optional[dict]:
    """詳細ページ用。secret_url は購入者/出品者/管理者の場合のみ付与する。"""
    item = await get_item(item_id)
    if not item:
        return None
    purchased = False
    if viewer_id:
        purchased = await has_purchased(item_id, viewer_id)
    item["viewer_has_purchased"] = purchased
    is_owner = bool(viewer_id) and item.get("seller_user_id") == viewer_id
    if item["item_type"] == "proxy" and (purchased or is_owner or viewer_is_admin):
        # 権限確認後にだけ secret_url を別クエリで取得する
        full = await get_item_full(item_id)
        item["secret_url"] = full.get("secret_url") if full else None
    else:
        item["secret_url"] = None
    return item


async def get_inventory(buyer_id: int) -> list[dict]:
    """購入済み商品(インベントリ)。購入者本人なので secret_url を含めてよい。
    出品者が商品を削除(論理削除)した後も購入者は中身を参照できる。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT p.id AS purchase_id, p.price_paid, p.created_at AS purchased_at, "
            "       i.id AS item_id, i.title, i.item_type, i.image_url, i.secret_url, "
            "       i.asset_url, i.is_official, i.status, "
            "       u.username AS seller_username, u.public_id AS seller_public_id "
            "FROM market_purchases p "
            "JOIN market_items i ON i.id = p.item_id "
            "LEFT JOIN users u ON u.id = i.seller_user_id "
            "WHERE p.buyer_id = ? ORDER BY p.id DESC",
            [buyer_id],
        )
        rows = await cur.fetchall()
        await cur.close()
    return [_with_seller_name(dict(r)) for r in rows]


# =========================
# 評価(レビュー)
# =========================

async def upsert_review(item_id: int, buyer_id: int, rating: int, comment: str) -> None:
    """レビューを登録/更新する。呼び出し側で「購入済み」「公式商品でない」
    「自分の出品でない」ことを必ず確認してから呼ぶこと。"""
    _check_initialized()
    if not (1 <= int(rating) <= 5):
        raise ValueError("rating must be 1..5")
    conn = await _get_conn()
    async with _lock:
        await conn.execute(
            "INSERT INTO market_reviews (item_id, buyer_id, rating, comment) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(item_id, buyer_id) DO UPDATE SET "
            "rating = excluded.rating, comment = excluded.comment, created_at = datetime('now')",
            [item_id, buyer_id, int(rating), comment],
        )
        await conn.commit()


async def get_reviews(item_id: int, limit: int = 50) -> list[dict]:
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT r.rating, r.comment, r.created_at, u.username, u.public_id "
            "FROM market_reviews r LEFT JOIN users u ON u.id = r.buyer_id "
            "WHERE r.item_id = ? ORDER BY r.id DESC LIMIT ?",
            [item_id, limit],
        )
        rows = await cur.fetchall()
        await cur.close()
    return [dict(r) for r in rows]


async def get_item_rating_summary(item_id: int) -> dict:
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT AVG(rating) AS avg_rating, COUNT(*) AS cnt FROM market_reviews WHERE item_id = ?",
            [item_id],
        )
        row = await cur.fetchone()
        await cur.close()
    avg = round(float(row["avg_rating"]), 1) if row and row["avg_rating"] is not None else None
    return {"avg": avg, "count": int(row["cnt"]) if row else 0}


async def seller_rating_summary(seller_user_id: int) -> dict:
    """ショップページ用: 出品者の全商品に対する平均評価。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT AVG(r.rating) AS avg_rating, COUNT(*) AS cnt "
            "FROM market_reviews r JOIN market_items i ON i.id = r.item_id "
            "WHERE i.seller_user_id = ?",
            [seller_user_id],
        )
        row = await cur.fetchone()
        await cur.close()
    avg = round(float(row["avg_rating"]), 1) if row and row["avg_rating"] is not None else None
    return {"avg": avg, "count": int(row["cnt"]) if row else 0}


# =========================
# 壁紙・フォントの「適用」(装備)
# =========================
#
# 設計メモ
# --------
# - 購入済みの壁紙/フォント商品を、自分のアカウント表示(index/thread)に
#   反映させるための機能。
# - users.equipped_wallpaper_item_id / equipped_font_item_id に
#   適用中の market_items.id を保存する(0/NULL = 未適用)。
# - フォント商品のフォント自体は外部フォントURL(フォント商品の image_url を
#   そのままフォントファイルURLとして使う運用。woff2/woff/ttf/otf)を
#   @font-face で読み込む。購入者本人の画面にだけ配信される。
# - 適用/解除は「購入済みか」の確認とUPDATEを同一ロック内で行い、
#   購入記録の無い商品や他人の未購入商品は適用できない。

_EQUIP_COLUMNS = {
    "wallpaper": "equipped_wallpaper_item_id",
    "font": "equipped_font_item_id",
}


def _equip_column(item_type: str) -> str:
    col = _EQUIP_COLUMNS.get(item_type)
    if not col:
        raise ValueError("wallpaper / font 以外は適用できません")
    return col


async def apply_item(item_id: int, buyer_id: int) -> dict:
    """購入済みの壁紙/フォントを自分のアカウントに適用する。

    戻り値: {'success': bool, 'error': str|None}
    error: 'not_purchased' / 'not_applicable' / 'server_error'
    """
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT i.item_type, i.status FROM market_purchases p "
            "JOIN market_items i ON i.id = p.item_id "
            "WHERE p.item_id = ? AND p.buyer_id = ?",
            [item_id, buyer_id],
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return {"success": False, "error": "not_purchased"}
        if row["item_type"] not in _EQUIP_COLUMNS:
            return {"success": False, "error": "not_applicable"}
        col = _EQUIP_COLUMNS[row["item_type"]]
        await conn.execute(
            f"UPDATE users SET {col} = ? WHERE id = ?",
            [item_id, buyer_id],
        )
        await conn.commit()
    return {"success": True, "error": None}


async def unequip_item(item_type: str, user_id: int) -> None:
    """適用を解除する(未適用なら何もしない)。"""
    _check_initialized()
    col = _equip_column(item_type)
    conn = await _get_conn()
    async with _lock:
        await conn.execute(
            f"UPDATE users SET {col} = NULL WHERE id = ?",
            [user_id],
        )
        await conn.commit()


async def get_equipped(user_id: int) -> dict:
    """適用中の壁紙/フォントを取得する。

    戻り値:
      {
        'wallpaper': {'item_id': int, 'image_url': str|None} | None,
        'font':      {'item_id': int, 'font_url': str|None, 'title': str} | None,
      }
    """
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT equipped_wallpaper_item_id AS wp, equipped_font_item_id AS ft "
            "FROM users WHERE id = ?",
            [user_id],
        )
        row = await cur.fetchone()
        await cur.close()
        result = {"wallpaper": None, "font": None}
        if not row:
            return result
        if row["wp"]:
            cur = await conn.execute(
                "SELECT id, title, image_url FROM market_items WHERE id = ?",
                [row["wp"]],
            )
            item = await cur.fetchone()
            await cur.close()
            if item:
                result["wallpaper"] = {
                    "item_id": item["id"], "title": item["title"],
                    "image_url": item["image_url"],
                }
        if row["ft"]:
            cur = await conn.execute(
                "SELECT id, title, asset_url FROM market_items WHERE id = ?",
                [row["ft"]],
            )
            item = await cur.fetchone()
            await cur.close()
            if item:
                # asset_url にフォントファイル(woff2等)のURLが入る
                result["font"] = {
                    "item_id": item["id"], "title": item["title"],
                    "font_url": item["asset_url"],
                }
    return result


async def get_equipped_fonts_by_public_ids(public_ids) -> dict:
    """レス投稿者(users.public_id)ごとに「適用中のフォント」をまとめて解決する。

    スレッド画面のレス一覧で「購入した人のレスだけそのフォントで表示する」ために使う。
    レス1件ごとに問い合わせるとN+1になるため、対象の投稿者全員分を1本の
    SELECTでまとめて引く。

    戻り値: {public_id: {'item_id': int, 'title': str, 'font_url': str}}
      - 適用フォントが無い投稿者は含まれない(呼び出し側は未適用として扱う)。
      - フォントファイル(asset_url)を持たない商品、既に消えた商品も含まれない。
      - 出品者が商品を削除(論理削除)しても、購入済みの適用は維持したいので
        status は判定しない(購入記録が残っている限り適用を続けられる)。
    """
    _check_initialized()
    ids = [str(p) for p in (public_ids or []) if p]
    if not ids:
        return {}
    conn = await _get_conn()
    placeholders = ",".join("?" for _ in ids)
    sql = (
        "SELECT u.public_id AS public_id, i.id AS item_id, i.title AS title, "
        "       i.asset_url AS asset_url "
        "FROM users u JOIN market_items i ON i.id = u.equipped_font_item_id "
        f"WHERE u.public_id IN ({placeholders}) "
        "  AND u.equipped_font_item_id IS NOT NULL "
        "  AND i.asset_url IS NOT NULL AND i.asset_url != ''"
    )
    async with _lock:
        cur = await conn.execute(sql, ids)
        rows = await cur.fetchall()
        await cur.close()
    return {
        r["public_id"]: {
            "item_id": int(r["item_id"]),
            "title": r["title"],
            "font_url": r["asset_url"],
        }
        for r in rows
    }


async def reviews_for_seller(seller_user_id: int, limit: int = 20) -> list[dict]:
    """ショップページ用: 出品者が受けたレビュー一覧(商品名つき)。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cur = await conn.execute(
            "SELECT r.rating, r.comment, r.created_at, i.title AS item_title, i.id AS item_id, "
            "       u.username, u.public_id "
            "FROM market_reviews r "
            "JOIN market_items i ON i.id = r.item_id "
            "LEFT JOIN users u ON u.id = r.buyer_id "
            "WHERE i.seller_user_id = ? ORDER BY r.id DESC LIMIT ?",
            [seller_user_id, limit],
        )
        rows = await cur.fetchall()
        await cur.close()
    return [dict(r) for r in rows]
