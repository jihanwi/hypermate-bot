import pytest

from hypermate.db.repo import Repo


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / 'data' / 'hypermate.db'))
    await r.connect()
    yield r
    await r.close()
