"""Test doubles and helpers shared by the test modules."""

import json
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

FIXTURES = Path(__file__).parent / 'fixtures'

# Tags Telegram accepts with parse_mode=HTML that the bot uses
ALLOWED_TAGS = {'b', 'a', 'code'}


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
        self.calls = []

    async def clearinghouse_state(self, user):
        self.calls.append(('clearinghouseState', user))
        return self.clearinghouse.get(user, {'assetPositions': [], 'marginSummary': {'accountValue': '0'}})

    async def spot_clearinghouse_state(self, user):
        self.calls.append(('spotClearinghouseState', user))
        return self.spot.get(user, {'balances': []})

    async def ledger_updates(self, user, start_time):
        self.calls.append(('userNonFundingLedgerUpdates', user, start_time))
        return [u for u in self.ledger.get(user, []) if u['time'] >= start_time]

    async def user_fills_by_time(self, user, start_time):
        self.calls.append(('userFillsByTime', user, start_time))
        return [f for f in self.fills.get(user, []) if f['time'] >= start_time]

    async def portfolio(self, user):
        self.calls.append(('portfolio', user))
        return self.portfolios.get(user, [])

    async def spot_display_name(self, coin):
        return {'@107': 'HYPE', 'PURR/USDC': 'PURR'}.get(coin, coin)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        self.sent.append({'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode})


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
