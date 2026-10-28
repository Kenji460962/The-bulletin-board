"""将棋(2人対戦)。ロジックとルート。登録は battle_games/__init__.py の register_all() から。"""
import json
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from game_service import (
    Game,
    _award_game_resign_result,
    _award_game_result_points,
    _cookie_response,
    _finish_room_if_playing,
    _game_chat_close,
    _game_member_id,
    _game_name,
    _game_token,
    _new_room_code,
    _rating_key,
    apply_game_result,
    execute_query,
    get_json_silent,
    json_resp,
    templates,
)

router = APIRouter()


SHOGI_HAND_TYPES = ['P', 'L', 'N', 'S', 'G', 'B', 'R']
SHOGI_PROMOTABLE = ('P', 'L', 'N', 'S', 'B', 'R')


def _shogi_empty_hands():
    return {'s': {t: 0 for t in SHOGI_HAND_TYPES}, 'g': {t: 0 for t in SHOGI_HAND_TYPES}}


def _initial_shogi_hands_json():
    return json.dumps(_shogi_empty_hands())


def _initial_shogi_board():
    board = [''] * 81
    back_rank = ['L', 'N', 'S', 'G', 'K', 'G', 'S', 'N', 'L']
    for c in range(9):
        board[0 * 9 + c] = 'g' + back_rank[c]
        board[8 * 9 + c] = 's' + back_rank[c]
        board[2 * 9 + c] = 'gP'
        board[6 * 9 + c] = 'sP'
    board[1 * 9 + 1] = 'gR'
    board[1 * 9 + 7] = 'gB'
    board[7 * 9 + 1] = 'sB'
    board[7 * 9 + 7] = 'sR'
    return json.dumps(board)


def _shogi_forward(color):
    return -1 if color == 's' else 1


def _shogi_zone(color, r):
    return r <= 2 if color == 's' else r >= 6


def _shogi_forced_promotion(base_typ, color, tr):
    if base_typ in ('P', 'L'):
        return tr == (0 if color == 's' else 8)
    if base_typ == 'N':
        return tr <= 1 if color == 's' else tr >= 7
    return False


def _shogi_piece_moves(board, r, c):
    p = board[r * 9 + c]
    if not p:
        return []
    color, typ = p[0], p[1:]
    out = []

    def step(dr, dc):
        rr, cc = r + dr, c + dc
        if 0 <= rr < 9 and 0 <= cc < 9:
            t = board[rr * 9 + cc]
            if not t or t[0] != color:
                out.append((rr, cc))

    def slide(dr, dc):
        rr, cc = r + dr, c + dc
        while 0 <= rr < 9 and 0 <= cc < 9:
            t = board[rr * 9 + cc]
            if not t:
                out.append((rr, cc))
            else:
                if t[0] != color:
                    out.append((rr, cc))
                break
            rr += dr; cc += dc

    f = _shogi_forward(color)
    if typ == 'P':
        step(f, 0)
    elif typ == 'L':
        slide(f, 0)
    elif typ == 'N':
        step(2 * f, -1); step(2 * f, 1)
    elif typ == 'S':
        for d in [(f, 0), (f, -1), (f, 1), (-f, -1), (-f, 1)]:
            step(*d)
    elif typ in ('G', '+P', '+L', '+N', '+S'):
        for d in [(f, 0), (f, -1), (f, 1), (0, -1), (0, 1), (-f, 0)]:
            step(*d)
    elif typ == 'K':
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                step(dr, dc)
    elif typ == 'B':
        for d in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            slide(*d)
    elif typ == 'R':
        for d in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            slide(*d)
    elif typ == '+B':
        for d in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            slide(*d)
        for d in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            step(*d)
    elif typ == '+R':
        for d in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            slide(*d)
        for d in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            step(*d)
    return out


def _shogi_find_king(board, color):
    target = color + 'K'
    for i, p in enumerate(board):
        if p == target:
            return i // 9, i % 9
    return None


def _shogi_attacked(board, r, c, by_color):
    for i, p in enumerate(board):
        if p and p[0] == by_color:
            rr, cc = i // 9, i % 9
            if (r, c) in _shogi_piece_moves(board, rr, cc):
                return True
    return False


def _shogi_in_check(board, color):
    pos = _shogi_find_king(board, color)
    if not pos:
        return False
    r, c = pos
    opp = 'g' if color == 's' else 's'
    return _shogi_attacked(board, r, c, opp)


