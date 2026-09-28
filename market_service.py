"""
market_service.py
=================

talk-ch「ポイント取引所」のデータ層。

■ 設計方針
----------
- ポイントの増減は必ず points_service.add_points()/spend_points() 経由で行い、
  users.points を直接触らない（points_service と同じ依存性注入方式で
  DB接続と排他ロックを init() で受け取る）。
- 公式商品は seller_id = NULL / is_official = 1 の行として保存する。
  adminアカウントは出品者にならない（CHECK制約でDB側でも保証）。
- 商品URL・素材URL（asset_url）は購入者と出品者本人にしか返さない。
  一覧・詳細用の関数はSELECT対象から asset_url を外してあり、
  「テンプレートに渡してしまって漏れる」事故が構造的に起きない。

■ 二重購入・二重消費の防止
--------------------------
1. market_purchases に UNIQUE(item_id, buyer_id) → 同じ商品は1人1回しか買えない。
2. 支払いは idempotency_key = "market_buy:{item_id}:{buyer_id}" の spend_points 1回のみ。
   連打・再送・同時リクエストでも実際に引かれるのは1回。
3. 「支払い済みだが購入記録が無い」中間状態（プロセス落ち等）は、同じ購入操作を
   再実行すると台帳(idempotency)側が重複を返すので、購入記録だけを補完して完了させる。
4. 出品者への入金も idempotency_key = "market_sale:{item_id}:{buyer_id}"。
   入金漏れは seller_credited フラグ + reconcile_pending_credits() で再実行できる。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiosqlite

import points_service

_get_conn: Optional[Callable[[], Awaitable[Any]]] = None
_lock: Optional[asyncio.Lock] = None

OFFICIAL_SELLER_NAME = 'Talk-ch公式'
KIND_LABELS = {'font': 'フォント', 'wallpaper': '壁紙', 'proxy': 'Webプロキシ', 'other': 'その他'}
OFFICIAL_KINDS = ('font', 'wallpaper')
USER_KINDS = ('wallpaper', 'proxy', 'other')
EQUIPPABLE_KINDS = ('font', 'wallpaper')

MAX_PRICE = 1_000_000
MAX_TITLE = 60
MAX_DESC = 1000
MAX_COMMENT = 500
MAX_ASSET_URL = 500
MAX_ACTIVE_LISTINGS_PER_USER = 30


class MarketError(Exception):
    """入力検証・権限エラー。code は画面/APIに返す機械可読コード。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


ERROR_MESSAGES = {
    'invalid_kind': '商品の種類が正しくありません。',
    'invalid_title': f'タイトルは1〜{MAX_TITLE}文字で入力してください。',
    'invalid_description': f'説明は{MAX_DESC}文字以内で入力してください。',
    'invalid_price': f'価格は1〜{MAX_PRICE:,}ptの整数で入力してください。',
    'invalid_asset': '商品データ（URLまたはファイル）が正しくありません。',
    'invalid_proxy_url': 'URLは http:// または https:// で始まる500文字以内で入力してください。',
    'listing_limit': f'出品できるのは同時に{MAX_ACTIVE_LISTINGS_PER_USER}件までです。',
    'item_unavailable': 'この商品は現在購入できません。',
    'own_item': '自分の出品は購入できません。',
    'already_purchased': 'この商品は購入済みです。',
    'insufficient_balance': 'ポイントが不足しています。',
    'not_purchased': '購入者のみ評価できます。',
    'official_not_rateable': '公式商品は評価できません。',
    'invalid_rating': '評価は★1〜5で選択してください。',
    'invalid_comment': f'コメントは{MAX_COMMENT}文字以内で入力してください。',
    'not_owned': '購入済みの商品のみ操作できます。',
    'not_equippable': 'この商品は適用できません。',
    'forbidden': 'この操作を行う権限がありません。',
    'not_found': '商品が見つかりません。',
}


def init(get_conn: Callable[[], Awaitable[Any]], lock: asyncio.Lock) -> None:
    """アプリ起動時に一度だけ、DB接続取得関数と共有ロックを登録する。"""
    global _get_conn, _lock
    _get_conn = get_conn
    _lock = lock


