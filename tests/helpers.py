"""Test doubles and helpers shared by the test modules."""

import json
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

FIXTURES = Path(__file__).parent / 'fixtures'

# Tags Telegram accepts with parse_mode=HTML that the bot uses
ALLOWED_TAGS = {'b', 'a', 'code', 'i'}


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class _TelegramHTMLChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced </{tag}>")

    def handle_data(self, data):
        self.text.append(data)


def check_telegram_html(text: str) -> str:
    """Assert the message is HTML Telegram would accept; return its visible text."""
    checker = _TelegramHTMLChecker()
    checker.feed(text)
    checker.close()
    assert not checker.errors, checker.errors
    assert not checker.stack, f"unclosed tags {checker.stack}"
    return ''.join(checker.text)


class FakeHLClient:
    """Stands in for HyperliquidClient; responses are set per address."""

    def __init__(self):
        self.clearinghouse = {}
        self.spot = {}
        self.ledger = {}
        self.fills = {}
        self.portfolios = {}
        self.web = {}
        self.dexs = []
        self.now = None          # optional clock: only items with time <= now() are returned
        self.twap_histories = {}
        self.calls = []

    async def clearinghouse_state(self, user, dex=''):
        self.calls.append(('clearinghouseState', user, dex) if dex else ('clearinghouseState', user))
        key = (user, dex) if dex else user
        return self.clearinghouse.get(key, {'assetPositions': [], 'marginSummary': {'accountValue': '0'}})

    async def perp_dexs(self):
        self.calls.append(('perpDexs',))
        return list(self.dexs)

    async def spot_clearinghouse_state(self, user):
        self.calls.append(('spotClearinghouseState', user))
        return self.spot.get(user, {'balances': []})

    async def ledger_updates(self, user, start_time):
        self.calls.append(('userNonFundingLedgerUpdates', user, start_time))
        return [u for u in self.ledger.get(user, []) if u['time'] >= start_time and self._past(u)]

    async def user_fills_by_time(self, user, start_time):
        self.calls.append(('userFillsByTime', user, start_time))
        return [f for f in self.fills.get(user, []) if f['time'] >= start_time and self._past(f)]

    def _past(self, item):
        return self.now is None or item['time'] <= self.now()

    async def portfolio(self, user):
        self.calls.append(('portfolio', user))
        return self.portfolios.get(user, [])

    async def web_data2(self, user):
        self.calls.append(('webData2', user))
        return self.web.get(user, {'twapStates': [], 'meta': {'universe': []}, 'assetCtxs': []})

    async def twap_history(self, user):
        self.calls.append(('twapHistory', user))
        return self.twap_histories.get(user, [])

    async def spot_display_name(self, coin):
        return {'@107': 'HYPE', 'PURR/USDC': 'PURR'}.get(coin, coin)


class FakeBot:
    """Records sends and edits. sent[i]['text'] is updated in place when that message is edited."""

    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        self.sent.append({'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode,
                          'message_id': len(self.sent) + 1})
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.sent))

    async def edit_message_text(self, text, chat_id, message_id, parse_mode=None, **kwargs):
        self.edits.append({'chat_id': chat_id, 'message_id': message_id, 'text': text})
        self.sent[message_id - 1]['text'] = text


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.replies.append({'text': text, 'parse_mode': parse_mode})


def make_update(user_id: int):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=FakeMessage())


def make_context(bot_data: dict, args=None, bot=None):
    return SimpleNamespace(args=list(args or []), bot_data=bot_data, bot=bot or FakeBot())


def position(coin, szi, entry_px='100', position_value=None, upnl='0'):
    return {'position': {
        'coin': coin, 'szi': szi, 'entryPx': entry_px,
        'positionValue': position_value if position_value is not None else str(abs(Decimal(szi)) * 100),
        'unrealizedPnl': upnl, 'cumFunding': {'sinceOpen': '0'},
    }, 'type': 'oneWay'}


def clearinghouse(*positions, account_value='1000'):
    return {'assetPositions': list(positions), 'marginSummary': {'accountValue': account_value}}


def fill(coin, direction, sz, px, time_ms, start_position, oid=1, tid=None, side=None, closed_pnl='0', **extra):
    """A userFillsByTime entry with the fields the engine reads (spec 5.1)."""
    if side is None:
        side = 'B' if direction in ('Open Long', 'Close Short', 'Short > Long', 'Buy') else 'A'
    data = {'coin': coin, 'px': str(px), 'sz': str(sz), 'side': side, 'time': time_ms,
            'startPosition': str(start_position), 'dir': direction, 'closedPnl': str(closed_pnl),
            'hash': f'0x{time_ms:x}', 'oid': oid, 'crossed': True, 'fee': '0.1', 'tid': tid or time_ms,
            'feeToken': 'USDC', 'twapId': None}
    data.update(extra)
    return data
