from app.database import engine
from app.models.base import Base
from app.models.equipo import Equipo
from sqlalchemy import inspect, text


def crear_tablas():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE reservas_equipos "
            "ADD COLUMN IF NOT EXISTS inicio TIMESTAMP WITH TIME ZONE, "
            "ADD COLUMN IF NOT EXISTS fin TIMESTAMP WITH TIME ZONE"
        ))

    print(inspect(engine).get_table_names())

if __name__ == "__main__":
    crear_tablas()