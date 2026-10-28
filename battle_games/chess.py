"""チェス(2人対戦)。ロジックとルート。登録は battle_games/__init__.py の register_all() から。"""
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


def _initial_chess():
    # 64要素のリストを作成し、JSON文字列にシリアライズして返す
    board_list = [
        'bR', 'bN', 'bB', 'bQ', 'bK', 'bB', 'bN', 'bR',
        'bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        'wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP',
        'wR', 'wN', 'wB', 'wQ', 'wK', 'wB', 'wN', 'wR'
    ]
    return json.dumps(board_list)


def _chess_board():
    return _initial_chess()


def _chess_pseudo(board, r, c, castling='', en_passant=None):
    p = board[r * 8 + c]
    if not p:
        return []
    color, typ = p[0], p[1]; out = []
    dirs = []
    if typ == 'N':
        dirs = [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]
    elif typ == 'K':
        dirs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    elif typ in 'BRQ':
        if typ in 'BQ':
            dirs += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
        if typ in 'RQ':
            dirs += [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if typ in 'NK':
        for dr, dc in dirs:
            rr, cc = r + dr, c + dc
            if 0 <= rr < 8 and 0 <= cc < 8 and (not board[rr * 8 + cc] or board[rr * 8 + cc][0] != color):
                out.append((rr, cc))
    elif typ in 'BRQ':
        for dr, dc in dirs:
            rr, cc = r + dr, c + dc
            while 0 <= rr < 8 and 0 <= cc < 8:
                t = board[rr * 8 + cc]
                if not t:
                    out.append((rr, cc))
                else:
                    if t[0] != color:
                        out.append((rr, cc))
                    break
                rr += dr; cc += dc
    elif typ == 'P':
        d = -1 if color == 'w' else 1; start = 6 if color == 'w' else 1
        rr = r + d
        if 0 <= rr < 8 and not board[rr * 8 + c]:
            out.append((rr, c))
            rr2 = r + 2 * d
            if r == start and not board[rr2 * 8 + c]:
                out.append((rr2, c))
        for dc in (-1, 1):
            rr, cc = r + d, c + dc
            if 0 <= rr < 8 and 0 <= cc < 8:
                if board[rr * 8 + cc] and board[rr * 8 + cc][0] != color:
                    out.append((rr, cc))
                elif en_passant and en_passant == (rr, cc):
                    out.append((rr, cc))

    if typ == 'K':
        row = 7 if color == 'w' else 0
        if r == row and c == 4:
            k_flag = 'K' if color == 'w' else 'k'
            q_flag = 'Q' if color == 'w' else 'q'
            opp = 'b' if color == 'w' else 'w'
            if (k_flag in castling and not board[row * 8 + 5] and not board[row * 8 + 6]
                    and board[row * 8 + 7] == color + 'R'
                    and not _chess_attacked(board, row, 4, opp)
                    and not _chess_attacked(board, row, 5, opp)
                    and not _chess_attacked(board, row, 6, opp)):
                out.append((row, 6))
            if (q_flag in castling and not board[row * 8 + 3] and not board[row * 8 + 2] and not board[row * 8 + 1]
                    and board[row * 8 + 0] == color + 'R'
                    and not _chess_attacked(board, row, 4, opp)
                    and not _chess_attacked(board, row, 3, opp)
                    and not _chess_attacked(board, row, 2, opp)):
                out.append((row, 2))
    return out


def _chess_find_king(board, color):
    target = color + 'K'
    for i, p in enumerate(board):
        if p == target:
            return i // 8, i % 8
    return None


def _chess_attacked(board, r, c, by_color):
    # ポーンの攻撃
    d = 1 if by_color == 'w' else -1
    for dc in (-1, 1):
        pr, pc = r + d, c + dc
        if 0 <= pr < 8 and 0 <= pc < 8 and board[pr * 8 + pc] == by_color + 'P':
            return True
    # ナイトの攻撃
    for dr, dc in [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]:
        rr, cc = r + dr, c + dc
        if 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == by_color + 'N':
            return True
    # 王の攻撃(隣接マス)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, c + dc
            if 0 <= rr < 8 and 0 <= cc < 8 and board[rr * 8 + cc] == by_color + 'K':
                return True
    # 直線(ルーク・クイーン)
    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        rr, cc = r + dr, c + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            p = board[rr * 8 + cc]
            if p:
                if p[0] == by_color and p[1] in ('R', 'Q'):
                    return True
                break
            rr += dr; cc += dc
    # 斜め(ビショップ・クイーン)
    for dr, dc in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
        rr, cc = r + dr, c + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            p = board[rr * 8 + cc]
            if p:
                if p[0] == by_color and p[1] in ('B', 'Q'):
                    return True
                break
            rr += dr; cc += dc
    return False


def _chess_in_check(board, color):
    pos = _chess_find_king(board, color)
    if not pos:
        return False
    r, c = pos
    opp = 'b' if color == 'w' else 'w'
    return _chess_attacked(board, r, c, opp)


def _chess_apply(board, r, c, tr, tc):
   
    nb = board[:]
    piece = nb[r * 8 + c]
    color, typ = piece[0], piece[1]
    nb[r * 8 + c] = ''

    if typ == 'K' and abs(tc - c) == 2:
       
        nb[tr * 8 + tc] = piece
        row = r
        if tc == 6:
            nb[row * 8 + 7] = ''
            nb[row * 8 + 5] = color + 'R'
        elif tc == 2:
            nb[row * 8 + 0] = ''
            nb[row * 8 + 3] = color + 'R'
    elif typ == 'P' and c != tc and not board[tr * 8 + tc]:
        nb[tr * 8 + tc] = piece
        nb[r * 8 + tc] = ''
    elif typ == 'P' and tr in (0, 7):
        nb[tr * 8 + tc] = color + 'Q'
    else:
        nb[tr * 8 + tc] = piece
    return nb


def _chess_update_castling_rights(castling, typ, color, r, c, tr, tc):
    new_castling = castling or ''
    if typ == 'K':
        new_castling = new_castling.replace('K', '').replace('Q', '') if color == 'w' else new_castling.replace('k', '').replace('q', '')
    for (rr, cc), flag in [((7, 0), 'Q'), ((7, 7), 'K'), ((0, 0), 'q'), ((0, 7), 'k')]:
        if (r, c) == (rr, cc) or (tr, tc) == (rr, cc):
            new_castling = new_castling.replace(flag, '')
    return new_castling


def _chess_legal_moves(board, color, castling='', en_passant=None):
    moves = []
    for i, p in enumerate(board):
        if p and p[0] == color:
            r, c = i // 8, i % 8
            for tr, tc in _chess_pseudo(board, r, c, castling, en_passant):
                simulated = _chess_apply(board, r, c, tr, tc)
                if not _chess_in_check(simulated, color):
                    moves.append((r, c, tr, tc))
    return moves


@router.get('/chess')
async def chess_lobby(request: Request):
    resp = templates.TemplateResponse(request, 'chess.html', {'room': None, 'my_color': None})
    return _cookie_response(request, resp, _game_token(request))


@router.post('/chess/create')
async def chess_create(request: Request):
    token = _game_token(request)
    name = await _game_name(request)
    member_id = await _game_member_id(request)
    code = await _new_room_code()
    now = datetime.utcnow().isoformat()
    await execute_query(
        '''INSERT INTO chess_rooms
           (room_code, white_token, white_name, white_member_id, black_token, black_name, board, turn, status, winner, in_check, castling, en_passant, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [code, token, name, member_id, None, None, _chess_board(), 'w', 'waiting', None, None, 'KQkq', None, now, now]
    )
    resp = RedirectResponse(url=f'/chess/{code}', status_code=303)
    return _cookie_response(request, resp, token)


@router.get('/chess/{room_code}')
async def chess_room(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return RedirectResponse(url='/chess')
    r = rows[0]
    token = _game_token(request)
    my = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)

    # 招待リンクを踏んだ2人目をその場で自動参加させる
    if my is None and not r.get('black_token') and r.get('white_token') != token:
        name = await _game_name(request)
        await execute_query(
            'UPDATE chess_rooms SET black_token=?,black_name=?,black_member_id=?,status=?,updated_at=? '
            'WHERE room_code=? AND black_token IS NULL',
            [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
        )
        rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
        r = rows[0]
        my = 'b' if r.get('black_token') == token else my

    resp = templates.TemplateResponse(request, 'chess.html', {'room': r, 'my_color': my})
    return _cookie_response(request, resp, token)


@router.post('/chess/{room_code}/join')
async def chess_join(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    name = await _game_name(request)
    if r.get('white_token') == token or r.get('black_token') == token:
        return {'success': True}
    if r.get('black_token'):
        return json_resp({'success': False, 'error': 'この部屋は満員です'}, 409)
    await execute_query(
        'UPDATE chess_rooms SET black_token=?,black_name=?,black_member_id=?,status=?,updated_at=? WHERE room_code=?',
        [token, name, await _game_member_id(request), 'playing', datetime.utcnow().isoformat(), code]
    )
    return {'success': True}


@router.get('/api/chess/{room_code}/state')
async def chess_state(request: Request, room_code: str):
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [room_code.upper()])
    if not rows:
        return json_resp({'error': 'not found'}, 404)
    r = rows[0]
    token = _game_token(request)
    my = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)

    # DBに保存されたJSON文字列を配列に変換してフロントに渡す
    try:
        board_data = json.loads(r['board'])
    except Exception:
        board_data = []

    en_passant_raw = r.get('en_passant')
    en_passant_out = json.loads(en_passant_raw) if en_passant_raw else None

    return {
        'success': True,
        'room_code': r['room_code'],
        'board': board_data,
        'turn': r['turn'],
        'status': r['status'],
        'winner': r['winner'],
        'in_check': r['in_check'],
        'castling': r.get('castling') or 'KQkq',
        'en_passant': en_passant_out,
        'white_name': r.get('white_name') or '名無しさん',
        'black_name': r.get('black_name') or '名無しさん',
        'white_id': (r.get('white_token') or '')[:4],
        'black_id': (r.get('black_token') or '')[:4],
        'has_black': bool(r.get('black_token')),
        'my_color': my,
    }


@router.post('/chess/{room_code}/move')
async def chess_move(request: Request, room_code: str):
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)
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
    if not all(0 <= x < 8 for x in (fr, fc, tr, tc)):
        return json_resp({'success': False, 'error': '着手位置が不正です'}, 400)

    # JSON文字列をリストに読み込んで操作する
    try:
        board = json.loads(r['board'])
    except Exception:
        return json_resp({'success': False, 'error': '盤面データの読み込みに失敗しました'}, 500)

    castling = r.get('castling') or 'KQkq'
    en_passant_raw = r.get('en_passant')
    en_passant = tuple(json.loads(en_passant_raw)) if en_passant_raw else None

    piece = board[fr * 8 + fc]
    if not piece or piece[0] != color:
        return {'success': False, 'error': '自分の駒を選んでください'}
    if (tr, tc) not in _chess_pseudo(board, fr, fc, castling, en_passant):
        return {'success': False, 'error': 'その駒はそこへ動かせません'}

    # その手を指した結果、自分の王が王手にさらされる場合は指せない
    simulated = _chess_apply(board, fr, fc, tr, tc)
    if _chess_in_check(simulated, color):
        return {'success': False, 'error': 'その手を指すと自分の王が王手にさらされます'}

    # キャスリング権の更新(王・ルークが動いた/取られたら該当する権利を失う)
    new_castling = _chess_update_castling_rights(castling, piece[1], color, fr, fc, tr, tc)

    # アンパッサンの対象マスの更新(ポーンが2マス動いた時だけセット)
    new_en_passant = None
    if piece[1] == 'P' and abs(tr - fr) == 2:
        new_en_passant = [(fr + tr) // 2, fc]

    board = simulated
    next_color = 'b' if color == 'w' else 'w'

    # 次の手番が王手されているか、さらに合法手が残っているか(チェックメイト/ステイルメイト判定)
    next_in_check = _chess_in_check(board, next_color)
    next_has_moves = len(_chess_legal_moves(board, next_color, new_castling, new_en_passant)) > 0

    new_status = r['status']
    winner = r.get('winner')
    if not next_has_moves:
        new_status = 'finished'
        winner = 'draw' if not next_in_check else color
        white_key = _rating_key(r.get('white_member_id'), r.get('white_token'))
        black_key = _rating_key(r.get('black_member_id'), r.get('black_token'))
        result_white = 0.5 if winner == 'draw' else (1 if winner == 'w' else 0)
        await apply_game_result('chess', white_key, r.get('white_name') or '名無しさん',
                           black_key, r.get('black_name') or '名無しさん', result_white)
        await _award_game_result_points('chess', code,
                                         r.get('white_member_id'), result_white,
                                         r.get('black_member_id'), 1 - result_white)

    new_board_json = json.dumps(board)
    new_ep_json = json.dumps(new_en_passant) if new_en_passant else None
    now = datetime.utcnow().isoformat()
    await execute_query(
        'UPDATE chess_rooms SET board=?,turn=?,updated_at=?,in_check=?,status=?,winner=?,castling=?,en_passant=? WHERE room_code=? AND turn=?',
        [new_board_json, next_color, now, (next_color if next_in_check else None), new_status, winner, new_castling, new_ep_json, code, color]
    )
    if new_status == 'finished':
        await _game_chat_close('chess', code)
    return {'success': True}


@router.post('/chess/{room_code}/resign')
async def chess_resign(request: Request, room_code: str):
    """チェス: 対局中の参加者が降参する。"""
    code = room_code.upper()
    rows = await execute_query('SELECT * FROM chess_rooms WHERE room_code=? LIMIT 1', [code])
    if not rows:
        return json_resp({'success': False, 'error': '部屋が見つかりません'}, 404)
    r = rows[0]
    token = _game_token(request)
    color = 'w' if r.get('white_token') == token else ('b' if r.get('black_token') == token else None)
    if not color:
        return json_resp({'success': False, 'error': '観戦者は降参できません'}, 403)
    if r['status'] != 'playing':
        return {'success': False, 'error': '対局は終了しています'}

    winner = 'b' if color == 'w' else 'w'
    if not await _finish_room_if_playing('chess_rooms', code, winner):
        return {'success': False, 'error': '対局は終了しています'}

    white_key = _rating_key(r.get('white_member_id'), r.get('white_token'))
    black_key = _rating_key(r.get('black_member_id'), r.get('black_token'))
    result_white = 1 if winner == 'w' else 0
    await apply_game_result('chess', white_key, r.get('white_name') or '名無しさん',
                            black_key, r.get('black_name') or '名無しさん', result_white)
    resigner_id = r.get('white_member_id') if color == 'w' else r.get('black_member_id')
    winner_id = r.get('black_member_id') if color == 'w' else r.get('white_member_id')
    await _award_game_resign_result('chess', code, resigner_id, winner_id)
    await _game_chat_close('chess', code)
    return {'success': True}


GAME = Game(
    key='chess', label='チェス', router=router, lobby_path='/chess',
    room_meta={'table': 'chess_rooms', 'a': 'white', 'b': 'black', 'path': '/chess/',
               'label_a': '白', 'label_b': '黒'},
    has_in_check_col=True,
)
