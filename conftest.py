import os
import tempfile
from pathlib import Path

_TMP_ROOT = Path(tempfile.gettempdir()) / "opencode" / "vyuha-tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
_TEST_DB = _TMP_ROOT / "vyuha-test.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

import pytest

from db.models import Base
from db.session import sync_engine


@pytest.fixture(scope="session", autouse=True)
def _create_test_schema():
    Base.metadata.create_all(bind=sync_engine)
    yield
    Base.metadata.drop_all(bind=sync_engine)
    try:
        _TEST_DB.unlink()
    except FileNotFoundError:
        pass
