"""人狼ゲーム(4〜12人)。登録は battle_games/__init__.py の register_all() から。

■ 進行の仕組み
  バックグラウンドタスクは使わず、状態取得(/api/werewolf/{code}/state)や各操作のたびに
  「時間切れ/全員行動済みなら次のフェーズへ」を _advance() が進める(遅延進行)。
  フェーズ遷移は phase_no を使った比較交換(CAS)を1トランザクションで行うので、
  複数workerから同時に呼ばれても二重に処理されない。

■ フェーズ
  waiting(募集) → night(夜) → day(昼の議論) → vote(投票) → night → ... → end(終了)
  ・最初の夜は襲撃・護衛なし(占いだけ)
  ・投票が同数なら1回だけ決選投票、それも同数ならランダムで1人処刑

■ 役職(人数で自動決定。role_set() 参照)
  村人 / 人狼 / 占い師 / 騎士 / 霊媒師 / 狂人(人狼陣営だが占い結果は「人狼ではない」)
"""
import asyncio
import html
import json
import random
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

import points_service
from game_service import (
    Game, _game_token, _game_name, _game_member_id, _cookie_response,
    GAME_POINTS_WIN, GAME_POINTS_LOSS,
    execute_query, _get_db_conn, _db_lock, filter_ng_words, json_resp, templates,
)

router = APIRouter()

# =========================
# 設定
# =========================
MIN_PLAYERS = 4
MAX_PLAYERS = 12
NIGHT_FIRST_SEC = 30        # 最初の夜(占いのみ)
NIGHT_SEC = 60
VOTE_SEC = 60
NIGHT_MIN_SEC = 15          # 全員行動済みでも、これより早くは朝にしない(誰が行動したかの推測防止)
DISCUSS_DEFAULT = 180
DISCUSS_MIN, DISCUSS_MAX = 60, 300
CHAT_MAX_LEN = 200
CHAT_MIN_INTERVAL = 1.0     # 秒。連投防止
ROOM_TTL_HOURS = 24         # これより長く更新の無い部屋は、部屋作成時にまとめて掃除する
LIST_WINDOW_HOURS = 2       # ロビー一覧に出す部屋の新しさ

ROLE_LABELS = {
    'villager': '村人', 'werewolf': '人狼', 'seer': '占い師',
    'knight': '騎士', 'medium': '霊媒師', 'madman': '狂人',
}
ROLE_ORDER = ['werewolf', 'madman', 'seer', 'knight', 'medium', 'villager']
ROLE_DESC = {
    'villager': '特殊能力はありません。議論と投票で人狼を見つけましょう。',
    'werewolf': '夜に仲間と相談して1人を襲撃します。村人と同数まで減らせば勝ちです。',
    'seer': '毎晩1人を占い、人狼かどうかを知ることができます。',
    'knight': '毎晩1人を護衛します(自分は不可)。襲撃先と一致すれば犠牲者が出ません。',
    'medium': '処刑された人が人狼だったかどうかを知ることができます。',
    'madman': '人狼陣営ですが、人狼が誰かは分かりません。占われても「人狼ではない」と出ます。',
}


def role_set(n: int) -> list:
    """人数nに応じた役職構成(長さn)を返す。"""
    wolves = 1 if n <= 6 else (2 if n <= 10 else 3)
    roles = ['werewolf'] * wolves + ['seer']
    if n >= 5:
        roles.append('knight')
    if n >= 8:
        roles.append('medium')
    if n >= 9:
        roles.append('madman')
    roles += ['villager'] * (n - len(roles))
    return roles[:n]


def _composition_text(roles: list) -> str:
    parts = []
    for r in ROLE_ORDER:
        c = roles.count(r)
        if c:
            parts.append(f'{ROLE_LABELS[r]}{c}')
    return '、'.join(parts)