def _check_initialized() -> None:
    if _get_conn is None or _lock is None:
        raise RuntimeError("market_service.init(get_conn, lock) が呼ばれていません。")


# ------------------------------------------------------------------
# 低レベルDBヘルパー（execute_queryと違い、UNIQUE違反等の例外を握りつぶさない）
# ------------------------------------------------------------------
async def _fetchall(sql: str, params: Optional[list] = None) -> list[dict]:
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        cursor = await conn.execute(sql, params or [])
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
    return [dict(r) for r in rows]


async def _fetchone(sql: str, params: Optional[list] = None) -> Optional[dict]:
    rows = await _fetchall(sql, params)
    return rows[0] if rows else None


async def _execute(sql: str, params: Optional[list] = None) -> tuple[int, Optional[int]]:
    """書き込みを1件実行してcommitし、(rowcount, lastrowid)を返す。失敗時はrollbackして再送出。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        try:
            cursor = await conn.execute(sql, params or [])
            rowcount, lastrowid = cursor.rowcount, cursor.lastrowid
            await cursor.close()
            await conn.commit()
            return rowcount, lastrowid
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


async def ensure_schema() -> None:
    """取引所のテーブルを用意する（冪等・既存データには触れない）。"""
    _check_initialized()
    conn = await _get_conn()
    async with _lock:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER,
                is_official INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price INTEGER NOT NULL CHECK (price > 0),
                preview_url TEXT,
                asset_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK ((is_official = 1 AND seller_id IS NULL)
                    OR (is_official = 0 AND seller_id IS NOT NULL))
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_items_list "
            "ON market_items(status, is_official, id DESC)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_items_seller "
            "ON market_items(seller_id, status)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                buyer_id INTEGER NOT NULL,
                seller_id INTEGER,
                price INTEGER NOT NULL,
                seller_credited INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (item_id, buyer_id)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_purchases_buyer "
            "ON market_purchases(buyer_id, id DESC)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL UNIQUE,
                item_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                buyer_id INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_reviews_seller "
            "ON market_reviews(seller_id, id DESC)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_equipped (
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, kind)
            )
        """)
        await conn.commit()


# ------------------------------------------------------------------
# 検証ヘルパー
# ------------------------------------------------------------------
def validate_proxy_url(url: str) -> str:
    url = (url or '').strip()
    if not url or len(url) > MAX_ASSET_URL or re.search(r'[\s\x00-\x1f]', url):
        raise MarketError('invalid_proxy_url')
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise MarketError('invalid_proxy_url')
    return url


def _validate_common(kind, title, description, price, official: bool):
    allowed = OFFICIAL_KINDS if official else USER_KINDS
    if kind not in allowed:
        raise MarketError('invalid_kind')
    title = (title or '').strip()
    if not title or len(title) > MAX_TITLE:
        raise MarketError('invalid_title')
    description = (description or '').strip()
    if len(description) > MAX_DESC:
        raise MarketError('invalid_description')
    if isinstance(price, bool) or not isinstance(price, int) or not (1 <= price <= MAX_PRICE):
        raise MarketError('invalid_price')
    return title, description


validate_fields = _validate_common   # ルート側から出品フォームの事前検証に使う公開名


def _decorate(row: dict) -> dict:
    """出品者表示名などを付与する。公式は常に「Talk-ch公式」。"""
    if row.get('is_official'):
        row['seller_name'] = OFFICIAL_SELLER_NAME
        row['seller_public_id'] = None
        row['seller_icon'] = None
    else:
        row['seller_name'] = row.get('seller_username') or '退会済みユーザー'
        row['seller_public_id'] = row.get('seller_public_id')
    row['kind_label'] = KIND_LABELS.get(row.get('kind'), row.get('kind'))
    return row


