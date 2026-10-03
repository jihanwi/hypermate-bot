"""Related wallet discovery, /related (spec 7).

One discovery is a fixed set of HL info calls at ledger priority, capped at
MAX_DISCOVERY_WEIGHT (spec 7.4: 300). Links are classified by spec 7.2:

  confirmed  subaccount / master (subAccounts, userRole), agent (webData2.agentAddress,
             extraAgents), staking_link (userFees.stakingLink), vault_leader (vaultDetails)
  likely     transfer_counterparty with transfers both ways, 2+ one way, or one of $10k+
  weak       transfer_counterparty otherwise, referral, vault_follow

System addresses (escrow, zero, assistance fund, HLP) are never links. Counterparties
whose userRole is 'vault' become vault_follow. Exchange tags: no data source (owner
decision 2026-10-03), backlog.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from hypermate.core.events import HYPERLIQUID
from hypermate.core.numbers import to_decimal
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import scheduler
from hypermate.venues.hyperliquid.client import HyperliquidAPIError, HyperliquidClient

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
MAX_DISCOVERY_WEIGHT = 300
USER_ROLE_TTL_MS = 7 * 24 * 3600 * 1000
LINKS_TTL_MS = 24 * 3600 * 1000
LIKELY_SINGLE_USD = Decimal(10_000)
MAX_ROWS = 12                    # rows shown (and priced with clearinghouseState, 2 each)
PRIORITY = scheduler.P_LEDGER

# Owner-confirmed system addresses (2026-10-03)
SYSTEM_ADDRESSES = frozenset({
    '0x2000000000000000000000000000000000000000',   # HIP-3 / system escrow
    '0x0000000000000000000000000000000000000000',
    '0xfefefefefefefefefefefefefefefefefefefefe',   # assistance fund
    '0xdfc24b077bc1425ad1dea75bcb6f8158e10df303',   # HLP vault
})

CONFIRMED, LIKELY, WEAK = 'confirmed', 'likely', 'weak'
CONFIDENCE_ORDER = (CONFIRMED, LIKELY, WEAK)
TRANSFER_TYPES = ('spotTransfer', 'send', 'internalTransfer', 'subAccountTransfer')


def is_system_address(address: str) -> bool:
    """Listed system addresses, plus any address whose last 36 hex digits repeat one or two characters
    (0x2000…0000, 0x0000…, 0xfefe…fe)."""
    address = address.lower()
    if address in SYSTEM_ADDRESSES:
        return True
    tail = address[6:]
    if len(tail) != 36:
        return False
    return len(set(tail)) == 1 or tail == tail[:2] * 18


@dataclass
class Link:
    related_address: str
    link_type: str
    confidence: str
    evidence: dict = field(default_factory=dict)

    def row(self) -> dict:
        return {'related_address': self.related_address.lower(), 'related_venue': HYPERLIQUID,
                'link_type': self.link_type, 'confidence': self.confidence,
                'evidence': {**self.evidence, 'discovery': True}}


@dataclass
class Discovery:
    address: str
    links: list[Link] = field(default_factory=list)
    meter: dict = field(default_factory=dict)         # {request type: weight}
    values: dict = field(default_factory=dict)        # related address -> accountValue (Decimal)
    errors: list[str] = field(default_factory=list)

    @property
    def weight(self) -> int:
        return sum(self.meter.values())

    def add(self, address: Optional[str], link_type: str, confidence: str, **evidence) -> None:
        if not address:
            return
        address = address.lower()
        if address == self.address or is_system_address(address):
            return
        for link in self.links:
            if link.related_address == address and link.link_type == link_type:
                link.evidence.update(evidence)
                if CONFIDENCE_ORDER.index(confidence) < CONFIDENCE_ORDER.index(link.confidence):
                    link.confidence = confidence
                return
        self.links.append(Link(address, link_type, confidence, evidence))

    def by_address(self) -> dict[str, list[Link]]:
        out: dict[str, list[Link]] = {}
        for link in self.links:
            out.setdefault(link.related_address, []).append(link)
        return out


# Counterparties ------------------------------------------------------------------

def counterparties(address: str, updates: list[dict]) -> dict[str, dict]:
    """{counterparty: {in, out, usd, last_ms}} from ledger transfers (self-moves and system addresses skipped)."""
    address = address.lower()
    stats: dict[str, dict] = {}
    for update in updates:
        delta = update.get('delta') or {}
        if delta.get('type') not in TRANSFER_TYPES:
            continue
        user = str(delta.get('user', '')).lower()
        destination = str(delta.get('destination', '')).lower()
        if user == destination:
            continue                                     # spot <-> perp class move of the same account
        if user == address:
            other, direction = destination, 'out'
        elif destination == address:
            other, direction = user, 'in'
        else:
            continue
        if not other or is_system_address(other):
            continue
        entry = stats.setdefault(other, {'in': 0, 'out': 0, 'usd': ZERO, 'last_ms': 0})
        entry[direction] += 1
        entry['usd'] += to_decimal(delta.get('usdcValue')) or to_decimal(delta.get('amount')) or ZERO
        entry['last_ms'] = max(entry['last_ms'], int(update.get('time', 0)))
    return stats


def counterparty_confidence(entry: dict) -> str:
    """Spec 7.2: both directions, 2+ one way, or a single transfer of $10k+ -> likely; else weak."""
    both = entry['in'] > 0 and entry['out'] > 0
    repeated = entry['in'] + entry['out'] >= 2
    big = (to_decimal(entry['usd']) or ZERO) >= LIKELY_SINGLE_USD
    return LIKELY if both or repeated or big else WEAK


# Discovery -------------------------------------------------------------------------

async def cached_user_role(client: HyperliquidClient, repo: Repo, address: str, now_ms: int,
                           meter: dict) -> dict:
    """userRole (weight 60) through api_cache for USER_ROLE_TTL_MS (spec 7.1)."""
    key = f"userRole:{address.lower()}"
    cached = await repo.cache_get(key, now_ms)
    if cached is not None:
        return cached
    role = await client.user_role(address, priority=PRIORITY, meter=meter)
    await repo.cache_set(key, role, now_ms + USER_ROLE_TTL_MS)
    return role


async def _step(discovery: Discovery, name: str, coro):
    try:
        return await coro
    except HyperliquidAPIError as e:
        discovery.errors.append(f"{name}: {e}")
        logger.warning(f"/related {name} failed for {discovery.address}: {e}")
        return None


async def discover(client: HyperliquidClient, repo: Repo, address: str, now_ms: int) -> Discovery:
    """One full discovery for an HL address (1 hop, 2 hops for subaccount/master)."""
    address = address.lower()
    d = Discovery(address)
    meter = d.meter
    opts = {'priority': PRIORITY, 'meter': meter}

    role = await _step(d, 'userRole', cached_user_role(client, repo, address, now_ms, meter)) or {}
    role_name = role.get('role')
    role_data = role.get('data') or {}
    if role_name == 'subAccount':
        d.add(role_data.get('master'), 'master', CONFIRMED, source='userRole')
    elif role_name == 'agent':
        d.add(role_data.get('user'), 'master', CONFIRMED, source='userRole', note='this address is an API wallet')

    subs = await _step(d, 'subAccounts', client.sub_accounts(address, **opts)) or []
    for sub in subs:
        d.add(sub.get('subAccountUser'), 'subaccount', CONFIRMED, name=sub.get('name'), source='subAccounts')
    # 2 hops (spec 7.3): the master's other subaccounts
    master = next((l.related_address for l in d.links if l.link_type == 'master'), None)
    if master:
        siblings = await _step(d, 'subAccounts(master)', client.sub_accounts(master, **opts)) or []
        for sub in siblings:
            d.add(sub.get('subAccountUser'), 'subaccount', CONFIRMED, name=sub.get('name'), via=master,
                  source='subAccounts')

    web = await _step(d, 'webData2', client.web_data2(address, **opts)) or {}
    d.add(web.get('agentAddress'), 'agent', CONFIRMED, source='webData2', valid_until=web.get('agentValidUntil'))
    for agent in await _step(d, 'extraAgents', client.extra_agents(address, **opts)) or []:
        d.add(agent.get('address'), 'agent', CONFIRMED, name=agent.get('name'), source='extraAgents',
              valid_until=agent.get('validUntil'))

    fees = await _step(d, 'userFees', client.user_fees(address, **opts)) or {}
    staking = fees.get('stakingLink') or {}
    if isinstance(staking, dict):
        d.add(staking.get('stakingUser') or staking.get('tradingUser'), 'staking_link', CONFIRMED,
              link=staking.get('type'), source='userFees')

    ref = await _step(d, 'referral', client.referral(address, **opts)) or {}
    referred_by = ref.get('referredBy') or {}
    d.add(referred_by.get('referrer'), 'referral', WEAK, relation='referred by', code=referred_by.get('code'))
    for state in ((ref.get('referrerState') or {}).get('data') or {}).get('referralStates') or []:
        d.add(state.get('user'), 'referral', WEAK, relation='referred', cum_vlm=str(state.get('cumVlm', '')))

    for equity in await _step(d, 'userVaultEquities', client.user_vault_equities(address, **opts)) or []:
        d.add(equity.get('vaultAddress'), 'vault_follow', WEAK, equity=str(equity.get('equity', '')),
              source='userVaultEquities')

    if role_name == 'vault':
        details = await _step(d, 'vaultDetails', client.vault_details(address, **opts)) or {}
        d.add(details.get('leader'), 'vault_leader', CONFIRMED, vault=address, name=details.get('name'))
        for follower in details.get('followers') or []:
            d.add(follower.get('user'), 'vault_follow', WEAK, vault=address, equity=str(follower.get('vaultEquity', '')))

    updates = await _step(d, 'ledger', client.ledger_updates(address, 0, **opts)) or []
    stats = counterparties(address, updates)
    for update in updates:                              # vault deposits name the vault directly
        delta = update.get('delta') or {}
        if delta.get('type') in ('vaultDeposit', 'vaultWithdraw'):
            d.add(delta.get('vault'), 'vault_follow', WEAK, source='ledger')
    vaults = {l.related_address for l in d.links if l.link_type in ('vault_follow', 'vault_leader')}
    for other, entry in sorted(stats.items(), key=lambda kv: -(kv[1]['in'] + kv[1]['out'])):
        if other in vaults:
            continue
        d.add(other, 'transfer_counterparty', counterparty_confidence(entry), **{
            'in': entry['in'], 'out': entry['out'], 'usd': str(entry['usd']), 'last_ms': entry['last_ms']})

    # Account value of the rows that will be shown (clearinghouseState, 2 each), then userRole checks
    # on likely counterparties while the budget allows (a 'vault' role turns the row into vault_follow)
    shown = ranked_addresses(d)
    for other in shown:
        if d.weight + scheduler.request_cost('clearinghouseState')[0] > MAX_DISCOVERY_WEIGHT:
            break
        state = await _step(d, 'clearinghouseState', client.clearinghouse_state(other, **opts))
        if state:
            d.values[other] = to_decimal((state.get('marginSummary') or {}).get('accountValue'))
    for link in [l for l in d.links if l.link_type == 'transfer_counterparty' and l.confidence == LIKELY]:
        if link.related_address not in shown:
            continue
        cached = await repo.cache_get(f"userRole:{link.related_address}", now_ms)
        if cached is None and d.weight + scheduler.request_cost('userRole')[0] > MAX_DISCOVERY_WEIGHT:
            break
        other_role = await _step(d, 'userRole(counterparty)',
                                 cached_user_role(client, repo, link.related_address, now_ms, meter)) or {}
        if other_role.get('role') == 'vault':
            link.link_type, link.confidence = 'vault_follow', WEAK
            link.evidence['source'] = 'userRole'

    logger.info(f"/related {address}: {len(d.links)} links, weight {d.weight} "
                f"({', '.join(f'{k} {v}' for k, v in d.meter.items())})")
    return d


def select_addresses(by_address: dict[str, list], max_rows: int) -> list[str]:
    """The addresses worth showing when there are more than max_rows: every address with a link other
    than transfer_counterparty first (subaccounts, agents, referrals, vaults are few), then the
    counterparties by transfer count. Display order is by confidence (see formatter)."""
    def rank(item):
        address, links = item
        counterparty_only = all(_get(l, 'link_type') == 'transfer_counterparty' for l in links)
        best = min(CONFIDENCE_ORDER.index(_get(l, 'confidence')) for l in links)
        transfers = sum(int(_get(l, 'evidence').get('in', 0)) + int(_get(l, 'evidence').get('out', 0))
                        for l in links)
        return counterparty_only, best, -transfers, address
    return [address for address, _ in sorted(by_address.items(), key=rank)][:max_rows]


def _get(link, field_name: str):
    """Link dataclass or a wallet_links row dict."""
    return getattr(link, field_name) if isinstance(link, Link) else link[field_name]


def ranked_addresses(d: Discovery) -> list[str]:
    return select_addresses(d.by_address(), MAX_ROWS)


async def links_for(client: HyperliquidClient, repo: Repo, address: str, now_ms: int,
                    refresh: bool = False) -> tuple[list[dict], Optional[Discovery]]:
    """Stored links if a discovery ran within LINKS_TTL_MS (and not refresh), else a new discovery."""
    wallet_id = await repo.wallet_id(address)
    if wallet_id is None:
        return [], None
    last = await repo.links_discovered_at(wallet_id)
    if not refresh and last is not None and now_ms - last < LINKS_TTL_MS:
        return await repo.links(wallet_id), None
    d = await discover(client, repo, address, now_ms)
    rows = [l.row() for l in d.links]
    for row in rows:
        value = d.values.get(row['related_address'])
        if value is not None:
            row['evidence']['account_value'] = str(value)
    await repo.replace_links(wallet_id, rows, now_ms)
    return await repo.links(wallet_id), d
