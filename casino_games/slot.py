"""スロット。3リール × 7絵柄。"""
import secrets

from casino_service import Game

SYMBOLS = ['7', '💎', '🔔', '🍉', '🍒', '⭐', '🍋']

# 3つ揃いの配当(賭け金倍率)
TRIPLE_PAYOUT = {
    '7': 50, '💎': 25, '🔔': 15, '🍉': 10,
    '🍒': 8, '⭐': 8, '🍋': 5,
}
PAIR_NUM, PAIR_DEN = 3, 2   # 2つ揃い: bet * 3 // 2


def _play(bet: int):
    reels = [secrets.choice(SYMBOLS) for _ in range(3)]
    if reels[0] == reels[1] == reels[2]:
        payout = bet * TRIPLE_PAYOUT.get(reels[0], 0)
    else:
        a, b, c = reels
        payout = bet * PAIR_NUM // PAIR_DEN if (a == b or b == c or a == c) else 0
    return {'reels': reels}, payout


def _describe(detail):
    return '/'.join(detail.get('reels') or [])


GAME = Game(
    key='slot',
    label='スロット',
    icon='🎰',
    description='3リールを回して絵柄を揃えよう。3つ揃いで最大50倍。',
    min_bet=1,
    max_bet=10000,
    play=_play,
    describe_result=_describe,
    rules="""
      <ul>
        <li>3リールを回して絵柄を揃えます。</li>
        <li>3つ揃い: 7️⃣×50 / 💎×25 / 🔔×15 / 🍉×10 / 🍒×8 / ⭐×8 / 🍋×5</li>
        <li>2つ揃い: 賭け金の1.5倍を払い戻し</li>
        <li>ハズレ: 払い戻しなし</li>
        <li>還元率 約90%（ハウスエッジ 約10%）</li>
      </ul>
    """,
    category='slot',
)
