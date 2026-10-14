"""
カジノのゲームレジストリ。
新ゲームを追加するときは、ここに import + register するだけでよい。
"""
from . import slot


def register_all(casino_service):
    casino_service.register_game(slot.GAME)
