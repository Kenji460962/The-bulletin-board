"""スロット。実機風: 3リール × 32コマのリール配列(仮想リール) × 3段表示、センターライン1本。

- リールごとに配列(コマの並び)が違い、止まる位置(stop)を乱数で決める。
- 上段・中段・下段は、止まった位置の前後のコマ。実機と同じく「目押し外れ」「リーチ目」が自然に出る。
- 払い戻しは中段(ペイライン)の並びだけで決まる。
- 乱数はサーバー側(secrets)。クライアントは結果を演出するだけ。
"""
import secrets

from casino_service import Game

# 32コマ。出現数: 7×2 / 💎×3 / 🔔×4 / 🍉×5 / ⭐×5 / 🍒×6 / 🍋×7
REEL_STRIPS = [
    ['🍋', '⭐', '🍋', '💎', '🍉', '🍒', '🍋', '🍒', '💎', '🍒', '🔔', '🍒', '7', '🔔', '🍉', '💎',
     '🍉', '⭐', '🍉', '🍒', '🍋', '🔔', '🍋', '⭐', '🍋', '🍉', '⭐', '🔔', '🍒', '⭐', '🍋', '7'],
    ['🍋', '🍒', '🍋', '🍒', '🍉', '🍋', '⭐', '🍉', '🍋', '💎', '🍒', '🔔', '🍉', '🍋', '7', '🍉',
     '🍒', '⭐', '🍉', '🍋', '🔔', '🍒', '7', '⭐', '🔔', '🍋', '💎', '🍒', '🔔', '⭐', '💎', '⭐'],
    ['🍒', '🍋', '🍉', '🍋', '🍒', '🍋', '🍒', '7', '🔔', '⭐', '🍒', '🔔', '🍉', '⭐', '💎', '🍋',
     '🔔', '🍒', '🍋', '🍉', '💎', '🍋', '⭐', '🍒', '🍉', '💎', '7', '⭐', '🍋', '⭐', '🔔', '🍉'],
]
STRIP_LEN = 32

# 3つ揃い(賭け金倍率)
TRIPLE_PAYOUT = {
    '7': 100, '💎': 50, '🔔': 25, '🍉': 12,
    '🍒': 10, '⭐': 10, '🍋': 6,
}
FRUITS = {'🍒', '🍉', '🍋'}
CHERRY_DOUBLE = 6    # 左から🍒🍒(3つ目は何でも)
FRUIT_MIX = 2        # 🍒🍉🍋のどれかが3つ並ぶ(絵柄ばらばら)
CHERRY_SINGLE = 1    # 左リールに🍒(賭け金が戻る)


def _line_payout(line):
    """中段の3絵柄 -> (配当倍率, 役名)。"""
    a, b, c = line
    if a == b == c:
        return TRIPLE_PAYOUT[a], f'{a}揃い'
    if a == '🍒' and b == '🍒':
        return CHERRY_DOUBLE, 'チェリー2つ'
    if {a, b, c} <= FRUITS:
        return FRUIT_MIX, 'フルーツ'
    if a == '🍒':
        return CHERRY_SINGLE, 'チェリー'
    return 0, ''


def _spin():
    stops = [secrets.randbelow(STRIP_LEN) for _ in REEL_STRIPS]
    window = []
    for strip, s in zip(REEL_STRIPS, stops):
        window.append([strip[(s - 1) % STRIP_LEN], strip[s], strip[(s + 1) % STRIP_LEN]])
    return stops, window


def _play(bet: int):
    stops, window = _spin()
    line = [w[1] for w in window]
    mult, name = _line_payout(line)
    detail = {'reels': line, 'stops': stops, 'window': window, 'hit': name}
    return detail, bet * mult


def _describe(detail):
    return '/'.join(detail.get('reels') or [])


GAME = Game(
    key='slot',
    label='スロット',
    icon='🎰',
    description='3リールを回して中段に絵柄を揃えよう。7揃いで最大100倍。',
    min_bet=1,
    max_bet=10000,
    play=_play,
    describe_result=_describe,
    rules="""
      <ul>
        <li>3リール×3段。<b>中段（赤いライン）</b>の並びで配当が決まります。</li>
        <li>3つ揃い: 7️⃣×100 / 💎×50 / 🔔×25 / 🍉×12 / 🍒×10 / ⭐×10 / 🍋×6</li>
        <li>左から🍒🍒: ×6 ／ 🍒🍉🍋のどれかが3つ（ばらばらでもOK）: ×2</li>
        <li>左リールに🍒: ×1（賭け金が戻ります）</li>
        <li>上記以外はハズレ。上段・下段は配当に関係ありません。</li>
        <li>リールは1本ずつ配列が違い、止まる位置はサーバーが抽選します。</li>
        <li>還元率 約90%（ハウスエッジ 約10%）</li>
      </ul>
    """,
    category='slot',
)
