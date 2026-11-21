import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://arriendos_user:arriendos_password@localhost:5430/arriendo-db"
)

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def ensure_saga_delivery_column() -> None:
    inspector = inspect(engine)
    if "sagas" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("sagas")}
    if "delivery_id" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE sagas ADD COLUMN delivery_id BIGINT")
        )

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()