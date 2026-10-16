"""ビデオポーカー(ジャックス・オア・ベター)。

流れ: 賭けて5枚配る → 残すカードを選ぶ → 残り(選ばなかったカード)を交換して役で配当。
山札はサーバー側で配る時に1回だけシャッフルして保存し、交換で出るカードも配る時点で決まっている
(クライアントが見られるのは最初の5枚だけ)。乱数はすべてサーバー側。
"""
import secrets

from casino_service import Game, DAILY_LOSS_LIMIT, BET_COOLDOWN_SECONDS

MIN_BET = 1
MAX_BET = 10000

# カードは [ランク, スート] の整数ペア。ランクは 2〜14 (11=J, 12=Q, 13=K, 14=A)、スートは 0〜3。
SUITS = ['♠', '♥', '♦', '♣']

# 役 → (表示名, 配当倍率)。倍率はそのまま「賭け金 × 倍率」を払い戻す。
PAYTABLE = [
    ('royal_flush',    'ロイヤルフラッシュ',   250),
    ('straight_flush', 'ストレートフラッシュ', 50),
    ('four',           'フォーカード',         25),
    ('full_house',     'フルハウス',           8),
    ('flush',          'フラッシュ',           5),
    ('straight',       'ストレート',           4),
    ('three',          'スリーカード',         3),
    ('two_pair',       'ツーペア',             2),
    ('jacks_or_better', 'ジャックス・オア・ベター(Jのワンペア以上)', 1),
]
PAY = {k: mult for k, _label, mult in PAYTABLE}
LABEL = {k: label for k, label, _mult in PAYTABLE}

# 画面上部の配当表(筐体)用。テンプレートから game.paytable で参照する。
_SHORT = {'jacks_or_better': 'J以上のワンペア'}
PAYTABLE_VIEW = [
    {'key': k, 'label': _SHORT.get(k, label), 'mult': mult} for k, label, mult in PAYTABLE
]


