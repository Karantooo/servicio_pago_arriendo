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
        pass
    else:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE sagas ADD COLUMN delivery_id BIGINT")
            )

    columns = {column["name"] for column in inspector.get_columns("sagas")}
    checkout_status = next(
        (column for column in inspector.get_columns("sagas") if column["name"] == "checkout_status"),
        None,
    )
    if (
        checkout_status is not None
        and engine.dialect.name == "postgresql"
        and getattr(checkout_status["type"], "length", None) is not None
        and checkout_status["type"].length < 30
    ):
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE sagas ALTER COLUMN checkout_status TYPE VARCHAR(30)")
            )
    with engine.begin() as connection:
        if "delivery_scheduled_at" not in columns:
            connection.execute(
                text("ALTER TABLE sagas ADD COLUMN delivery_scheduled_at TIMESTAMP WITH TIME ZONE")
            )
        if "refund_id" not in columns:
            connection.execute(
                text("ALTER TABLE sagas ADD COLUMN refund_id BIGINT")
            )
        if "refund_status" not in columns:
            connection.execute(
                text("ALTER TABLE sagas ADD COLUMN refund_status VARCHAR(20)")
            )
        if "equipment_release_status" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE sagas "
                    "ADD COLUMN equipment_release_status VARCHAR(20)"
                )
            )

def ensure_rental_reservation_column() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("rentals")}
    if "equipment_reservation_id" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE rentals ADD COLUMN equipment_reservation_id VARCHAR(36)")
            )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()