"""ブロック崩し(1人用)。登録は battle_games/__init__.py の register_all() から。

オンライン対戦ではなく個人で遊ぶゲーム。ゲームの動きはすべてブラウザ側(templates/breakout.html)で処理し、
ベストスコアもブラウザ(localStorage)に保存するので、サーバー側はページを返すだけ(DB・部屋・通信なし)。
"""
from fastapi import APIRouter, Request

from game_service import Game, templates

router = APIRouter()


@router.get('/breakout')
async def breakout_page(request: Request):
    return templates.TemplateResponse(request, 'breakout.html', {})


GAME = Game(key='breakout', label='ブロック崩し', router=router, lobby_path='/breakout')