# asset_url を含まない公開用カラム。一覧・詳細はこれだけを使う。
_PUBLIC_COLS = (
    "i.id, i.seller_id, i.is_official, i.kind, i.title, i.description, i.price, "
    "i.preview_url, i.status, i.created_at, "
    "u.username AS seller_username, u.public_id AS seller_public_id, u.icon_path AS seller_icon, "
    "(SELECT COUNT(*) FROM market_purchases p WHERE p.item_id = i.id) AS sales_count, "
    "(SELECT ROUND(AVG(r.rating), 1) FROM market_reviews r WHERE r.seller_id = i.seller_id) AS seller_avg, "
    "(SELECT COUNT(*) FROM market_reviews r WHERE r.seller_id = i.seller_id) AS seller_review_count"
)


# ------------------------------------------------------------------
# 商品の参照
# ------------------------------------------------------------------
async def list_items(kind: Optional[str] = None, official: Optional[bool] = None,
                     seller_id: Optional[int] = None, limit: int = 24, offset: int = 0) -> list[dict]:
    where, params = ["i.status = 'active'"], []
    if official is not None:
        where.append("i.is_official = ?")
        params.append(1 if official else 0)
    if kind:
        where.append("i.kind = ?")
        params.append(kind)
    if seller_id is not None:
        where.append("i.seller_id = ?")
        params.append(seller_id)
    params += [limit, offset]
    rows = await _fetchall(
        f"SELECT {_PUBLIC_COLS} FROM market_items i LEFT JOIN users u ON u.id = i.seller_id "
        f"WHERE {' AND '.join(where)} ORDER BY i.id DESC LIMIT ? OFFSET ?", params)
    return [_decorate(r) for r in rows]


async def get_item(item_id: int) -> Optional[dict]:
    """公開情報のみ（asset_urlは含まない）。status='removed'の行も返す。"""
    row = await _fetchone(
        f"SELECT {_PUBLIC_COLS} FROM market_items i LEFT JOIN users u ON u.id = i.seller_id "
        f"WHERE i.id = ?", [item_id])
    return _decorate(row) if row else None


async def has_purchased(item_id: int, buyer_id: int) -> bool:
    row = await _fetchone(
        "SELECT 1 AS x FROM market_purchases WHERE item_id = ? AND buyer_id = ?", [item_id, buyer_id])
    return row is not None


async def get_item_asset(item_id: int, viewer_id: Optional[int]) -> Optional[str]:
    """商品URL/素材URLを返す。購入者または出品者本人にのみ返し、それ以外はNone。"""
    if not viewer_id:
        return None
    row = await _fetchone(
        "SELECT i.asset_url AS asset_url, i.seller_id AS seller_id, i.is_official AS is_official "
        "FROM market_items i WHERE i.id = ?", [item_id])
    if not row:
        return None
    if not row['is_official'] and row['seller_id'] == viewer_id:
        return row['asset_url']
    if await has_purchased(item_id, viewer_id):
        return row['asset_url']
    return None


async def count_active_listings(seller_id: int) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS c FROM market_items WHERE seller_id = ? AND status = 'active'", [seller_id])
    return int(row['c']) if row else 0


# ------------------------------------------------------------------
# 出品・編集・削除（権限判定は呼び出し側 + ここでも二重に検証）
# ------------------------------------------------------------------
async def create_item(*, seller_id: Optional[int], official: bool, kind: str, title: str,
                      description: str, price: int, preview_url: Optional[str], asset_url: str) -> int:
    title, description = _validate_common(kind, title, description, price, official)
    if official and seller_id is not None:
        raise MarketError('forbidden')          # 公式商品にユーザーIDは紐づけない
    if not official:
        if not seller_id:
            raise MarketError('forbidden')
        if await count_active_listings(seller_id) >= MAX_ACTIVE_LISTINGS_PER_USER:
            raise MarketError('listing_limit')
    if not asset_url or len(asset_url) > MAX_ASSET_URL:
        raise MarketError('invalid_asset')
    if kind == 'proxy':
        asset_url = validate_proxy_url(asset_url)
    _, item_id = await _execute(
        "INSERT INTO market_items (seller_id, is_official, kind, title, description, price, preview_url, asset_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [None if official else seller_id, 1 if official else 0, kind, title, description, price,
         preview_url, asset_url])
    return int(item_id)


