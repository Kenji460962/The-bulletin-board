"""
カジノのゲームレジストリ。
新ゲームを追加するときは、ここに import + register するだけでよい。
"""
from . import slot, poker, roulette


def register_all(casino_service):
    casino_service.register_game(slot.GAME)
    casino_service.register_game(poker.GAME)
    casino_service.register_game(roulette.GAME)
