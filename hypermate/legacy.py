"""Legacy in-memory wallet list (removed together with the B1 fix)."""

from typing import Dict, List

# In-memory storage for tracked wallets per user.
# Nothing populates it anymore; commands still read it until the B1 fix.
user_wallets: Dict[int, List[Dict[str, str]]] = {}
