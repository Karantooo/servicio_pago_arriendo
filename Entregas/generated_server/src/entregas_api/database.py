import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def _create_engine():
    database_url = os.getenv("DATABASE_URL", "sqlite:///./entregas.db")
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    options = {"connect_args": connect_args, "pool_pre_ping": True}
    if database_url in {"sqlite://", "sqlite:///:memory:"}:
        options["poolclass"] = StaticPool
    return create_engine(database_url, **options)


engine = _create_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
