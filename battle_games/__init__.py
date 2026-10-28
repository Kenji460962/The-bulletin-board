"""
対戦ゲームのレジストリ。
新ゲームを追加するときは、ここに import + register するだけでよい。
"""
from . import othello, chess, shogi, werewolf


def register_all(game_service):
    game_service.register_game(othello.GAME)
    game_service.register_game(chess.GAME)
    game_service.register_game(shogi.GAME)
    game_service.register_game(werewolf.GAME)
