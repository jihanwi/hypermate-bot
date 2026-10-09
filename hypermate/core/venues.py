"""Venue resolution for /add, /rescan and the daily rescan (spec 6.1).

All adapters' resolve() run in parallel; a venue with accounts becomes (or stays) active,
a venue without becomes inactive. A failing adapter leaves that venue as it is.
"""

import asyncio
import logging
from typing import Optional

from hypermate.db.repo import Repo
from hypermate.venues import base
from hypermate.venues.base import NAMES, VenueAccount

logger = logging.getLogger(__name__)

VENUE_ORDER = (base.HYPERLIQUID, base.LIGHTER, base.RISEX, base.ASTER)


async def resolve_wallet(repo: Repo, adapters: dict, address: str, now_ms: int,
                         only_inactive: bool = False, only_venue: Optional[str] = None) -> dict[str, Optional[list[VenueAccount]]]:
    """{venue: accounts found, or None when the adapter failed}. Updates venue_accounts.

    only_inactive: skip venues the wallet is already active on (daily rescan).
    only_venue: /add <venue>:<address>: resolve that venue alone and activate the address there even
    when the adapter finds nothing (an address that exists on one venue only).
    """
    address = address.lower()
    existing = await repo.venue_accounts_of(address)
    active_venues = {row['venue'] for row in existing if row['active']}
    targets = [(venue, adapter) for venue, adapter in adapters.items()
               if not (only_inactive and venue in active_venues) and (only_venue is None or venue == only_venue)]
    if only_venue is not None and only_venue not in adapters:
        return {}
    results = await asyncio.gather(*(adapter.resolve(address) for _, adapter in targets), return_exceptions=True)
    out: dict[str, Optional[list[VenueAccount]]] = {}
    for (venue, _), result in zip(targets, results):
        if isinstance(result, BaseException):
            logger.error(f"{venue} resolve failed for {address}: {result}")
            out[venue] = None
            continue
        accounts: list[VenueAccount] = list(result)
        if only_venue is not None and not accounts and venue != base.LIGHTER:
            accounts = [VenueAccount(venue, address, address, meta={'forced': True})]
        found = {a.account_ref for a in accounts}
        for account in accounts:
            key, created = await repo.ensure_venue_account(address, venue, account.account_ref, True, now_ms,
                                                           account.meta)
            account.venue_account_id = key
            if created:
                logger.info(f"{venue} account {account.account_ref} added for {address}")
        for row in existing:
            if row['venue'] == venue and row['account_ref'] not in found and row['active']:
                await repo.ensure_venue_account(address, venue, row['account_ref'], False, now_ms)
                logger.info(f"{venue} account {row['account_ref']} inactive for {address}")
        out[venue] = accounts
    return out


VENUE_PREFIXES = {'hl': base.HYPERLIQUID, 'hyperliquid': base.HYPERLIQUID, 'lighter': base.LIGHTER,
                  'ltr': base.LIGHTER, 'risex': base.RISEX, 'rise': base.RISEX, 'aster': base.ASTER}


def split_venue_prefix(text: str) -> tuple[Optional[str], str]:
    """'risex:0xabc' -> ('risex', '0xabc'); '0xabc' -> (None, '0xabc'). Unknown prefixes stay in the text."""
    if ':' in text:
        prefix, rest = text.split(':', 1)
        venue = VENUE_PREFIXES.get(prefix.strip().lower())
        if venue is not None:
            return venue, rest.strip()
    return None, text


def resolve_summary(results: dict[str, Optional[list[VenueAccount]]], hl_dexs: Optional[list[str]] = None) -> str:
    """'HL ✅ · dex: xyz · Lighter ✅ (2 sub-accounts) · RISEx ✗ · Aster ✗' for the venues that were tried."""
    parts = []
    for venue in VENUE_ORDER:
        if venue not in results:
            continue
        accounts = results[venue]
        name = NAMES.get(venue, venue)
        if accounts is None:
            parts.append(f"{name} ? (error)")
        elif not accounts:
            parts.append(f"{name} ✗")
        elif venue == base.HYPERLIQUID:
            parts.append(f"{name} ✅" + (f" · dex: {', '.join(hl_dexs)}" if hl_dexs else ""))
        elif venue == base.LIGHTER:
            n = len(accounts)
            parts.append(f"{name} ✅ ({n} sub-account{'s' if n != 1 else ''})")
        elif (accounts[0].meta or {}).get('forced'):
            parts.append(f"{name} ✅ (added as given, no activity seen)")
        elif venue == base.ASTER:
            privacy = (accounts[0].meta or {}).get('privacy')
            parts.append(f"{name} ✅" + (f" (privacy: {'on' if privacy == 'enabled' else 'off'})" if privacy else ""))
        else:
            parts.append(f"{name} ✅")
    return " · ".join(parts)
