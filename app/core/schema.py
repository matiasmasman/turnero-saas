"""
Protección de arranque: la app no arranca si a la base le falta una migración.

La app ya NO crea tablas sola (antes lo hacía create_all en app/main.py). Las tablas las crean:
- una base nueva: `python manage_staff.py init` y después `python -m scripts.migrate_e3`
- una base existente: `python -m scripts.migrate_e3`
Así nunca queda una base "a medias" porque la app arrancó con código nuevo antes de migrar.
"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Migraciones que este código necesita, en orden. Para agregar una: sumarla acá y en scripts/.
REQUIRED_MIGRATIONS = ["e3a_catalogo"]

HOW_TO_MIGRATE = (
    "Corré la migración con la app parada, desde la carpeta del proyecto:\n"
    "    venv/bin/python -m scripts.migrate_e3 --dry-run   # muestra qué haría, no cambia nada\n"
    "    venv/bin/python -m scripts.migrate_e3             # la aplica\n"
    "Si la base está vacía, primero: venv/bin/python manage_staff.py init"
)


def missing_migrations(engine: Engine) -> list:
    """Las migraciones requeridas que la base todavía no tiene (lista vacía = todo en orden)."""
    with engine.connect() as conn:
        if not inspect(conn).has_table("schema_migrations"):
            return list(REQUIRED_MIGRATIONS)
        applied = {row[0] for row in conn.execute(text("SELECT id FROM schema_migrations"))}
    return [m for m in REQUIRED_MIGRATIONS if m not in applied]


def ensure_schema_ready(engine: Engine) -> None:
    missing = missing_migrations(engine)
    if missing:
        raise RuntimeError(
            f"La base de datos no tiene aplicada la migración {', '.join(missing)}. "
            f"La app no arranca para no trabajar con una base a medias.\n{HOW_TO_MIGRATE}"
        )
