"""
対戦ゲームのレジストリ。
新ゲームを追加するときは、ここに import + register するだけでよい。
"""
from . import othello, chess, shogi, werewolf, tetris, breakout


def register_all(game_service):
    game_service.register_game(othello.GAME)
    game_service.register_game(chess.GAME)
    game_service.register_game(shogi.GAME)
    game_service.register_game(werewolf.GAME)
    game_service.register_game(tetris.GAME)
    game_service.register_game(breakout.GAME)
