"""オセロ(2人対戦)。ロジックとルート。登録は battle_games/__init__.py の register_all() から。"""
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


def _initial_othello():
    b = ['.'] * 64
    b[3 * 8 + 3] = 'W'; b[3 * 8 + 4] = 'B'; b[4 * 8 + 3] = 'B'; b[4 * 8 + 4] = 'W'
    return ''.join(b)


def _othello_valid(board, player):
    opp = 'W' if player == 'B' else 'B'
    dirs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    out = []
    for r in range(8):
        for c in range(8):
            if board[r * 8 + c] != '.':
                continue
            ok = False
            for dr, dc in dirs:
                rr, cc = r + dr, c + dc; seen = False
                while 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == opp:
                    seen = True; rr += dr; cc += dc
                if seen and 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == player:
                    ok = True; break
            if ok:
                out.append((r, c))
    return out


def _othello_apply(board, player, r, c):
    if (r, c) not in _othello_valid(board, player):
        return None
    a = list(board); a[r * 8 + c] = player; opp = 'W' if player == 'B' else 'B'
    dirs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for dr, dc in dirs:
        rr, cc = r + dr, c + dc; flips = []
        while 0 <= rr < 8 and 0 <= cc < 8 and a[rr * 8 + cc] == opp:
            flips.append((rr, cc)); rr += dr; cc += dc
        if flips and 0 <= rr < 8 and 0 <= cc < 8 and a[rr * 8 + cc] == player:
            for fr, fc in flips:
                a[fr * 8 + fc] = player
    return ''.join(a)


@router.get('/game')
async def game_lobby(request: Request):
    resp = templates.TemplateResponse(request, 'game.html', {'room': None, 'my_color': None})
    return _cookie_response(request, resp, _game_token(request))


