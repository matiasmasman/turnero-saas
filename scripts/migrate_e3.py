"""
Migración e3a_catalogo (Entrega 3, sub-bloque 3a): tablas del catálogo + columnas nuevas en appointments.

Uso (con la app PARADA, desde la carpeta del proyecto; lee el .env igual que la app):
    venv/bin/python -m scripts.migrate_e3 --dry-run   # hace todo, muestra qué haría y deshace (rollback)
    venv/bin/python -m scripts.migrate_e3             # la aplica

Qué hace, todo en UNA transacción (si algo falla, no queda nada a medias):
1. Chequeos previos: existen las tablas de hoy, hay exactamente un OWNER, todos los turnos
   tienen un servicio conocido ("Corte", "Barba", "Corte + Barba") y las tablas nuevas están vacías.
2. Crea businesses, services, discounts, discount_prices, barber_services y schema_migrations,
   y agrega a appointments service_id, discount_id y discount_name (nulas).
3. Semilla: negocio 1 "mensclub"; los 3 servicios con los precios del OWNER; cada usuario hace los
   3 servicios con SU duración actual (copiada tal cual). Sin descuentos.
4. Completa appointments.service_id por nombre exacto.
5. Verifica que no se perdió ni cambió nada (conteos, sumas, duraciones, precios).
6. Anota la migración en schema_migrations y deja SCHEMA_MIGRATION_APPLIED en security.log.

No borra ni renombra nada. Si ya estaba aplicada, no hace nada y lo dice.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:          # también funciona como `python scripts/migrate_e3.py`
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, event, insert, inspect, select, text  # noqa: E402
from sqlalchemy.engine import Connection, Engine  # noqa: E402

from app import models  # noqa: E402
from app.models import UserRole, utc_now  # noqa: E402

MIGRATION_ID = "e3a_catalogo"

BUSINESS = {"id": 1, "slug": "mensclub", "name": "Men's Club"}

# (nombre exacto, columna de precio del OWNER, columna de duración de cada usuario, duración base)
SEED_SERVICES = [
    ("Corte", "price_corte", "duration_corte", 30),
    ("Barba", "price_barba", "duration_barba", 15),
    ("Corte + Barba", "price_corte_barba", "duration_corte_barba", 45),
]
SERVICE_NAMES = [s[0] for s in SEED_SERVICES]

NEW_TABLES = [
    models.Business.__table__,
    models.Service.__table__,
    models.Discount.__table__,
    models.DiscountPrice.__table__,
    models.BarberService.__table__,
    models.SchemaMigration.__table__,
]

# El mismo SQL sirve para PostgreSQL 16 y SQLite (SQLite acepta REFERENCES en ADD COLUMN si es nula)
NEW_APPOINTMENT_COLUMNS = [
    ("service_id", "INTEGER CONSTRAINT fk_appointments_service_id "
                   "REFERENCES services (id) ON DELETE RESTRICT"),
    ("discount_id", "INTEGER CONSTRAINT fk_appointments_discount_id "
                    "REFERENCES discounts (id) ON DELETE RESTRICT"),
    ("discount_name", "VARCHAR(40)"),
]

EXISTING_TABLES = ["users", "appointments", "working_hours", "schedule_exceptions",
                   "wa_optins", "wa_notifications"]
COUNTED_TABLES = ["users", "working_hours", "schedule_exceptions", "wa_optins", "wa_notifications"]

users_t = models.User.__table__


class MigrationError(Exception):
    """La migración no se puede hacer o una verificación no cerró. No se cambió nada."""


# ---------- Engine ----------
def _sqlite_connect(dbapi_conn, _record):
    # pysqlite abre transacciones solo antes de INSERT/UPDATE/DELETE: un CREATE/ALTER queda
    # fuera y NO se deshace con rollback. Le sacamos ese manejo y mandamos BEGIN nosotros.
    dbapi_conn.isolation_level = None


def _sqlite_begin(conn):
    conn.exec_driver_sql("BEGIN")


def make_engine(url: str) -> Engine:
    """Engine para migrar. En SQLite activa DDL transaccional (ver _sqlite_connect)."""
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_connect)
        event.listen(engine, "begin", _sqlite_begin)
    return engine


def _check_engine(engine: Engine):
    if engine.dialect.name == "sqlite" and not event.contains(engine, "begin", _sqlite_begin):
        raise MigrationError("En SQLite hay que crear el engine con make_engine(): si no, "
                             "el rollback no deshace CREATE/ALTER.")
    if engine.dialect.name not in ("sqlite", "postgresql"):
        raise MigrationError(f"Base no soportada: {engine.dialect.name}")


# ---------- Pasos ----------
def _already_applied(conn: Connection) -> bool:
    if not inspect(conn).has_table("schema_migrations"):
        return False
    return conn.execute(text("SELECT 1 FROM schema_migrations WHERE id = :id"),
                        {"id": MIGRATION_ID}).first() is not None


def _preflight(conn: Connection):
    insp = inspect(conn)
    missing = [t for t in EXISTING_TABLES if not insp.has_table(t)]
    if missing:
        raise MigrationError(f"Faltan tablas: {', '.join(missing)}. Si la base es nueva, "
                             "corré primero: venv/bin/python manage_staff.py init")

    owners = conn.execute(
        select(users_t.c.id, users_t.c.price_corte, users_t.c.price_barba, users_t.c.price_corte_barba,
               users_t.c.price_corte_promo2x, users_t.c.price_corte_vip)
        .where(users_t.c.role == UserRole.OWNER)
    ).all()
    if len(owners) != 1:
        raise MigrationError(f"Tiene que haber exactamente 1 usuario OWNER y hay {len(owners)}.")

    unknown = conn.execute(
        text("SELECT service, COUNT(*) FROM appointments "
             "WHERE service NOT IN ('Corte', 'Barba', 'Corte + Barba') GROUP BY service ORDER BY service")
    ).all()
    if unknown:
        detail = ", ".join(f"{s!r} ({n} turnos)" for s, n in unknown)
        raise MigrationError(f"Hay turnos con servicios desconocidos: {detail}. No se inventa nada.")

    for table in NEW_TABLES:
        if table.name != "schema_migrations" and insp.has_table(table.name):
            n = conn.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar_one()
            if n:
                raise MigrationError(f"La tabla {table.name} ya tiene {n} filas sin que la migración "
                                     "esté anotada. Revisar a mano antes de seguir.")

    appt_cols = {c["name"] for c in insp.get_columns("appointments")}
    for col in ("service_id", "discount_id", "discount_name"):
        if col in appt_cols:
            n = conn.execute(text(f"SELECT COUNT(*) FROM appointments WHERE {col} IS NOT NULL")).scalar_one()
            if n:
                raise MigrationError(f"appointments.{col} ya tiene {n} valores. Revisar a mano.")

    return owners[0]._mapping


def _snapshot(conn: Connection) -> dict:
    count, total = conn.execute(text("SELECT COUNT(*), COALESCE(SUM(price), 0) FROM appointments")).one()
    groups = conn.execute(text(
        "SELECT service, price, duration, status, COUNT(*) FROM appointments "
        "GROUP BY service, price, duration, status"
    )).all()
    return {
        "appointments": int(count),
        "appointments_price_sum": int(total),
        "appointments_groups": sorted((tuple(g) for g in groups), key=repr),
        "tables": {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in COUNTED_TABLES},
    }


def _create_schema(conn: Connection):
    insp = inspect(conn)
    created = [t.name for t in NEW_TABLES if not insp.has_table(t.name)]
    models.Base.metadata.create_all(conn, tables=NEW_TABLES)   # solo crea las que faltan

    existing = {c["name"] for c in inspect(conn).get_columns("appointments")}
    added = []
    for name, ddl in NEW_APPOINTMENT_COLUMNS:
        if name not in existing:
            conn.execute(text(f"ALTER TABLE appointments ADD COLUMN {name} {ddl}"))
            added.append(name)
    return created, added


def _seed(conn: Connection, owner) -> dict:
    now = utc_now()
    conn.execute(insert(models.Business.__table__).values(**BUSINESS, active=True, created_at=now))
    if conn.dialect.name == "postgresql":
        # Insertamos id 1 a mano: hay que avisarle a la secuencia, o el próximo negocio chocaría con el 1
        conn.execute(text("SELECT setval(pg_get_serial_sequence('businesses', 'id'), "
                          "(SELECT MAX(id) FROM businesses))"))

    for name, price_col, _, base in SEED_SERVICES:
        conn.execute(insert(models.Service.__table__).values(
            business_id=BUSINESS["id"], name=name, price=owner[price_col], base_duration=base,
            active=True, created_at=now, updated_at=now))
    service_ids = dict(conn.execute(text(
        "SELECT name, id FROM services WHERE business_id = :b"), {"b": BUSINESS["id"]}).all())

    users = conn.execute(select(users_t.c.id, users_t.c.duration_corte, users_t.c.duration_barba,
                                users_t.c.duration_corte_barba)).all()
    rows = [
        {"business_id": BUSINESS["id"], "employee_id": u.id, "service_id": service_ids[name],
         "duration": getattr(u, dur_col), "active": True, "created_at": now, "updated_at": now}
        for u in users for name, _, dur_col, _ in SEED_SERVICES
    ]
    if rows:
        conn.execute(insert(models.BarberService.__table__), rows)
    return service_ids


def _link_appointments(conn: Connection, service_ids: dict) -> dict:
    linked = {}
    for name in SERVICE_NAMES:
        result = conn.execute(
            text("UPDATE appointments SET service_id = :sid WHERE service = :name AND service_id IS NULL"),
            {"sid": service_ids[name], "name": name})
        linked[name] = result.rowcount
    return linked


def _verify(conn: Connection, before: dict, owner) -> None:
    problems = []
    after = _snapshot(conn)
    for key in ("appointments", "appointments_price_sum", "appointments_groups", "tables"):
        if after[key] != before[key]:
            problems.append(f"{key} cambió: antes={before[key]} después={after[key]}")

    q = lambda sql: conn.execute(text(sql)).scalar_one()  # noqa: E731
    if q("SELECT COUNT(*) FROM appointments WHERE service_id IS NULL"):
        problems.append("hay turnos sin service_id")
    if q("SELECT COUNT(*) FROM appointments a JOIN services s ON s.id = a.service_id WHERE s.name <> a.service"):
        problems.append("hay turnos vinculados al servicio equivocado")
    if q("SELECT COUNT(*) FROM appointments WHERE discount_id IS NOT NULL OR discount_name IS NOT NULL"):
        problems.append("hay turnos con descuento")

    biz = conn.execute(text("SELECT id, slug, name, active FROM businesses")).all()
    if [tuple(b) for b in biz] != [(1, "mensclub", "Men's Club", True)]:
        problems.append(f"businesses no es la fila esperada: {biz}")

    services = {r.name: r for r in conn.execute(text(
        "SELECT name, price, base_duration, business_id, active FROM services"))}
    if sorted(services) != sorted(SERVICE_NAMES):
        problems.append(f"servicios inesperados: {sorted(services)}")
    else:
        for name, price_col, _, base in SEED_SERVICES:
            s = services[name]
            if (s.price, s.base_duration, s.business_id, bool(s.active)) != (owner[price_col], base, 1, True):
                problems.append(f"servicio {name!r} mal cargado")

    if q("SELECT COUNT(*) FROM discounts") or q("SELECT COUNT(*) FROM discount_prices"):
        problems.append("hay descuentos y no debería haber")

    n_users = before["tables"]["users"]
    n_bs = q("SELECT COUNT(*) FROM barber_services")
    if n_bs != n_users * len(SEED_SERVICES):
        problems.append(f"barber_services tiene {n_bs} filas y deberían ser {n_users} × 3")
    mismatched = q(
        "SELECT COUNT(*) FROM barber_services bs "
        "JOIN users u ON u.id = bs.employee_id JOIN services s ON s.id = bs.service_id "
        "WHERE NOT ((s.name = 'Corte' AND bs.duration = u.duration_corte) "
        "        OR (s.name = 'Barba' AND bs.duration = u.duration_barba) "
        "        OR (s.name = 'Corte + Barba' AND bs.duration = u.duration_corte_barba))")
    if mismatched:
        problems.append(f"{mismatched} filas de barber_services con duración distinta a users.duration_*")

    if problems:
        raise MigrationError("Verificación falló: " + "; ".join(problems))


# ---------- Principal ----------
def migrate(engine: Engine, dry_run: bool = False, out=print) -> dict:
    """Aplica la migración (o la simula con dry_run). Devuelve un resumen solo con conteos."""
    _check_engine(engine)
    started = time.monotonic()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            if _already_applied(conn):
                trans.rollback()
                out(f"La migración {MIGRATION_ID} ya estaba aplicada. No se hizo nada.")
                return {"status": "already_applied"}

            owner = _preflight(conn)
            before = _snapshot(conn)
            created, added = _create_schema(conn)
            service_ids = _seed(conn, owner)
            linked = _link_appointments(conn, service_ids)
            _verify(conn, before, owner)
            conn.execute(insert(models.SchemaMigration.__table__).values(id=MIGRATION_ID, applied_at=utc_now()))

            summary = {
                "status": "dry_run" if dry_run else "applied",
                "migration_id": MIGRATION_ID,
                "dialect": engine.dialect.name,
                "tables_created": created,
                "appointment_columns_added": added,
                "services": {n: {"price": owner[p], "base_duration": b} for n, p, _, b in SEED_SERVICES},
                "barber_services": before["tables"]["users"] * len(SEED_SERVICES),
                "appointments_linked": linked,
                "counts": {"appointments": before["appointments"], **before["tables"]},
                "appointments_price_sum": before["appointments_price_sum"],
                # Solo para el registro: NO se migran (los descuentos los crea el dueño después)
                "old_discount_prices": {"promo2x": owner["price_corte_promo2x"],
                                        "vip": owner["price_corte_vip"]},
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
            k: summary[k] for k in ("migration_id", "dialect", "tables_created", "appointment_columns_added",
                                    "barber_services", "appointments_linked", "counts",
                                    "old_discount_prices", "duration_ms")})
    return summary


def _log(level, event_type, message, metadata):
    from app.core.telemetry import sec_logger   # acá: así las pruebas usan su logger en memoria
    getattr(sec_logger, level)(message, extra={"event_type": event_type, "metadata": metadata})


def _ars(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


def _print_summary(s: dict, out):
    title = "SIMULACIÓN (--dry-run): se deshizo todo, la base quedó igual" if s["status"] == "dry_run" \
        else "MIGRACIÓN APLICADA"
    out(f"== {title} — {s['migration_id']} ({s['dialect']}, {s['duration_ms']} ms)")
    out(f"Tablas nuevas: {', '.join(s['tables_created']) or '(ya existían)'}")
    out(f"Columnas nuevas en appointments: {', '.join(s['appointment_columns_added']) or '(ya existían)'}")
    for name, v in s["services"].items():
        out(f"Servicio {name!r}: {_ars(v['price'])} · base {v['base_duration']} min")
    out(f"barber_services: {s['barber_services']} filas ({s['counts']['users']} usuarios × 3)")
    out("Turnos vinculados: " + ", ".join(f"{n} {c}" for n, c in s["appointments_linked"].items()))
    out("Conteos (iguales antes y después): " + ", ".join(f"{t} {n}" for t, n in s["counts"].items())
        + f", suma de precios {_ars(s['appointments_price_sum'])}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"Migración {MIGRATION_ID} (Entrega 3a)")
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