# =========================
# DB
# =========================
async def ensure_schema():
    conn = await _get_db_conn()
    async with _db_lock:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS werewolf_rooms (
                room_code   TEXT PRIMARY KEY,
                host_token  TEXT,
                status      TEXT NOT NULL DEFAULT 'waiting',
                phase       TEXT NOT NULL DEFAULT 'waiting',
                phase_no    INTEGER NOT NULL DEFAULT 0,
                day_no      INTEGER NOT NULL DEFAULT 0,
                deadline    REAL,
                discuss_sec INTEGER NOT NULL DEFAULT 180,
                revote      INTEGER NOT NULL DEFAULT 0,
                candidates  TEXT,
                winner      TEXT,
                created_at  TEXT,
                updated_at  TEXT
            )''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS werewolf_players (
                room_code     TEXT NOT NULL,
                token         TEXT NOT NULL,
                name          TEXT,
                member_id     TEXT,
                seat          INTEGER NOT NULL,
                role          TEXT,
                alive         INTEGER NOT NULL DEFAULT 1,
                action_target INTEGER,
                vote_target   INTEGER,
                ready         INTEGER NOT NULL DEFAULT 0,
                notes         TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (room_code, token),
                UNIQUE (room_code, seat)
            )''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS werewolf_messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                channel   TEXT NOT NULL,
                token     TEXT,
                name      TEXT,
                seat      INTEGER,
                text      TEXT,
                created   REAL
            )''')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_ww_msg_room ON werewolf_messages (room_code, id)')
        await conn.commit()


async def _tx(stmts, cas=False) -> bool:
    """複数SQLを1トランザクションで実行する。cas=True なら先頭文の更新件数が0のとき全部取り消して False。"""
    conn = await _get_db_conn()
    async with _db_lock:
        try:
            for i, (sql, params) in enumerate(stmts):
                cur = await conn.execute(sql, params)
                rc = cur.rowcount
                await cur.close()
                if cas and i == 0 and rc <= 0:
                    await conn.rollback()
                    return False
            await conn.commit()
            return True
        except Exception as e:
            try:
                await conn.rollback()
            except Exception:
                pass
            print(f'人狼 トランザクションエラー: {e}')
            return False


async def _load(code: str):
    rows = await execute_query('SELECT * FROM werewolf_rooms WHERE room_code = ? LIMIT 1', [code])
    if not rows:
        return None, []
    players = await execute_query(
        'SELECT * FROM werewolf_players WHERE room_code = ? ORDER BY seat', [code])
    return rows[0], players


def _iso_now() -> str:
    return datetime.utcnow().isoformat()


def _sys(code: str, text: str):
    return ('INSERT INTO werewolf_messages (room_code, channel, text, created) VALUES (?,?,?,?)',
            [code, 'system', text, time.time()])


def _notes(p) -> list:
    try:
        return json.loads(p.get('notes') or '[]')
    except Exception:
        return []


def _note_stmt(code: str, p, text: str):
    notes = _notes(p) + [text]
    p['notes'] = json.dumps(notes, ensure_ascii=False)
    return ('UPDATE werewolf_players SET notes = ? WHERE room_code = ? AND token = ?',
            [p['notes'], code, p['token']])


def _winner(players) -> str:
    wolves = sum(1 for p in players if p['alive'] and p['role'] == 'werewolf')
    others = sum(1 for p in players if p['alive'] and p['role'] != 'werewolf')
    if wolves == 0:
        return 'villagers'
    if wolves >= others:
        return 'wolves'
    return None


def _team(role: str) -> str:
    return 'wolves' if role in ('werewolf', 'madman') else 'villagers'


# =========================
# 進行エンジン
# =========================
def _night_actors(room, players):
    """この夜に行動が必要な生存者。"""
    first = room['day_no'] == 0
    actors = []
    for p in players:
        if not p['alive']:
            continue
        if p['role'] == 'seer' or (not first and p['role'] in ('werewolf', 'knight')):
            actors.append(p)
    return actors


def _finish_stmts(code, players, winner):
    label = '村人陣営' if winner == 'villagers' else '人狼陣営'
    lines = ' / '.join(
        f"No.{p['seat']} {p['name']}:{ROLE_LABELS.get(p['role'], '?')}"
        + ('' if p['alive'] else '(死亡)') for p in players)
    return [
        _sys(code, f'ゲーム終了! {label}の勝利です。'),
        _sys(code, f'役職一覧: {lines}'),
    ]


async def _award_points(code: str, winner: str, players: list):
    """終了時、ログイン会員の参加者にポイントを付与(勝ち+20/負け+5。idempotency_keyで二重付与なし)。"""
    for p in players:
        mid = p.get('member_id')
        if not mid:
            continue
        try:
            uid = int(mid)
        except (TypeError, ValueError):
            continue
        won = _team(p['role']) == winner
        try:
            await points_service.add_points(
                uid, GAME_POINTS_WIN if won else GAME_POINTS_LOSS,
                reason='game_win' if won else 'game_loss',
                description='人狼で勝利' if won else '人狼で敗北',
                source='game', idempotency_key=f'game:werewolf:{code}:{uid}',
            )
        except Exception as e:
            print(f'人狼ポイント付与エラー(room={code}, user={uid}): {e}')


def _reset_actions_stmt(code):
    return ('UPDATE werewolf_players SET action_target = NULL, vote_target = NULL, ready = 0 WHERE room_code = ?',
            [code])


def _room_update(code, phase_no, **fields):
    """phase_no が一致するときだけ部屋を更新するCAS文(phase_no は自動で+1)。"""
    cols = ', '.join(f'{k} = ?' for k in fields)
    return (f'UPDATE werewolf_rooms SET {cols}, phase_no = phase_no + 1, updated_at = ? '
            'WHERE room_code = ? AND phase_no = ?',
            list(fields.values()) + [_iso_now(), code, phase_no])


async def _resolve_night(room, players) -> bool:
    code, day_no = room['room_code'], room['day_no']
    by_seat = {p['seat']: p for p in players}
    note_stmts, msg_stmts = [], []

    # 占い
    seer = next((p for p in players if p['alive'] and p['role'] == 'seer'), None)
    if seer and seer['action_target'] in by_seat:
        t = by_seat[seer['action_target']]
        result = '人狼' if t['role'] == 'werewolf' else '人狼ではない'
        note_stmts.append(_note_stmt(
            code, seer, f"第{day_no + 1}夜の占い: No.{t['seat']} {t['name']}さんは【{result}】"))

    # 襲撃(最初の夜は無し)
    victim = None
    if day_no > 0:
        votes = [p['action_target'] for p in players
                 if p['alive'] and p['role'] == 'werewolf' and p['action_target'] in by_seat]
        if votes:
            counts = {}
            for s in votes:
                counts[s] = counts.get(s, 0) + 1
            top = max(counts.values())
            target = random.choice([s for s, c in counts.items() if c == top])
            knight = next((p for p in players if p['alive'] and p['role'] == 'knight'), None)
            guarded = knight['action_target'] if knight else None
            if target != guarded and by_seat[target]['alive'] and by_seat[target]['role'] != 'werewolf':
                victim = by_seat[target]

    new_day = day_no + 1
    stmts = []
    if victim:
        victim['alive'] = 0
        stmts.append(('UPDATE werewolf_players SET alive = 0 WHERE room_code = ? AND token = ?',
                      [code, victim['token']]))
        msg_stmts.append(_sys(code, f"【{new_day}日目】朝になりました。No.{victim['seat']} {victim['name']}さんが無残な姿で発見されました。"))
    else:
        msg_stmts.append(_sys(code, f'【{new_day}日目】朝になりました。昨夜は誰も犠牲になりませんでした。'))

    winner = _winner(players)
    if winner:
        cas = _room_update(code, room['phase_no'], phase='end', status='finished', winner=winner,
                           deadline=None, day_no=new_day)
        stmts = [cas] + stmts + note_stmts + msg_stmts + _finish_stmts(code, players, winner)
    else:
        alive_names = '、'.join(f"No.{p['seat']}" for p in players if p['alive'])
        msg_stmts.append(_sys(code, f'生存者: {alive_names}。議論を始めてください。'))
        cas = _room_update(code, room['phase_no'], phase='day', day_no=new_day,
                           deadline=time.time() + room['discuss_sec'], revote=0, candidates=None)
        stmts = [cas] + stmts + [_reset_actions_stmt(code)] + note_stmts + msg_stmts

    ok = await _tx(stmts, cas=True)
    if ok and winner:
        asyncio.create_task(_award_points(code, winner, players))
    return ok


async def _tally_votes(room, players) -> bool:
    code, day_no = room['room_code'], room['day_no']
    by_seat = {p['seat']: p for p in players}
    alive = [p for p in players if p['alive']]
    candidates = None
    if room.get('candidates'):
        try:
            candidates = set(json.loads(room['candidates']))
        except Exception:
            candidates = None

    counts = {}
    detail = []
    for p in alive:
        t = p['vote_target']
        if t in by_seat:
            counts[t] = counts.get(t, 0) + 1
            detail.append(f"No.{p['seat']}→No.{t}")
    msgs = []
    if detail:
        msgs.append(_sys(code, '投票結果: ' + ' / '.join(detail)))

    if counts:
        top = max(counts.values())
        tied = sorted(s for s, c in counts.items() if c == top)
    else:
        tied = sorted(candidates) if candidates else [p['seat'] for p in alive]
        msgs.append(_sys(code, '誰も投票しなかったため、ランダムに処刑者を決めます。'))

    if len(tied) > 1 and not room['revote'] and counts:
        names = '、'.join(f'No.{s}' for s in tied)
        msgs.append(_sys(code, f'同数のため決選投票を行います。候補: {names}'))
        cas = _room_update(code, room['phase_no'], phase='vote', revote=1,
                           candidates=json.dumps(tied), deadline=time.time() + VOTE_SEC)
        return await _tx([cas, _reset_actions_stmt(code)] + msgs, cas=True)

    executed = by_seat[tied[0] if len(tied) == 1 else random.choice(tied)]
    executed['alive'] = 0
    stmts = [('UPDATE werewolf_players SET alive = 0 WHERE room_code = ? AND token = ?',
              [code, executed['token']])]
    msgs.append(_sys(code, f"No.{executed['seat']} {executed['name']}さんが処刑されました。"))
    result = '人狼' if executed['role'] == 'werewolf' else '人狼ではない'
    for m in players:
        if m['alive'] and m['role'] == 'medium':
            stmts.append(_note_stmt(code, m, f"{day_no}日目に処刑されたNo.{executed['seat']} {executed['name']}さんは【{result}】でした"))

    winner = _winner(players)
    if winner:
        cas = _room_update(code, room['phase_no'], phase='end', status='finished', winner=winner,
                           deadline=None, revote=0, candidates=None)
        all_stmts = [cas] + stmts + msgs + _finish_stmts(code, players, winner)
    else:
        msgs.append(_sys(code, '夜になりました。役職を持つ人は行動してください。'))
        cas = _room_update(code, room['phase_no'], phase='night', deadline=time.time() + NIGHT_SEC,
                           revote=0, candidates=None)
        all_stmts = [cas, _reset_actions_stmt(code)] + stmts + msgs

    ok = await _tx(all_stmts, cas=True)
    if ok and winner:
        asyncio.create_task(_award_points(code, winner, players))
    return ok


async def _step(room, players) -> bool:
    """必要なら1段階フェーズを進める。進めたら True。"""
    now = time.time()
    phase = room['phase']
    deadline = room['deadline'] or 0
    alive = [p for p in players if p['alive']]

    if phase == 'night':
        actors = _night_actors(room, players)
        dur = NIGHT_FIRST_SEC if room['day_no'] == 0 else NIGHT_SEC
        all_done = all(p['action_target'] is not None for p in actors)
        early_ok = now >= deadline - dur + NIGHT_MIN_SEC
        if now >= deadline or (all_done and early_ok):
            return await _resolve_night(room, players)
        return False

    if phase == 'day':
        if now >= deadline or all(p['ready'] for p in alive):
            cas = _room_update(room['room_code'], room['phase_no'], phase='vote', revote=0,
                               candidates=None, deadline=now + VOTE_SEC)
            return await _tx([cas, _reset_actions_stmt(room['room_code']),
                              _sys(room['room_code'], '投票の時間です。処刑する人を選んでください。')], cas=True)
        return False

    if phase == 'vote':
        if now >= deadline or all(p['vote_target'] is not None for p in alive):
            return await _tally_votes(room, players)
        return False

    return False


async def _advance(code: str):
    for _ in range(6):
        room, players = await _load(code)
        if not room or room['status'] != 'playing':
            return room, players
        if not await _step(room, players):
            return room, players
    return await _load(code)


# =========================
# 補助
# =========================
async def _body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_me(players, token):
    return next((p for p in players if p['token'] == token), None)


def _can_post(room, me) -> list:
    if not me:
        return []
    if room['status'] in ('waiting', 'finished'):
        return ['day']
    if not me['alive']:
        return ['dead']
    if room['phase'] in ('day', 'vote'):
        return ['day']
    if room['phase'] == 'night' and me['role'] == 'werewolf':
        return ['wolf']
    return []


def _visible_channels(room, me) -> set:
    vis = {'system', 'day'}
    if room['status'] == 'finished':
        return vis | {'wolf', 'dead'}
    if me and not me['alive']:
        vis |= {'wolf', 'dead'}
    elif me and me['role'] == 'werewolf':
        vis.add('wolf')
    return vis


async def _new_code() -> str:
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(30):
        code = ''.join(random.choice(alphabet) for _ in range(6))
        if not await execute_query('SELECT 1 FROM werewolf_rooms WHERE room_code = ? LIMIT 1', [code]):
            return code
    return ''.join(random.choice(alphabet) for _ in range(8))


async def _cleanup_old_rooms():
    cutoff = (datetime.utcnow() - timedelta(hours=ROOM_TTL_HOURS)).isoformat()
    old = "SELECT room_code FROM werewolf_rooms WHERE updated_at < ?"
    await _tx([
        (f'DELETE FROM werewolf_messages WHERE room_code IN ({old})', [cutoff]),
        (f'DELETE FROM werewolf_players WHERE room_code IN ({old})', [cutoff]),
        ('DELETE FROM werewolf_rooms WHERE updated_at < ?', [cutoff]),
    ])


# =========================
# ページ / ロビー
# =========================
@router.get('/werewolf')
async def werewolf_lobby(request: Request):
    resp = templates.TemplateResponse(request, 'werewolf.html', {'room_code': ''})
    return _cookie_response(request, resp, _game_token(request))


@router.get('/api/werewolf/rooms')
async def werewolf_rooms(request: Request):
    cutoff = (datetime.utcnow() - timedelta(hours=LIST_WINDOW_HOURS)).isoformat()
    rows = await execute_query(
        '''SELECT r.room_code, r.status, r.discuss_sec, r.day_no,
                  (SELECT COUNT(*) FROM werewolf_players p WHERE p.room_code = r.room_code) AS cnt,
                  (SELECT name FROM werewolf_players p WHERE p.room_code = r.room_code
                      AND p.token = r.host_token) AS host_name
           FROM werewolf_rooms r
           WHERE r.status IN ('waiting', 'playing') AND r.updated_at >= ?
           ORDER BY (r.status = 'waiting') DESC, r.updated_at DESC LIMIT 40''', [cutoff])
    rooms = [{
        'room_code': r['room_code'], 'status': r['status'], 'count': r['cnt'],
        'max': MAX_PLAYERS, 'host_name': r['host_name'] or '名無しさん',
        'discuss_sec': r['discuss_sec'], 'day_no': r['day_no'],
    } for r in rows if r['cnt'] > 0]
    return {'success': True, 'rooms': rooms}


@router.post('/werewolf/create')
async def werewolf_create(request: Request):
    token = _game_token(request)
    name = await _game_name(request)
    member_id = await _game_member_id(request)
    body = await _body(request)
    try:
        discuss = int(body.get('discuss_sec', DISCUSS_DEFAULT))
    except (TypeError, ValueError):
        discuss = DISCUSS_DEFAULT
    discuss = max(DISCUSS_MIN, min(DISCUSS_MAX, discuss))

    await _cleanup_old_rooms()
    code = await _new_code()
    now = _iso_now()
    ok = await _tx([
        ('''INSERT INTO werewolf_rooms
            (room_code, host_token, status, phase, phase_no, day_no, discuss_sec, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
         [code, token, 'waiting', 'waiting', 0, 0, discuss, now, now]),
        ('''INSERT INTO werewolf_players (room_code, token, name, member_id, seat, alive, notes)
            VALUES (?,?,?,?,?,?,?)''', [code, token, name, member_id, 1, 1, '[]']),
        _sys(code, f'{name}さんが部屋を作りました。{MIN_PLAYERS}人以上で開始できます。'),
    ])
    if not ok:
        return json_resp({'success': False, 'error': '部屋を作れませんでした。もう一度お試しください'}, 500)
    resp = json_resp({'success': True, 'url': f'/werewolf/{code}', 'room_code': code})
    return _cookie_response(request, resp, token)