@router.post('/game/create')
async def game_create(request: Request):
    token = _game_token(request)
    name = await _game_name(request)
    member_id = await _game_member_id(request)
    code = await _new_room_code()
    now = datetime.utcnow().isoformat()
    await execute_query(
        '''INSERT INTO othello_rooms
           (room_code, black_token, black_name, black_member_id, white_token, white_name, board, turn, status, winner, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _initial_othello(), 'B', 'waiting', None, now, now]
    )
    resp = RedirectResponse(url=f'/game/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@router.get('/game/{room_code}')
async def game_room(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return RedirectResponse(url='/game')
    room = rows[0]
    token = _game_token(request)
    my_color = 'B' if room.get('black_token') == token else ('W' if room.get('white_token') == token else None)

    # 招待リンクを踏んだ2人目をその場で自動参加させる
    if my_color is None and not room.get('white_token') and room.get('black_token') != token:
        name = await _game_name(request)
        await execute_query(
            'UPDATE othello_rooms SET white_token=?,white_name=?,white_member_id=?,status=?,updated_at=? '
            'WHERE room_code=? AND white_token IS NULL',
            [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
        )
        rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
        room = rows[0]
        my_color = 'W' if room.get('white_token') == token else my_color

    resp = templates.TemplateResponse(request, 'game.html', {'room': room, 'my_color': my_color})
    return _cookie_response(request, resp, token)


@router.post('/game/{room_code}/join')
async def game_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    room = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if room.get('black_token') == token or room.get('white_token') == token:
        return {'success': True}
    if room.get('white_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    await execute_query(
        'UPDATE othello_rooms SET white_token=?,white_name=?,white_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@router.get('/api/game/{room_code}/state')
async def game_state(request: Request, room_code: str):
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return json_resp({'error': 'not found'}, 404)
    r = rows[0]
    token = _game_token(request)
    my = 'B' if r.get('black_token') == token else ('W' if r.get('white_token') == token else None)
    board = r['board']
    black_count = board.count('B')
    white_count = board.count('W')
    valid_moves = _othello_valid(board, r['turn']) if r['status'] == 'playing' else []
    return {
        'success': True,
        'room_code': r['room_code'],
        'board': board,
        'turn': r['turn'],
        'status': r['status'],
        'winner': r['winner'],
        'black_name': r.get('black_name') or '名無しさん',
        'white_name': r.get('white_name') or '名無しさん',
        'black_id': (r.get('black_token') or '')[:4],
        'white_id': (r.get('white_token') or '')[:4],
        'has_white': bool(r.get('white_token')),
        'my_color': my,
        'black_count': black_count,
        'white_count': white_count,
        'valid_moves': valid_moves,
    }


@router.post('/game/{room_code}/move')
async def game_move(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    player = 'B' if r.get('black_token') == token else ('W' if r.get('white_token') == token else None)
    if not player:
        return json_resp({'success': False, 'error': '観戦者は着手できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}
    if r['turn'] != player:
        return {'success': False, 'error': '相手のターンです'}

    body = await get_json_silent(request)
    row = int(body.get('row', -1))
    col = int(body.get('col', -1))
    new_board = _othello_apply(r['board'], player, row, col)
    if new_board is None:
        return {'success': False, 'error': 'そこには置けません'}

    opponent = 'W' if player == 'B' else 'B'
    next_turn = opponent
    status = 'playing'
    winner = None
    if not _othello_valid(new_board, opponent):
        if _othello_valid(new_board, player):
            next_turn = player
        else:
            status = 'finished'
            black_count = new_board.count('B')
            white_count = new_board.count('W')
            winner = 'B' if black_count > white_count else ('W' if white_count > black_count else 'draw')
            black_key = _rating_key(r.get('black_member_id'), r.get('black_token'))
            white_key = _rating_key(r.get('white_member_id'), r.get('white_token'))
            result_black = 1 if winner == 'B' else (0 if winner == 'W' else 0.5)
            await apply_game_result('othello', black_key, r.get('black_name') or '名無しさん',
                               white_key, r.get('white_name') or '名無しさん', result_black)
            await _award_game_result_points('othello', code,
                                             r.get('black_member_id'), result_black,
                                             r.get('white_member_id'), 1 - result_black)

    now = datetime.utcnow().isoformat()
    await execute_query(
        'UPDATE othello_rooms SET board=?,turn=?,status=?,winner=?,updated_at=? WHERE room_code=? AND turn=?',
        [new_board, next_turn, status, winner, now, code, player]
    )
    if status == 'finished':
        await _game_chat_close('othello', code)
    return {'success': True}


@router.post('/game/{room_code}/resign')
async def game_resign(request: Request, room_code: str):
    """オセロ: 対局中の参加者が降参する。相手の勝ちとして終了し、レーティングは通常の敗北と同じ。"""
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM othello_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    player = 'B' if r.get('black_token') == token else ('W' if r.get('white_token') == token else None)
    if not player:
        return json_resp({'success': False, 'error': '観戦者は降参できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}

    winner = 'W' if player == 'B' else 'B'
    # 先に「終了」へ更新できた1回だけが処理を続ける(二重送信・相手の着手との競合で二重処理しない)
    if not await _finish_room_if_playing('othello_rooms', code, winner):
        return {'success': False, 'error': '対局は終了しています'}

    black_key = _rating_key(r.get('black_member_id'), r.get('black_token'))
    white_key = _rating_key(r.get('white_member_id'), r.get('white_token'))
    result_black = 1 if winner == 'B' else 0
    await apply_game_result('othello', black_key, r.get('black_name') or '名無しさん',
                            white_key, r.get('white_name') or '名無しさん', result_black)
    resigner_id = r.get('black_member_id') if player == 'B' else r.get('white_member_id')
    winner_id = r.get('white_member_id') if player == 'B' else r.get('black_member_id')
    await _award_game_resign_result('othello', code, resigner_id, winner_id)
    await _game_chat_close('othello', code)
    return {'success': True}


GAME = Game(
    key='othello', label='オセロ', router=router, lobby_path='/game',
    room_meta={'table': 'othello_rooms', 'a': 'black', 'b': 'white', 'path': '/game/',
               'label_a': '黒', 'label_b': '白'},
    has_in_check_col=False,
)