async def update_item(item_id: int, *, official: bool, owner_id: Optional[int], title: str,
                      description: str, price: int, preview_url: Optional[str] = None,
                      asset_url: Optional[str] = None) -> None:
    """official=True なら公式商品のみ、False なら owner_id が出品者の一般商品のみ更新できる。
    kind・出品者・公式区分は変更不可。過去の購入記録（支払済み価格）には影響しない。"""
    item = await _fetchone(
        "SELECT id, kind, is_official, seller_id, status FROM market_items WHERE id = ?", [item_id])
    if not item or item['status'] != 'active':
        raise MarketError('not_found')
    if bool(item['is_official']) != official or (not official and item['seller_id'] != owner_id):
        raise MarketError('forbidden')
    title, description = _validate_common(item['kind'], title, description, price, official)
    sets, params = ["title = ?", "description = ?", "price = ?", "updated_at = datetime('now')"], \
                   [title, description, price]
    if preview_url:
        sets.append("preview_url = ?")
        params.append(preview_url)
    if asset_url:
        if item['kind'] == 'proxy':
            asset_url = validate_proxy_url(asset_url)
        sets.append("asset_url = ?")
        params.append(asset_url)
    params.append(item_id)
    await _execute(f"UPDATE market_items SET {', '.join(sets)} WHERE id = ?", params)


async def remove_item(item_id: int, *, official: bool, owner_id: Optional[int]) -> None:
    """論理削除。既に購入した人のインベントリ・購入記録は残す（支払い済みのため）。"""
    item = await _fetchone(
        "SELECT id, is_official, seller_id, status FROM market_items WHERE id = ?", [item_id])
    if not item:
        raise MarketError('not_found')
    if bool(item['is_official']) != official or (not official and item['seller_id'] != owner_id):
        raise MarketError('forbidden')
    await _execute(
        "UPDATE market_items SET status = 'removed', updated_at = datetime('now') WHERE id = ?", [item_id])


# ------------------------------------------------------------------
# 購入
# ------------------------------------------------------------------
async def _credit_seller(purchase: dict, title: str) -> None:
    """出品者へ売上を入金（公式商品は入金なし）。idempotency_keyで二重入金不可。"""
    if purchase['seller_credited']:
        return
    if purchase['seller_id'] is None:
        await _execute("UPDATE market_purchases SET seller_credited = 1 WHERE id = ?", [purchase['id']])
        return
    res = await points_service.add_points(
        purchase['seller_id'], purchase['price'], reason='market_sale',
        description=f'取引所で「{title}」が購入されました', source='market',
        idempotency_key=f"market_sale:{purchase['item_id']}:{purchase['buyer_id']}")
    if res['success'] or res['duplicate']:
        await _execute("UPDATE market_purchases SET seller_credited = 1 WHERE id = ?", [purchase['id']])


async def reconcile_pending_credits() -> int:
    """入金漏れ（支払い後にプロセスが落ちた等）を再実行する。起動時に呼ぶ。"""
    rows = await _fetchall(
        "SELECT p.id, p.item_id, p.buyer_id, p.seller_id, p.price, p.seller_credited, i.title "
        "FROM market_purchases p JOIN market_items i ON i.id = p.item_id "
        "WHERE p.seller_credited = 0")
    for r in rows:
        try:
            await _credit_seller(r, r['title'])
        except Exception as e:
            print(f"取引所: 入金の再実行に失敗 purchase={r['id']}: {e}")
    return len(rows)


