"""ルーレット(ヨーロピアン / 0〜36 のシングルゼロ)。賭け方は「赤」「黒」「数字(ストレートアップ)」。

- 赤 / 黒: 当たれば賭け金の2倍を払い戻し(0 は赤でも黒でもないので負け)
- 数字:    選んだ1つの数字が出れば賭け金の36倍を払い戻し
どちらも還元率は 36/37 ≒ 97.3%(ハウスエッジ 約2.7%)。結果の数字はサーバー側で決める。
"""
import secrets

from casino_service import Game

# ヨーロピアンルーレットの赤の数字(それ以外の 1〜36 が黒、0 が緑)
RED_NUMBERS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})

COLOR_PAYOUT = 2      # 赤・黒の払い戻し倍率
NUMBER_PAYOUT = 36    # 数字の払い戻し倍率

COLOR_JP = {'red': '赤', 'black': '黒', 'green': '緑'}


def color_of(n: int) -> str:
    if n == 0:
        return 'green'
    return 'red' if n in RED_NUMBERS else 'black'


def _validate_choice(choice) -> bool:
    if not isinstance(choice, dict):
        return False
    kind = choice.get('type')
    if kind in ('red', 'black'):
        return True
    if kind == 'number':
        n = choice.get('number')
        return isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 36
    return False


def _play(bet: int, choice: dict):
    number = secrets.randbelow(37)               # 0〜36
    color = color_of(number)
    kind = choice['type']
    if kind == 'number':
        win = (number == choice['number'])
        payout = bet * NUMBER_PAYOUT if win else 0
        saved_choice = {'type': 'number', 'number': choice['number']}
    else:
        win = (color == kind)
        payout = bet * COLOR_PAYOUT if win else 0
        saved_choice = {'type': kind}
    return {'number': number, 'color': color, 'choice': saved_choice, 'win': win}, payout


def choice_label(choice: dict) -> str:
    if choice.get('type') == 'number':
        return f"数字{choice.get('number')}"
    return COLOR_JP.get(choice.get('type'), '')


def _describe(detail):
    return f"{detail.get('number')}({COLOR_JP.get(detail.get('color'), '')}) / 予想:{choice_label(detail.get('choice') or {})}"


GAME = Game(
    key='roulette',
    label='ルーレット',
    icon='🎡',
    description='赤・黒・数字に賭けよう。赤黒は2倍、数字を当てれば36倍。',
    min_bet=1,
    max_bet=10000,
    play=_play,
    describe_result=_describe,
    rules="""
      <ul>
        <li>0〜36 の37個の数字(ヨーロピアン)。0 は緑、1〜36 は赤と黒です。</li>
        <li>赤 / 黒: 当たれば賭け金の2倍を払い戻し。0 が出たらどちらも負けです。</li>
        <li>数字: 選んだ数字がぴったり出れば賭け金の36倍を払い戻し。</li>
        <li>賭け方は1回につき1つ選びます。</li>
        <li>還元率 約97.3%（ハウスエッジ 約2.7%）</li>
      </ul>
    """,
    category='roulette',
    accepts_choice=True,
    validate_choice=_validate_choice,
)
