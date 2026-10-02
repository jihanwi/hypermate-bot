"""Explorer links per venue."""


def hl_address_url(address: str) -> str:
    return f"https://hypurrscan.io/address/{address}"


def hl_address_url_fallback(address: str) -> str:
    return f"https://app.hyperliquid.xyz/explorer/address/{address}"