async def purchase(item_id: int, buyer_id: int) -> dict:
    """商品を購入する。戻り値: {'success', 'error', 'balance'}。

    順序: 事前チェック → spend_points(冪等キー付き) → 購入記録(INSERT OR IGNORE) → 出品者入金。
    """
    item = await _fetchone(
        "SELECT id, seller_id, is_official, title, price, status FROM market_items WHERE id = ?", [item_id])
    if not item or item['status'] != 'active':
        return {'success': False, 'error': 'item_unavailable', 'balance': await points_service.get_balance(buyer_id)}
    if not item['is_official'] and item['seller_id'] == buyer_id:
        return {'success': False, 'error': 'own_item', 'balance': await points_service.get_balance(buyer_id)}

    existing = await _fetchone(
        "SELECT id, item_id, buyer_id, seller_id, price, seller_credited FROM market_purchases "
        "WHERE item_id = ? AND buyer_id = ?", [item_id, buyer_id])
    if existing:
        await _credit_seller(existing, item['title'])   # 入金漏れの自己修復
        return {'success': False, 'error': 'already_purchased', 'balance': await points_service.get_balance(buyer_id)}

    key = f"market_buy:{item_id}:{buyer_id}"
    paid = await points_service.spend_points(
        buyer_id, item['price'], reason='market_purchase',
        description=f"取引所で「{item['title']}」を購入", source='market', idempotency_key=key)
    if paid['error']:
        return {'success': False, 'error': paid['error'], 'balance': paid['balance']}

    price = item['price']
    if paid['duplicate']:
        # 既に支払い済み: 実際に引かれた額を台帳（読み取りのみ）から取り直す。
        led = await _fetchone("SELECT delta FROM point_history WHERE idempotency_key = ?", [key])
        if led:
            price = -int(led['delta'])

    rowcount, _ = await _execute(
        "INSERT OR IGNORE INTO market_purchases (item_id, buyer_id, seller_id, price) VALUES (?, ?, ?, ?)",
        [item_id, buyer_id, item['seller_id'], price])
    created_now = rowcount == 1
    row = await _fetchone(
        "SELECT id, item_id, buyer_id, seller_id, price, seller_credited FROM market_purchases "
        "WHERE item_id = ? AND buyer_id = ?", [item_id, buyer_id])
    if row:
        await _credit_seller(row, item['title'])

    balance = await points_service.get_balance(buyer_id)
    if paid['duplicate'] and not created_now:
        # 同時リクエストの片方: 購入は既に1件だけ成立している
        return {'success': False, 'error': 'already_purchased', 'balance': balance}
    return {'success': True, 'error': None, 'balance': balance}


# ------------------------------------------------------------------
# インベントリ・適用（壁紙/フォント）
# ------------------------------------------------------------------
async def get_inventory(buyer_id: int) -> list[dict]:
    rows = await _fetchall(
        "SELECT p.id AS purchase_id, p.price AS paid_price, p.created_at AS purchased_at, "
        "i.id, i.kind, i.title, i.description, i.preview_url, i.asset_url, i.status, "
        "i.is_official, i.seller_id, u.username AS seller_username, u.public_id AS seller_public_id, "
        "(SELECT 1 FROM market_equipped e WHERE e.user_id = p.buyer_id AND e.item_id = i.id) AS equipped, "
        "(SELECT r.rating FROM market_reviews r WHERE r.purchase_id = p.id) AS my_rating "
        "FROM market_purchases p JOIN market_items i ON i.id = p.item_id "
        "LEFT JOIN users u ON u.id = i.seller_id "
        "WHERE p.buyer_id = ? ORDER BY p.id DESC", [buyer_id])
    return [_decorate(r) for r in rows]


async def equip(user_id: int, item_id: int) -> None:
    item = await _fetchone("SELECT kind FROM market_items WHERE id = ?", [item_id])
    if not item:
        raise MarketError('not_found')
    if not await has_purchased(item_id, user_id):
        raise MarketError('not_owned')
    if item['kind'] not in EQUIPPABLE_KINDS:
        raise MarketError('not_equippable')
    await _execute(
        "INSERT INTO market_equipped (user_id, kind, item_id) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, kind) DO UPDATE SET item_id = excluded.item_id",
        [user_id, item['kind'], item_id])


