"""Explorer links per venue."""
from typing import Optional


def hl_address_url(address: str) -> str:
    return f"https://hypurrscan.io/address/{address}"


def hl_address_url_fallback(address: str) -> str:
    return f"https://app.hyperliquid.xyz/explorer/address/{address}"


def lighter_account_url(account_key: str) -> str:
    """Lighter explorer account page; the route takes an account index or an L1 address (master account)."""
    return f"https://app.lighter.xyz/explorer/accounts/{account_key}"


def hyperevm_address_url(address: str) -> str:
    """Papertrade runs on HyperEVM: the wallet's page on HyperEVMScan (spec 6.7, 200 checked 2026-10-15)."""
    return f"https://hyperevmscan.io/address/{address}"


def address_url(venue: str, address: str) -> Optional[str]:
    """Explorer link for an alert header, per venue. None where no per-wallet page is known
    (RISEx, Aster, Arcus): the alias is shown as plain text instead of linking the venue home page."""
    if venue == 'lighter':
        return lighter_account_url(address)
    if venue == 'papertrade':
        return hyperevm_address_url(address)
    if venue in ('risex', 'aster', 'arcus'):
        return None
    return hl_address_url(address)
