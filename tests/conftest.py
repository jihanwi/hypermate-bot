import pytest

from hypermate.config import Config
from hypermate.db.repo import Repo


@pytest.fixture(autouse=True)
def no_min_notional(monkeypatch):
    """Phase 0 to 2 tests use small synthetic fills; the $1,000 floor (deploy review 2026-10-05) is
    exercised by its own tests, which set it explicitly."""
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_USD', 0)


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / 'data' / 'hypermate.db'))
    await r.connect()
    yield r
    await r.close()