@router.get('/werewolf/{room_code}')
async def werewolf_room(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT 1 FROM werewolf_rooms WHERE room_code = ? LIMIT 1', [code])
    if not rows:
        return RedirectResponse(url='/werewolf')
    resp = templates.TemplateResponse(request, 'werewolf.html', {'room_code': code})
    return _cookie_response(request, resp, _game_token(request))


@router.post('/werewolf/{room_code}/join')
async def werewolf_join(request: Request, room_code: str):
    code = room_code.upper()
    room, players = await _load(code)
    if not room:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    token = _game_token(request)
    if _find_me(players, token):
        return {'success': True}
    if room['status'] != 'waiting':
        return json_resp({'success': False, 'error': 'すでに対局が始まっています(観戦のみ可能です)'}, 409)
    if len(players) >= MAX_PLAYERS:
        return json_resp({'success': False, 'error': f'満員です(最大{MAX_PLAYERS}人)'}, 409)
    name = await _game_name(request)
    member_id = await _game_member_id(request)
    ok = await _tx([
        ('''INSERT INTO werewolf_players (room_code, token, name, member_id, seat, alive, notes)
            SELECT ?, ?, ?, ?, COALESCE(MAX(seat), 0) + 1, 1, '[]'
            FROM werewolf_players WHERE room_code = ? HAVING COUNT(*) < ?''',
         [code, token, name, member_id, code, MAX_PLAYERS]),
        ('UPDATE werewolf_rooms SET updated_at = ? WHERE room_code = ?', [_iso_now(), code]),
        _sys(code, f'{name}さんが参加しました。'),
    ], cas=True)
    if not ok:
        return json_resp({'success': False, 'error': '参加できませんでした。もう一度お試しください'}, 409)
    resp = json_resp({'success': True})
    return _cookie_response(request, resp, token)


@router.post('/werewolf/{room_code}/leave')
async def werewolf_leave(request: Request, room_code: str):
    code = room_code.upper()
    room, players = await _load(code)
    if not room:
        return {'success': True}
    token = _game_token(request)
    me = _find_me(players, token)
    if not me:
        return {'success': True}
    if room['status'] == 'playing':
        return json_resp({'success': False, 'error': '対局中は退出できません'}, 409)
    rest = [p for p in players if p['token'] != token]
    if room['status'] == 'finished' or not rest:
        # 終了後の退出は席を空けるだけ。誰もいなくなったら部屋ごと片付ける
        stmts = [('DELETE FROM werewolf_players WHERE room_code = ? AND token = ?', [code, token])]
        if not rest:
            stmts += [('DELETE FROM werewolf_messages WHERE room_code = ?', [code]),
                      ('DELETE FROM werewolf_rooms WHERE room_code = ?', [code])]
        await _tx(stmts)
        return {'success': True}
    stmts = [('DELETE FROM werewolf_players WHERE room_code = ? AND token = ?', [code, token]),
             ('UPDATE werewolf_rooms SET updated_at = ? WHERE room_code = ?', [_iso_now(), code]),
             _sys(code, f"{me['name']}さんが退出しました。")]
    if room['host_token'] == token:
        new_host = rest[0]
        stmts.append(('UPDATE werewolf_rooms SET host_token = ? WHERE room_code = ?', [new_host['token'], code]))
        stmts.append(_sys(code, f"{new_host['name']}さんが新しいホストになりました。"))
    await _tx(stmts)
    return {'success': True}


@router.post('/werewolf/{room_code}/start')
async def werewolf_start(request: Request, room_code: str):
    code = room_code.upper()
    room, players = await _load(code)
    if not room:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    token = _game_token(request)
    if room['host_token'] != token:
        return json_resp({'success': False, 'error': 'ホストだけが開始できます'}, 403)
    if room['status'] != 'waiting':
        return json_resp({'success': False, 'error': 'すでに開始しています'}, 409)
    n = len(players)
    if n < MIN_PLAYERS:
        return json_resp({'success': False, 'error': f'{MIN_PLAYERS}人以上必要です(現在{n}人)'}, 400)

    roles = role_set(n)
    random.shuffle(roles)
    cas = ('''UPDATE werewolf_rooms SET status = 'playing', phase = 'night', phase_no = phase_no + 1,
                     day_no = 0, deadline = ?, revote = 0, candidates = NULL, winner = NULL, updated_at = ?
              WHERE room_code = ? AND status = 'waiting'
                AND (SELECT COUNT(*) FROM werewolf_players WHERE room_code = ?) = ?''',
           [time.time() + NIGHT_FIRST_SEC, _iso_now(), code, code, n])
    stmts = [cas]
    for p, role in zip(players, roles):
        stmts.append(('''UPDATE werewolf_players SET role = ?, alive = 1, action_target = NULL,
                         vote_target = NULL, ready = 0, notes = '[]'
                         WHERE room_code = ? AND token = ?''', [role, code, p['token']]))
    stmts.append(('DELETE FROM werewolf_players WHERE room_code = ? AND role IS NULL', [code]))
    stmts.append(_sys(code, f'ゲーム開始! 参加{n}人。役職構成: {_composition_text(roles)}'))
    stmts.append(_sys(code, '最初の夜です。占い師は占う相手を選んでください(襲撃はありません)。'))
    if not await _tx(stmts, cas=True):
        return json_resp({'success': False, 'error': '開始できませんでした。人数が変わった可能性があります'}, 409)
    return {'success': True}


# =========================
# 状態 / 操作
# =========================
@router.get('/api/werewolf/{room_code}/state')
async def werewolf_state(request: Request, room_code: str):
    code = room_code.upper()
    room, players = await _advance(code)
    if not room:
        return json_resp({'success': False, 'error': 'not found'}, 404)
    token = _game_token(request)
    me = _find_me(players, token)
    finished = room['status'] == 'finished'
    playing = room['status'] == 'playing'
    my_role = me['role'] if me else None

    try:
        since = int(request.query_params.get('since', 0))
    except ValueError:
        since = 0

    out_players = []
    for p in players:
        info = {
            'seat': p['seat'], 'name': p['name'] or '名無しさん', 'alive': bool(p['alive']),
            'id': (p['token'] or '')[:4], 'is_me': bool(me and p['token'] == token),
            'is_host': p['token'] == room['host_token'], 'role': None,
        }
        if finished or info['is_me'] or (my_role == 'werewolf' and p['role'] == 'werewolf'):
            if p['role']:
                info['role'] = p['role']
                info['role_label'] = ROLE_LABELS.get(p['role'])
        out_players.append(info)

    remaining = None
    if playing and room['deadline']:
        remaining = max(0, int(room['deadline'] - time.time() + 0.999))

    candidates = []
    if room.get('candidates'):
        try:
            candidates = json.loads(room['candidates'])
        except Exception:
            candidates = []

    my = None
    if me:
        my = {
            'seat': me['seat'], 'alive': bool(me['alive']), 'role': my_role,
            'role_label': ROLE_LABELS.get(my_role), 'role_desc': ROLE_DESC.get(my_role),
            'notes': _notes(me), 'action_target': me['action_target'],
            'vote_target': me['vote_target'], 'ready': bool(me['ready']),
            'is_host': me['token'] == room['host_token'],
        }
        if playing and my_role == 'werewolf':
            # 仲間の人狼が選んでいる襲撃先(相談用)
            my['wolf_targets'] = {p['seat']: p['action_target'] for p in players
                                  if p['role'] == 'werewolf' and p['action_target'] is not None}

    # 夜に自分が選べる行動
    can_act = None
    if me and playing and me['alive'] and room['phase'] == 'night':
        first = room['day_no'] == 0
        if my_role == 'seer':
            can_act = {'kind': 'divine', 'label': '占う'}
        elif my_role == 'werewolf' and not first:
            can_act = {'kind': 'attack', 'label': '襲撃'}
        elif my_role == 'knight' and not first:
            can_act = {'kind': 'guard', 'label': '護衛'}

    vis = _visible_channels(room, me)
    rows = await execute_query(
        'SELECT id, channel, name, seat, text, created FROM werewolf_messages '
        'WHERE room_code = ? AND id > ? ORDER BY id LIMIT 200', [code, since])
    last_id = since
    messages = []
    for r in rows:
        last_id = max(last_id, r['id'])
        if r['channel'] in vis:
            messages.append({'id': r['id'], 'channel': r['channel'], 'name': r['name'],
                             'seat': r['seat'], 'text': r['text'], 't': r['created']})

    composition = None
    if playing or finished:
        composition = _composition_text(role_set(len(players)))

    return {
        'success': True,
        'room': {
            'room_code': code, 'status': room['status'], 'phase': room['phase'],
            'day_no': room['day_no'], 'remaining': remaining, 'revote': bool(room['revote']),
            'candidates': candidates, 'winner': room['winner'],
            'discuss_sec': room['discuss_sec'], 'composition': composition,
            'min_players': MIN_PLAYERS, 'max_players': MAX_PLAYERS,
        },
        'players': out_players,
        'me': my,
        'can_act': can_act,
        'can_post': _can_post(room, me),
        'messages': messages,
        'last_id': last_id,
    }


async def _player_action(request: Request, code: str, column: str, phase: str, kind: str):
    """夜の行動(action_target)・投票(vote_target)の共通処理。"""
    code = code.upper()
    room, players = await _advance(code)
    if not room:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    me = _find_me(players, _game_token(request))
    if not me:
        return json_resp({'success': False, 'error': '参加していません'}, 403)
    if room['status'] != 'playing' or room['phase'] != phase:
        return json_resp({'success': False, 'error': '今は操作できません'}, 409)
    if not me['alive']:
        return json_resp({'success': False, 'error': '死亡したプレイヤーは操作できません'}, 403)

    body = await _body(request)
    try:
        target = int(body.get('target'))
    except (TypeError, ValueError):
        return json_resp({'success': False, 'error': '対象が不正です'}, 400)
    tp = next((p for p in players if p['seat'] == target), None)
    if not tp or not tp['alive'] or tp['token'] == me['token']:
        return json_resp({'success': False, 'error': '選べない相手です'}, 400)

    if phase == 'night':
        first = room['day_no'] == 0
        role = me['role']
        if not (role == 'seer' or (not first and role in ('werewolf', 'knight'))):
            return json_resp({'success': False, 'error': 'あなたは今夜行動できません'}, 403)
        if role == 'werewolf' and tp['role'] == 'werewolf':
            return json_resp({'success': False, 'error': '仲間は襲撃できません'}, 400)
    else:
        if room['revote'] and target not in json.loads(room.get('candidates') or '[]'):
            return json_resp({'success': False, 'error': '決選投票の候補から選んでください'}, 400)

    ok = await _tx([(
        f'''UPDATE werewolf_players SET {column} = ?
            WHERE room_code = ? AND token = ? AND alive = 1
              AND EXISTS (SELECT 1 FROM werewolf_rooms WHERE room_code = ? AND phase = ? AND phase_no = ?)''',
        [target, code, me['token'], code, phase, room['phase_no']])], cas=True)
    if not ok:
        return json_resp({'success': False, 'error': 'タイミングが合いませんでした。もう一度お試しください'}, 409)
    await _advance(code)
    return {'success': True}


@router.post('/werewolf/{room_code}/action')
async def werewolf_action(request: Request, room_code: str):
    return await _player_action(request, room_code, 'action_target', 'night', 'action')


@router.post('/werewolf/{room_code}/vote')
async def werewolf_vote(request: Request, room_code: str):
    return await _player_action(request, room_code, 'vote_target', 'vote', 'vote')


@router.post('/werewolf/{room_code}/skip')
async def werewolf_skip(request: Request, room_code: str):
    """昼の議論を「もう十分」と感じた人が押す。生存者全員が押すと投票へ進む。"""
    code = room_code.upper()
    room, players = await _advance(code)
    if not room:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    me = _find_me(players, _game_token(request))
    if not me or not me['alive'] or room['status'] != 'playing' or room['phase'] != 'day':
        return json_resp({'success': False, 'error': '今は操作できません'}, 409)
    ok = await _tx([(
        '''UPDATE werewolf_players SET ready = 1 WHERE room_code = ? AND token = ?
           AND EXISTS (SELECT 1 FROM werewolf_rooms WHERE room_code = ? AND phase = 'day' AND phase_no = ?)''',
        [code, me['token'], code, room['phase_no']])], cas=True)
    if not ok:
        return json_resp({'success': False, 'error': 'タイミングが合いませんでした'}, 409)
    await _advance(code)
    return {'success': True}


@router.post('/werewolf/{room_code}/chat')
async def werewolf_chat(request: Request, room_code: str):
    code = room_code.upper()
    room, players = await _advance(code)
    if not room:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    me = _find_me(players, _game_token(request))
    allowed = _can_post(room, me)
    body = await _body(request)
    channel = body.get('channel') or (allowed[0] if allowed else None)
    if channel not in allowed:
        return json_resp({'success': False, 'error': '今は発言できません'}, 403)
    text = str(body.get('text') or '').strip()
    if not text:
        return json_resp({'success': False, 'error': '本文が空です'}, 400)
    text = html.escape(filter_ng_words(text[:CHAT_MAX_LEN]))

    last = await execute_query(
        'SELECT created FROM werewolf_messages WHERE room_code = ? AND token = ? ORDER BY id DESC LIMIT 1',
        [code, me['token']])
    if last and time.time() - (last[0]['created'] or 0) < CHAT_MIN_INTERVAL:
        return json_resp({'success': False, 'error': '連投はできません'}, 429)
    await execute_query(
        'INSERT INTO werewolf_messages (room_code, channel, token, name, seat, text, created) '
        'VALUES (?,?,?,?,?,?,?)',
        [code, channel, me['token'], me['name'], me['seat'], text, time.time()])
    return {'success': True}


GAME = Game(
    key='werewolf', label='人狼', router=router, lobby_path='/werewolf',
    ensure_schema=ensure_schema,
)
