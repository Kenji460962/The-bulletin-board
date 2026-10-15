"""
カジノのゲームレジストリ。
新ゲームを追加するときは、ここに import + register するだけでよい。
"""
from . import slot, poker


def register_all(casino_service):
    casino_service.register_game(slot.GAME)
    casino_service.register_game(poker.GAME)
