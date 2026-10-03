"""Explorer links per venue."""


def hl_address_url(address: str) -> str:
    return f"https://hypurrscan.io/address/{address}"


def hl_address_url_fallback(address: str) -> str:
    return f"https://app.hyperliquid.xyz/explorer/address/{address}"


LIGHTER_APP_URL = "https://app.lighter.xyz/"     # official explorer URL format not confirmed [?]
RISEX_APP_URL = "https://app.rise.trade/"        # RISE chain explorer address format not confirmed [?]
ASTER_APP_URL = "https://www.asterdex.com/"      # Aster Chain explorer address format not confirmed [?]


def address_url(venue: str, address: str) -> str:
    """Explorer link for an alert header, per venue."""
    if venue == 'lighter':
        return LIGHTER_APP_URL
    if venue == 'risex':
        return RISEX_APP_URL
    if venue == 'aster':
        return ASTER_APP_URL
    return hl_address_url(address)
