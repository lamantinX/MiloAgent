import pytest
from core.orchestrator import Orchestrator
from core.database import Database
from core.business_manager import BusinessManager

@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db

def test_cache_collision_regression(test_db, tmp_path):
    pass
