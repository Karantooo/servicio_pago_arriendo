import atexit
import os
import subprocess
import sys
import tempfile
import types
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parents[1]
_temporary_databases = tempfile.TemporaryDirectory(prefix="experimentos-saga-")
_database_dir = Path(_temporary_databases.name)

os.environ["DATABASE_URL"] = f"sqlite:///{_database_dir / 'arriendos.sqlite'}"
os.environ["ADMIN_API_KEY"] = "experiment-admin-key"
os.environ["CLIENT_API_KEY"] = "experiment-client-key"
os.environ["PAYMENTS_DATABASE_URL"] = f"sqlite:///{_database_dir / 'pagos.sqlite'}"
os.environ["PAYMENTS_PROVIDER"] = "mock"
os.environ["PAYMENTS_PROVIDER_MODE"] = "NORMAL"

_quiet_log_handler = logging.NullHandler()
_quiet_log_handler._service_json_formatter = True
logging.getLogger().addHandler(_quiet_log_handler)

for project_path in (ROOT / "arriendo", ROOT / "Pagos"):
    sys.path.insert(0, str(project_path))

_generated_dir = _database_dir / "generated"
_generated_dir.mkdir()
subprocess.run(
    [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{ROOT / 'Equipos'}",
        f"--python_out={_generated_dir}",
        f"--grpc_python_out={_generated_dir}",
        str(ROOT / "Equipos" / "equipos.proto"),
    ],
    check=True,
    capture_output=True,
    text=True,
)
_grpc_module = _generated_dir / "equipos_pb2_grpc.py"
_grpc_source = _grpc_module.read_text(encoding="utf-8")
_grpc_module.write_text(
    _grpc_source.replace(
        "import equipos_pb2 as equipos__pb2",
        "from . import equipos_pb2 as equipos__pb2",
    ),
    encoding="utf-8",
)
_generated_package = types.ModuleType("app.generated")
_generated_package.__path__ = [str(_generated_dir)]
sys.modules["app.generated"] = _generated_package

from app.bd_models import Base as ArriendosBase  # noqa: E402
from app import observability as arriendos_observability  # noqa: E402
from app.database import engine as arriendos_engine  # noqa: E402
from app.database import get_db as get_arriendos_db  # noqa: E402
from app.main import app as arriendos_app  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402

for collector in (
    arriendos_observability.http_request_duration_seconds,
    arriendos_observability.bulkhead_rejections_total,
    arriendos_observability.circuit_breaker_state,
):
    REGISTRY.unregister(collector)

from pagos.database import Base as PaymentsBase  # noqa: E402
from pagos.database import engine as payments_engine  # noqa: E402
from pagos.main import app as payments_app  # noqa: E402
from pagos.observability import bulkhead_rejections_total  # noqa: E402
from pagos.providers import get_provider  # noqa: E402
from pagos.resilience import circuit_breaker  # noqa: E402

for handler in tuple(logging.getLogger().handlers):
    if getattr(handler, "_service_json_formatter", False):
        logging.getLogger().removeHandler(handler)

_arriendos_engine = create_engine(
    os.environ["DATABASE_URL"],
    connect_args={"check_same_thread": False},
)
_ArriendosSession = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=_arriendos_engine,
    expire_on_commit=False,
)
atexit.register(_arriendos_engine.dispose)
atexit.register(arriendos_engine.dispose)
atexit.register(payments_engine.dispose)


@pytest.fixture
def db_session():
    ArriendosBase.metadata.drop_all(bind=_arriendos_engine)
    ArriendosBase.metadata.create_all(bind=_arriendos_engine)
    session = _ArriendosSession()
    try:
        yield session
    finally:
        session.close()
        ArriendosBase.metadata.drop_all(bind=_arriendos_engine)


@pytest.fixture
def arriendo_client(db_session):
    def override_get_db():
        yield db_session

    arriendos_app.dependency_overrides[get_arriendos_db] = override_get_db
    try:
        with TestClient(arriendos_app) as client:
            client.headers.update({"API-Key": os.environ["ADMIN_API_KEY"]})
            yield client
    finally:
        arriendos_app.dependency_overrides.clear()


@pytest.fixture
def payments_client():
    PaymentsBase.metadata.drop_all(bind=payments_engine)
    PaymentsBase.metadata.create_all(bind=payments_engine)
    circuit_breaker.on_success()
    get_provider().set_mode("NORMAL")
    bulkhead_rejections_total.labels(service="payments-api")._value.set(0)
    try:
        with TestClient(payments_app) as client:
            yield client
    finally:
        get_provider().set_mode("NORMAL")
        circuit_breaker.on_success()
        PaymentsBase.metadata.drop_all(bind=payments_engine)