def _shogi_apply_move(board, r, c, tr, tc, promote=False):
    nb = board[:]
    piece = nb[r * 9 + c]
    color, typ = piece[0], piece[1:]
    nb[r * 9 + c] = ''
    new_typ = typ
    if promote and typ in SHOGI_PROMOTABLE:
        new_typ = '+' + typ
    nb[tr * 9 + tc] = color + new_typ
    return nb


def _shogi_drop_allowed(board, color, ptype, r, c):
    if board[r * 9 + c]:
        return False
    if ptype == 'P':
        last_row = 0 if color == 's' else 8
        if r == last_row:
            return False
        for rr in range(9):
            if board[rr * 9 + c] == color + 'P':
                return False  # 二歩
    elif ptype == 'L':
        last_row = 0 if color == 's' else 8
        if r == last_row:
            return False
    elif ptype == 'N':
        if color == 's' and r <= 1:
            return False
        if color == 'g' and r >= 7:
            return False
    return True


def _shogi_legal_board_moves(board, color):
    moves = []
    for i, p in enumerate(board):
        if p and p[0] == color:
            r, c = i // 9, i % 9
            for tr, tc in _shogi_piece_moves(board, r, c):
                simulated = _shogi_apply_move(board, r, c, tr, tc, promote=False)
                if not _shogi_in_check(simulated, color):
                    moves.append((r, c, tr, tc))
    return moves


def _shogi_legal_drop_moves(board, color, hand):
    moves = []
    for ptype, count in (hand or {}).items():
        if count <= 0:
            continue
        for i, p in enumerate(board):
            if p:
                continue
            r, c = i // 9, i % 9
            if not _shogi_drop_allowed(board, color, ptype, r, c):
                continue
            nb = board[:]
            nb[i] = color + ptype
            if not _shogi_in_check(nb, color):
                moves.append((ptype, r, c))
    return moves


@router.get('/shogi')
async def shogi_lobby(request: Request):
    resp = templates.TemplateResponse(request, 'syogi.html', {'room': None, 'my_color': None})
    return _cookie_response(request, resp, _game_token(request))


