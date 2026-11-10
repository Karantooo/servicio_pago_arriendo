import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


DATABASE_URL = os.getenv(
    "PAYMENTS_DATABASE_URL",
    "sqlite:///./payments.db",
)

engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)
Base = declarative_base()


def initialize_database() -> None:
    from pagos import db_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
