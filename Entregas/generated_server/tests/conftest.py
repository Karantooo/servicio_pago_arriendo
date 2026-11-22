import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["CLIENT_API_KEY"] = "test-readonly-key"

import pytest
from fastapi.testclient import TestClient

from entregas_api.database import Base, engine
from entregas_api.main import app


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def reset_failure_injection(monkeypatch):
    monkeypatch.setenv("DELIVERIES_ENABLE_FAILURE_INJECTION", "false")
    from entregas_api.impl.failure_injection import set_failure_mode

    set_failure_mode("NORMAL")
    yield
    set_failure_mode("NORMAL")


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