@router.post('/shogi/create')
async def shogi_create(request: Request):
    token = _game_token(request)
    name = await _game_name(request)
    member_id = await _game_member_id(request)
    code = await _new_room_code()
    now = datetime.utcnow().isoformat()
    await execute_query(
        '''INSERT INTO shogi_rooms
           (room_code, sente_token, sente_name, sente_member_id, gote_token, gote_name, board, hands, turn, status, winner, in_check, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _initial_shogi_board(), _initial_shogi_hands_json(), 's', 'waiting', None, None, now, now]
    )
    resp = RedirectResponse(url=f'/shogi/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@router.get('/shogi/{room_code}')
async def shogi_room(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return RedirectResponse(url='/shogi')
    r = rows[0]
    token = _game_token(request)
    my = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)

    # 招待リンクを踏んだ2人目をその場で自動参加させる
    if my is None and not r.get('gote_token') and r.get('sente_token') != token:
        name = await _game_name(request)
        await execute_query(
            'UPDATE shogi_rooms SET gote_token=?,gote_name=?,gote_member_id=?,status=?,updated_at=? '
            'WHERE room_code=? AND gote_token IS NULL',
            [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
        )
        rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
        r = rows[0]
        my = 'g' if r.get('gote_token') == token else my

    resp = templates.TemplateResponse(request, 'syogi.html', {'room': r, 'my_color': my})
    return _cookie_response(request, resp, token)


@router.post('/shogi/{room_code}/join')
async def shogi_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if r.get('sente_token') == token or r.get('gote_token') == token:
        return {'success': True}
    if r.get('gote_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    await execute_query(
        'UPDATE shogi_rooms SET gote_token=?,gote_name=?,gote_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@router.get('/api/shogi/{room_code}/state')
async def shogi_state(request: Request, room_code: str):
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return json_resp({'error': 'not found'}, 404)
    r = rows[0]
    token = _game_token(request)
    my = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)

    try:
        board_data = json.loads(r['board'])
    except Exception:
        board_data = []
    try:
        hands_data = json.loads(r['hands'])
    except Exception:
        hands_data = _shogi_empty_hands()

    return {
        'success': True,
        'room_code': r['room_code'],
        'board': board_data,
        'hands': hands_data,
        'turn': r['turn'],
        'status': r['status'],
        'winner': r['winner'],
        'in_check': r['in_check'],
        'sente_name': r.get('sente_name') or '名無しさん',
        'gote_name': r.get('gote_name') or '名無しさん',
        'sente_id': (r.get('sente_token') or '')[:4],
        'gote_id': (r.get('gote_token') or '')[:4],
        'has_gote': bool(r.get('gote_token')),
        'my_color': my,
    }


@router.post('/shogi/{room_code}/move')
async def shogi_move(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)
    if not color:
        return json_resp({'success': False, 'error': '観戦者は着手できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != color:
        return {'success': False, 'error': '相手のターンです'}

    body = await get_json_silent(request)
    try:
        fr, fc, tr, tc = [int(body[k]) for k in ('from_row', 'from_col', 'to_row', 'to_col')]
    except Exception:
        return json_resp({'success': False, 'error': '着手情報が不正です'}, 400)
    if not all(0 <= x < 9 for x in (fr, fc, tr, tc)):
        return json_resp({'success': False, 'error': '着手位置が不正です'}, 400)
    want_promote = bool(body.get('promote'))

    try:
        board = json.loads(r['board'])
    except Exception:
        return json_resp({'success': False, 'error': '盤面データの読み込みに失敗しました'}, 500)
    try:
        hands = json.loads(r['hands'])
    except Exception:
        hands = _shogi_empty_hands()

    piece = board[fr * 9 + fc]
    if not piece or piece[0] != color:
        return {'success': False, 'error': '自分の駒を選んでください'}
    typ = piece[1:]
    if (tr, tc) not in _shogi_piece_moves(board, fr, fc):
        return {'success': False, 'error': 'その駒はそこへ動かせません'}

    is_already_promoted = typ.startswith('+')
    base_typ = typ[1:] if is_already_promoted else typ
    promote = False
    if not is_already_promoted and base_typ in SHOGI_PROMOTABLE:
        if _shogi_forced_promotion(base_typ, color, tr):
            promote = True
        elif want_promote and (_shogi_zone(color, fr) or _shogi_zone(color, tr)):
            promote = True

    # 王手放置チェック用のシミュレーション(成りは玉の安全性に影響しないためpromote=Falseで判定)
    simulated = _shogi_apply_move(board, fr, fc, tr, tc, promote=False)
    if _shogi_in_check(simulated, color):
        return {'success': False, 'error': 'その手を指すと自分の王が王手にさらされます'}

    captured = board[tr * 9 + tc]
    board = _shogi_apply_move(board, fr, fc, tr, tc, promote=promote)

    if captured:
        cap_typ = captured[1:]
        cap_base = cap_typ[1:] if cap_typ.startswith('+') else cap_typ
        hands.setdefault(color, {t: 0 for t in SHOGI_HAND_TYPES})
        hands[color][cap_base] = hands[color].get(cap_base, 0) + 1

    next_color = 'g' if color == 's' else 's'
    next_in_check = _shogi_in_check(board, next_color)
    next_has_moves = bool(_shogi_legal_board_moves(board, next_color)) or bool(_shogi_legal_drop_moves(board, next_color, hands.get(next_color, {})))

    new_status = r['status']
    winner = r.get('winner')
    if not next_has_moves:
        # 詰み(合法手が1つもない)は着手した側の勝ち
        new_status = 'finished'
        winner = color
        sente_key = _rating_key(r.get('sente_member_id'), r.get('sente_token'))
        gote_key = _rating_key(r.get('gote_member_id'), r.get('gote_token'))
        result_sente = 1 if winner == 's' else 0
        await apply_game_result('shogi', sente_key, r.get('sente_name') or '名無しさん',
                           gote_key, r.get('gote_name') or '名無しさん', result_sente)
        await _award_game_result_points('shogi', code,
                                         r.get('sente_member_id'), result_sente,
                                         r.get('gote_member_id'), 1 - result_sente)

    now = datetime.utcnow().isoformat()
    await execute_query(
        'UPDATE shogi_rooms SET board=?,hands=?,turn=?,updated_at=?,in_check=?,status=?,winner=? WHERE room_code=? AND turn=?',
        [json.dumps(board), json.dumps(hands), next_color, now, (next_color if next_in_check else None), new_status, winner, code, color]
    )
    if new_status == 'finished':
        await _game_chat_close('shogi', code)
    return {'success': True}


@router.post('/shogi/{room_code}/resign')
async def shogi_resign(request: Request, room_code: str):
    """将棋: 対局中の参加者が投了(降参)する。"""
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)
    if not color:
        return json_resp({'success': False, 'error': '観戦者は投了できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}

    winner = 'g' if color == 's' else 's'
    if not await _finish_room_if_playing('shogi_rooms', code, winner):
        return {'success': False, 'error': '対局は終了しています'}

    sente_key = _rating_key(r.get('sente_member_id'), r.get('sente_token'))
    gote_key = _rating_key(r.get('gote_member_id'), r.get('gote_token'))
    result_sente = 1 if winner == 's' else 0
    await apply_game_result('shogi', sente_key, r.get('sente_name') or '名無しさん',
                            gote_key, r.get('gote_name') or '名無しさん', result_sente)
    resigner_id = r.get('sente_member_id') if color == 's' else r.get('gote_member_id')
    winner_id = r.get('gote_member_id') if color == 's' else r.get('sente_member_id')
    await _award_game_resign_result('shogi', code, resigner_id, winner_id)
    await _game_chat_close('shogi', code)
    return {'success': True}


@router.post('/shogi/{room_code}/drop')
async def shogi_drop(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM shogi_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 's' if r.get('sente_token') == token else ('g' if r.get('gote_token') == token else None)
    if not color:
        return json_resp({'success': False, 'error': '観戦者は着手できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != color:
        return {'success': False, 'error': '相手のターンです'}

    body = await get_json_silent(request)
    ptype = str(body.get('piece', '')).upper()
    try:
        tr, tc = int(body['row']), int(body['col'])
    except Exception:
        return json_resp({'success': False, 'error': '着手情報が不正です'}, 400)
    if ptype not in SHOGI_HAND_TYPES:
        return json_resp({'success': False, 'error': '不正な駒です'}, 400)
    if not (0 <= tr < 9 and 0 <= tc < 9):
        return json_resp({'success': False, 'error': '着手位置が不正です'}, 400)

    try:
        board = json.loads(r['board'])
    except Exception:
        return json_resp({'success': False, 'error': '盤面データの読み込みに失敗しました'}, 500)
    try:
        hands = json.loads(r['hands'])
    except Exception:
        hands = _shogi_empty_hands()

    if hands.get(color, {}).get(ptype, 0) <= 0:
        return {'success': False, 'error': 'その持ち駒はありません'}
    if not _shogi_drop_allowed(board, color, ptype, tr, tc):
        return {'success': False, 'error': 'そこには打てません'}

    new_board = board[:]
    new_board[tr * 9 + tc] = color + ptype
    if _shogi_in_check(new_board, color):
        return {'success': False, 'error': 'その手を指すと自分の王が王手にさらされます'}

    hands[color][ptype] -= 1

    next_color = 'g' if color == 's' else 's'
    next_in_check = _shogi_in_check(new_board, next_color)
    next_has_moves = bool(_shogi_legal_board_moves(new_board, next_color)) or bool(_shogi_legal_drop_moves(new_board, next_color, hands.get(next_color, {})))

    new_status = r['status']
    winner = r.get('winner')
    if not next_has_moves:
        new_status = 'finished'
        winner = color
        sente_key = _rating_key(r.get('sente_member_id'), r.get('sente_token'))
        gote_key = _rating_key(r.get('gote_member_id'), r.get('gote_token'))
        result_sente = 1 if winner == 's' else 0
        await apply_game_result('shogi', sente_key, r.get('sente_name') or '名無しさん',
                           gote_key, r.get('gote_name') or '名無しさん', result_sente)
        await _award_game_result_points('shogi', code,
                                         r.get('sente_member_id'), result_sente,
                                         r.get('gote_member_id'), 1 - result_sente)

    now = datetime.utcnow().isoformat()
    await execute_query(
        'UPDATE shogi_rooms SET board=?,hands=?,turn=?,updated_at=?,in_check=?,status=?,winner=? WHERE room_code=? AND turn=?',
        [json.dumps(new_board), json.dumps(hands), next_color, now, (next_color if next_in_check else None), new_status, winner, code, color]
    )
    if new_status == 'finished':
        await _game_chat_close('shogi', code)
    return {'success': True}


GAME = Game(
    key='shogi', label='将棋', router=router, lobby_path='/shogi',
    room_meta={'table': 'shogi_rooms', 'a': 'sente', 'b': 'gote', 'path': '/shogi/',
               'label_a': '先手', 'label_b': '後手'},
    has_in_check_col=True,
)