async def unequip(user_id: int, kind: str) -> None:
    if kind not in EQUIPPABLE_KINDS:
        raise MarketError('not_equippable')
    await _execute("DELETE FROM market_equipped WHERE user_id = ? AND kind = ?", [user_id, kind])


async def get_theme(user_id: int) -> Optional[dict]:
    """適用中の壁紙・フォントURL。購入記録が無いもの（返金等で失効）は返さない。"""
    rows = await _fetchall(
        "SELECT e.kind AS kind, i.asset_url AS asset_url FROM market_equipped e "
        "JOIN market_items i ON i.id = e.item_id "
        "JOIN market_purchases p ON p.item_id = e.item_id AND p.buyer_id = e.user_id "
        "WHERE e.user_id = ?", [user_id])
    theme = {r['kind']: r['asset_url'] for r in rows}
    if not theme:
        return None
    font_url = theme.get('font')
    fmt = 'woff2'
    if font_url:
        ext = font_url.rsplit('.', 1)[-1].lower()
        fmt = {'woff2': 'woff2', 'woff': 'woff', 'ttf': 'truetype', 'otf': 'opentype'}.get(ext, 'woff2')
    return {'wallpaper': theme.get('wallpaper'), 'font': font_url, 'font_format': fmt}


# ------------------------------------------------------------------
# 評価（一般ユーザーの出品者のみ・購入者のみ・1購入につき1件）
# ------------------------------------------------------------------
async def submit_review(buyer_id: int, item_id: int, rating: Any, comment: str) -> None:
    if isinstance(rating, bool) or not isinstance(rating, int) or not (1 <= rating <= 5):
        raise MarketError('invalid_rating')
    comment = (comment or '').strip()
    if len(comment) > MAX_COMMENT:
        raise MarketError('invalid_comment')
    item = await _fetchone("SELECT id, is_official, seller_id FROM market_items WHERE id = ?", [item_id])
    if not item:
        raise MarketError('not_found')
    if item['is_official'] or item['seller_id'] is None:
        raise MarketError('official_not_rateable')
    if item['seller_id'] == buyer_id:
        raise MarketError('own_item')
    purchase_row = await _fetchone(
        "SELECT id FROM market_purchases WHERE item_id = ? AND buyer_id = ?", [item_id, buyer_id])
    if not purchase_row:
        raise MarketError('not_purchased')
    await _execute(
        "INSERT INTO market_reviews (purchase_id, item_id, seller_id, buyer_id, rating, comment) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(purchase_id) DO UPDATE SET rating = excluded.rating, comment = excluded.comment, "
        "updated_at = datetime('now')",
        [purchase_row['id'], item_id, item['seller_id'], buyer_id, rating, comment])


async def get_my_review(buyer_id: int, item_id: int) -> Optional[dict]:
    return await _fetchone(
        "SELECT rating, comment FROM market_reviews WHERE buyer_id = ? AND item_id = ?", [buyer_id, item_id])


async def list_reviews(seller_id: int, item_id: Optional[int] = None, limit: int = 20, offset: int = 0) -> list[dict]:
    where, params = ["r.seller_id = ?"], [seller_id]
    if item_id is not None:
        where.append("r.item_id = ?")
        params.append(item_id)
    params += [limit, offset]
    return await _fetchall(
        "SELECT r.rating, r.comment, r.created_at, r.item_id, i.title AS item_title, "
        "u.username AS buyer_username, u.public_id AS buyer_public_id, u.icon_path AS buyer_icon "
        "FROM market_reviews r JOIN market_items i ON i.id = r.item_id "
        "LEFT JOIN users u ON u.id = r.buyer_id "
        f"WHERE {' AND '.join(where)} ORDER BY r.id DESC LIMIT ? OFFSET ?", params)


async def seller_rating(seller_id: int) -> dict:
    row = await _fetchone(
        "SELECT ROUND(AVG(rating), 1) AS avg, COUNT(*) AS cnt FROM market_reviews WHERE seller_id = ?",
        [seller_id])
    return {'avg': row['avg'] if row and row['cnt'] else None, 'count': int(row['cnt']) if row else 0}