def _new_deck():
    deck = [[r, s] for r in range(2, 15) for s in range(4)]
    # secrets は shuffle を持たないので、Fisher-Yates を暗号論的乱数で行う
    for i in range(len(deck) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck


def evaluate(hand):
    """5枚の手札の役を返す。役がなければ None。"""
    ranks = sorted(c[0] for c in hand)
    suits = [c[1] for c in hand]
    flush = len(set(suits)) == 1
    uniq = sorted(set(ranks))
    straight = False
    if len(uniq) == 5:
        straight = (uniq[-1] - uniq[0] == 4) or (uniq == [2, 3, 4, 5, 14])   # A-2-3-4-5 もストレート
    counts = sorted((ranks.count(r) for r in uniq), reverse=True)

    if straight and flush:
        return 'royal_flush' if uniq == [10, 11, 12, 13, 14] else 'straight_flush'
    if counts[0] == 4:
        return 'four'
    if counts == [3, 2]:
        return 'full_house'
    if flush:
        return 'flush'
    if straight:
        return 'straight'
    if counts[0] == 3:
        return 'three'
    if counts == [2, 2, 1]:
        return 'two_pair'
    if counts == [2, 1, 1, 1]:
        pair_rank = next(r for r in uniq if ranks.count(r) == 2)
        if pair_rank >= 11:
            return 'jacks_or_better'
    return None


def _start(bet: int):
    deck = _new_deck()
    hand = deck[:5]
    state = {'deck': deck, 'hand': hand}        # 山札を含むので、プレイヤーには見せない
    return state, {'hand': hand}


def _public_view(state):
    return {'hand': state['hand']}


def _resolve(bet: int, state, action):
    """action = {'holds': [bool×5]} 。True のカードを残し、それ以外を山札の続きから引いて交換する。"""
    holds = action.get('holds')
    if not isinstance(holds, list) or len(holds) != 5 or not all(isinstance(h, bool) for h in holds):
        raise ValueError('invalid holds')
    deck, hand = state['deck'], state['hand']
    new_hand, draw_idx = [], 5
    for card, hold in zip(hand, holds):
        if hold:
            new_hand.append(card)
        else:
            new_hand.append(deck[draw_idx])
            draw_idx += 1
    rank = evaluate(new_hand)
    payout = bet * PAY[rank] if rank else 0
    detail = {
        'initial': hand, 'hand': new_hand, 'held': holds,
        'rank': rank, 'rank_label': LABEL.get(rank, 'ハズレ'),
    }
    return detail, payout, detail


def _describe(detail):
    return detail.get('rank_label') or ''


# ---- ルール表示(HTML) ----
# 役ごとの例と説明。例は「A♠ K♠ ...」の文字列で、♥♦は赤で表示する。
_HAND_INFO = {
    'royal_flush': ('A♠ K♠ Q♠ J♠ 10♠', '同じスート（マーク）の 10・J・Q・K・A。いちばん強い役です。'),
    'straight_flush': ('9♥ 8♥ 7♥ 6♥ 5♥', '同じスートで数字が5枚連続（ロイヤルフラッシュを除く）。A-2-3-4-5 も対象です。'),
    'four': ('8♠ 8♥ 8♦ 8♣ K♠', '同じ数字のカードが4枚。残りの1枚は何でもかまいません。'),
    'full_house': ('Q♠ Q♥ Q♦ 4♣ 4♠', '同じ数字3枚 ＋ 別の同じ数字2枚の組み合わせ。'),
    'flush': ('K♦ 9♦ 7♦ 4♦ 2♦', '5枚すべてが同じスート。数字は連続していなくてOKです。'),
    'straight': ('9♠ 8♥ 7♦ 6♣ 5♠', '数字が5枚連続（スートはバラバラ）。A-2-3-4-5 と 10-J-Q-K-A は有効、Q-K-A-2-3 のように A をまたぐ並びは無効です。'),
    'three': ('7♠ 7♥ 7♦ K♣ 2♠', '同じ数字が3枚。残り2枚は別の数字です。'),
    'two_pair': ('J♠ J♥ 4♦ 4♣ A♠', '同じ数字のペアが2組。'),
    'jacks_or_better': ('K♠ K♥ 9♦ 5♣ 2♠', 'J・Q・K・A のどれかのワンペア。10 以下のペアは払い戻しなしです。'),
}


def _mini_cards(text):
    out = []
    for tok in text.split():
        rank, suit = tok[:-1], tok[-1]
        cls = 'mc red' if suit in '♥♦' else 'mc'
        out.append(f'<span class="{cls}">{rank}{suit}</span>')
    return ''.join(out)


def _rules_html():
    hand_rows = ''.join(
        f'<div class="hand-row">'
        f'<div class="hand-head"><span>{label}</span><span>×{mult}</span></div>'
        f'<div class="mini">{_mini_cards(_HAND_INFO[k][0])}</div>'
        f'<div class="hand-desc">{_HAND_INFO[k][1]}</div>'
        f'</div>'
        for k, label, mult in PAYTABLE
    )
    return f"""
      <h3>基本の流れ</h3>
      <ol>
        <li>賭け金（{MIN_BET}〜{MAX_BET}pt）を決めて「配る」を押すと、5枚のカードが配られます。賭け金はこの時点で引き落とされます。</li>
        <li>残したいカードをタップして <b>HOLD</b> にします（もう一度タップで解除）。キーボードなら 1〜5 キーでも切り替えできます。</li>
        <li>「交換する」を押すと、HOLD していないカードだけが新しいカードに入れ替わります。<b>交換は1回だけ</b>です。</li>
        <li>最終的な5枚で役が成立していれば、賭け金 × 倍率が払い戻されます。役がなければ払い戻しはありません。</li>
      </ol>

      <h3>役と配当（強い順）</h3>
      <p>倍率は賭け金にかかります。複数の役が成立する場合は、いちばん高い役だけが配当の対象です。</p>
      {hand_rows}

      <h3>遊び方のコツ</h3>
      <ul>
        <li>ストレート以上がすでに完成しているときは、5枚すべてを HOLD するのが基本です。</li>
        <li>フォーカード・スリーカード・ツーペアは、そろっているカードだけを残します（ツーペアは残り1枚を交換すると、フルハウスのチャンスです）。</li>
        <li>J 以上のワンペアは配当の対象なので、ペアを残して残り3枚を交換します。</li>
        <li>10 以下のペアは配当にならないため、ペアを残して3枚引き、スリーカード以上を狙う手が一般的です。</li>
        <li>同じスートや連続した数字が4枚そろっているときは、残り1枚を引いてフラッシュ・ストレートを狙う選択肢もあります。</li>
        <li>これは簡易的な目安です。最善の手は手札によって変わります。</li>
      </ul>

      <h3>ルールの詳細</h3>
      <ul>
        <li>52枚のトランプ1組（ジョーカーなし）を使います。配るたびに山札を新しくシャッフルします。</li>
        <li>山札のシャッフルはサーバー側で行い、交換で出るカードも配る時点で決まっています。プレイヤーの画面には最初の5枚しか送られません。</li>
        <li>A（エース）は、10-J-Q-K-A では最も強く、A-2-3-4-5 では最も弱いカードとして扱われます。</li>
        <li>スートの強弱はありません。♠♥♦♣は役の判定（フラッシュなど）にだけ使われます。</li>
        <li>配った後に画面を閉じても、ページを開き直せば続きから遊べます。途中でやめても賭け金は戻りません。</li>
        <li>1日（日本時間）の損失が合計 {DAILY_LOSS_LIMIT:,}pt に達すると、その日はそれ以上賭けられません（カジノ全ゲーム合計）。</li>
        <li>連続して賭けるときは、{BET_COOLDOWN_SECONDS}秒以上の間隔が必要です。</li>
        <li>還元率は約96%（最善の遊び方をした場合の理論値）です。短期的には大きく上下します。</li>
      </ul>
    """


GAME = Game(
    key='poker',
    label='ビデオポーカー',
    icon='🃏',
    description='5枚から残すカードを選んで交換。ジャックス・オア・ベター以上で配当、最大250倍。',
    min_bet=MIN_BET,
    max_bet=MAX_BET,
    describe_result=_describe,
    rules=_rules_html(),
    category='poker',
    interactive=True,
    start=_start,
    resolve=_resolve,
    public_view=_public_view,
)
GAME.paytable = PAYTABLE_VIEW   # 筐体の配当表用(テンプレートが game.paytable で参照)
