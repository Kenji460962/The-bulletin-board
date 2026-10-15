"""ビデオポーカー(ジャックス・オア・ベター)。

流れ: 賭けて5枚配る → 残すカードを選ぶ → 残り(選ばなかったカード)を交換して役で配当。
山札はサーバー側で配る時に1回だけシャッフルして保存し、交換で出るカードも配る時点で決まっている
(クライアントが見られるのは最初の5枚だけ)。乱数はすべてサーバー側。
"""
import secrets

from casino_service import Game

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


def _rules_html():
    rows = ''.join(f'<li>{label}: ×{mult}</li>' for _k, label, mult in PAYTABLE)
    return f"""
      <ul>
        <li>賭けると5枚が配られます。残したいカードを選んで「交換する」を押すと、残りのカードが入れ替わります。</li>
        <li>最終的な5枚の役で、賭け金に倍率をかけた額が払い戻されます（役なしは払い戻しなし）。</li>
        {rows}
        <li>還元率 約96%（最善の遊び方をした場合）</li>
        <li>配ったあとで画面を閉じても、開き直せば続きから遊べます。途中でやめても賭け金は戻りません。</li>
      </ul>
    """


GAME = Game(
    key='poker',
    label='ビデオポーカー',
    icon='🃏',
    description='5枚から残すカードを選んで交換。ジャックス・オア・ベター以上で配当、最大250倍。',
    min_bet=1,
    max_bet=10000,
    describe_result=_describe,
    rules=_rules_html(),
    category='poker',
    interactive=True,
    start=_start,
    resolve=_resolve,
    public_view=_public_view,
)
