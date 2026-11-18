"""
Migración b4_cancelled_by (Bloque 4): quién y cuándo canceló un turno.

Uso (con la app PARADA, desde la carpeta del proyecto; lee el .env igual que la app):
    venv/bin/python -m scripts.migrate_b4 --dry-run   # hace todo, muestra qué haría y deshace (rollback)
    venv/bin/python -m scripts.migrate_b4             # la aplica

Qué hace, todo en UNA transacción (si algo falla, no queda nada a medias):
1. Chequeo previo: la migración e3e_service_ids tiene que estar aplicada.
2. Agrega a appointments dos columnas NULAS:
   - cancelled_by_id: el usuario del panel que canceló (FK a users, ON DELETE RESTRICT: los usuarios no se borran,
     se desactivan).
   - cancelled_at: cuándo (UTC, con zona).
   Si ya existen (base nueva hecha con `manage_staff.py init`, que crea las tablas desde el modelo), no las toca.
3. No completa nada: los turnos cancelados que ya existen son todos de WhatsApp, y "cancelado sin
   cancelled_by_id" es justamente "canceló por WhatsApp".
4. Verifica que no cambió ningún turno (conteo, suma de precios, servicio/precio/duración/estado/barbero) y que las
   dos columnas quedaron vacías.
5. Anota la migración en schema_migrations y deja SCHEMA_MIGRATION_APPLIED en security.log.

No borra ni renombra nada. Si ya estaba aplicada, no hace nada y lo dice.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:          # también funciona como `python scripts/migrate_b4.py`
    sys.path.insert(0, str(ROOT))

from sqlalchemy import insert, inspect, text  # noqa: E402
from sqlalchemy.engine import Connection, Engine  # noqa: E402

from app import models  # noqa: E402
from app.models import utc_now  # noqa: E402
from scripts.migrate_e3 import MigrationError, _check_engine, _log, make_engine  # noqa: E402

MIGRATION_ID = "b4_cancelled_by"
REQUIRES = "e3e_service_ids"

FK = "INTEGER CONSTRAINT fk_appointments_cancelled_by_id REFERENCES users (id) ON DELETE RESTRICT"
# (nombre, DDL en PostgreSQL, DDL en SQLite). SQLite acepta REFERENCES en ADD COLUMN si la columna es nula.
NEW_COLUMNS = [
    ("cancelled_by_id", FK, FK),
    ("cancelled_at", "TIMESTAMP WITH TIME ZONE", "DATETIME"),
]


def _applied(conn: Connection, migration_id: str) -> bool:
    return conn.execute(text("SELECT 1 FROM schema_migrations WHERE id = :id"),
                        {"id": migration_id}).first() is not None


def _snapshot(conn: Connection) -> dict:
    count, total = conn.execute(text("SELECT COUNT(*), COALESCE(SUM(price), 0) FROM appointments")).one()
    groups = conn.execute(text(
        "SELECT service, price, duration, status, employee_id, service_id, discount_id, discount_name, COUNT(*) "
        "FROM appointments GROUP BY service, price, duration, status, employee_id, service_id, discount_id, "
        "discount_name")).all()
    cancelled = conn.execute(text("SELECT COUNT(*) FROM appointments WHERE status = 'cancelled'")).scalar_one()
    return {"appointments": int(count), "price_sum": int(total), "cancelled": int(cancelled),
            "groups": sorted((tuple(g) for g in groups), key=repr)}


def _add_columns(conn: Connection) -> list:
    existing = {c["name"] for c in inspect(conn).get_columns("appointments")}
    added = []
    for name, ddl_pg, ddl_sqlite in NEW_COLUMNS:
        if name not in existing:
            ddl = ddl_pg if conn.dialect.name == "postgresql" else ddl_sqlite
            conn.execute(text(f"ALTER TABLE appointments ADD COLUMN {name} {ddl}"))
            added.append(name)
    return added


def _verify(conn: Connection, before: dict) -> None:
    problems = []
    after = _snapshot(conn)
    for key in ("appointments", "price_sum", "cancelled", "groups"):
        if after[key] != before[key]:
            problems.append(f"{key} cambió: antes={before[key]} después={after[key]}")
    cols = {c["name"] for c in inspect(conn).get_columns("appointments")}
    missing = [n for n, _, _ in NEW_COLUMNS if n not in cols]
    if missing:
        problems.append(f"faltan columnas: {missing}")
    else:
        filled = conn.execute(text("SELECT COUNT(*) FROM appointments "
                                   "WHERE cancelled_by_id IS NOT NULL OR cancelled_at IS NOT NULL")).scalar_one()
        if filled:
            problems.append(f"{filled} turnos ya tienen cancelled_by_id/cancelled_at (¿se migró a mano?)")
    if problems:
        raise MigrationError("Verificación falló: " + "; ".join(problems))


def migrate(engine: Engine, dry_run: bool = False, out=print) -> dict:
    """Aplica la migración (o la simula con dry_run). Devuelve un resumen solo con conteos."""
    _check_engine(engine)
    started = time.monotonic()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            if not inspect(conn).has_table("schema_migrations") or not _applied(conn, REQUIRES):
                raise MigrationError(f"Falta la migración {REQUIRES}. Corré primero: "
                                     "venv/bin/python -m scripts.migrate_e3e")
            if _applied(conn, MIGRATION_ID):
                trans.rollback()
                out(f"La migración {MIGRATION_ID} ya estaba aplicada. No se hizo nada.")
                return {"status": "already_applied"}

            before = _snapshot(conn)
            added = _add_columns(conn)
            _verify(conn, before)
            conn.execute(insert(models.SchemaMigration.__table__).values(id=MIGRATION_ID, applied_at=utc_now()))

            summary = {
                "status": "dry_run" if dry_run else "applied",
                "migration_id": MIGRATION_ID,
                "dialect": engine.dialect.name,
                "appointments": before["appointments"],
                "cancelled": before["cancelled"],
                "columns_added": added,
            }
            if dry_run:
                trans.rollback()
            else:
                trans.commit()
        except Exception:
            trans.rollback()
            raise

    summary["duration_ms"] = round((time.monotonic() - started) * 1000)
    _print_summary(summary, out)
    if not dry_run:
        _log("info", "SCHEMA_MIGRATION_APPLIED", f"Migración de esquema {MIGRATION_ID} aplicada", {
            "migration_id": MIGRATION_ID, "dialect": summary["dialect"],
            "columns_added": added, "duration_ms": summary["duration_ms"]})
    return summary


def _print_summary(s: dict, out):
    title = "SIMULACIÓN (--dry-run): se deshizo todo, la base quedó igual" if s["status"] == "dry_run" \
        else "MIGRACIÓN APLICADA"
    out(f"== {title} — {s['migration_id']} ({s['dialect']}, {s['duration_ms']} ms)")
    out(f"Turnos: {s['appointments']} (iguales antes y después); cancelados: {s['cancelled']} "
        "(quedan como 'Canceló por WhatsApp')")
    out("Columnas agregadas a appointments: " + (", ".join(s["columns_added"]) or "ninguna (ya estaban)"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"Migración {MIGRATION_ID} (Bloque 4)")
    parser.add_argument("--dry-run", action="store_true", help="hace todo, muestra qué haría y deshace")
    args = parser.parse_args(argv)

    from app.database import SQLALCHEMY_DATABASE_URL   # lee el .env igual que la app
    engine = make_engine(SQLALCHEMY_DATABASE_URL)
    print(f"Base: {engine.url.render_as_string(hide_password=True)}")
    try:
        migrate(engine, dry_run=args.dry_run)
    except MigrationError as e:
        print(f"ABORTADO, no se cambió nada: {e}")
        if not args.dry_run:
            _log("warning", "SCHEMA_MIGRATION_FAILED", f"Migración {MIGRATION_ID} abortada",
                 {"migration_id": MIGRATION_ID, "reason": str(e)[:300]})
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
