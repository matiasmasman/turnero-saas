"""
Migración e3e_service_ids (Entrega 3, sub-bloque 3e): completa appointments.service_id en los turnos que
quedaron sin él (los reservados entre la migración e3a y el 3e: la reserva vieja no lo escribía).

Uso (con la app PARADA, desde la carpeta del proyecto; lee el .env igual que la app):
    venv/bin/python -m scripts.migrate_e3e --dry-run   # hace todo, muestra qué haría y deshace (rollback)
    venv/bin/python -m scripts.migrate_e3e             # la aplica

Qué hace, todo en UNA transacción (si algo falla, no queda nada a medias):
1. Chequeo previo: la migración e3a_catalogo tiene que estar aplicada.
2. Cuenta los turnos sin service_id, por nombre de servicio.
3. Les pone el id del servicio del negocio 1 con ese nombre EXACTO (activo o desactivado).
   Si un nombre no existe en el catálogo, lo informa y esos turnos quedan como están: no se inventa nada.
   (Los conteos de "turnos futuros" ya los encuentran por nombre cuando no tienen service_id.)
4. Verifica que solo cambió service_id: mismos turnos, misma suma de precios, mismos servicio/precio/
   duración/estado/barbero/descuento, y cada turno que vinculó ESTA corrida apunta a un servicio con su
   mismo nombre. Los que ya tenían service_id no se miran: desde el 3d el dueño puede renombrar servicios
   y el nombre congelado del turno queda distinto a propósito.
5. Anota la migración en schema_migrations y deja SCHEMA_MIGRATION_APPLIED en security.log.

No borra ni renombra nada. Si ya estaba aplicada, no hace nada y lo dice.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:          # también funciona como `python scripts/migrate_e3e.py`
    sys.path.insert(0, str(ROOT))

from sqlalchemy import bindparam, insert, inspect, text  # noqa: E402
from sqlalchemy.engine import Connection, Engine  # noqa: E402

from app import models  # noqa: E402
from app.models import utc_now  # noqa: E402
from scripts.migrate_e3 import MigrationError, _check_engine, _log, make_engine  # noqa: E402

MIGRATION_ID = "e3e_service_ids"
REQUIRES = "e3a_catalogo"
BUSINESS_ID = 1


def _applied(conn: Connection, migration_id: str) -> bool:
    return conn.execute(text("SELECT 1 FROM schema_migrations WHERE id = :id"),
                        {"id": migration_id}).first() is not None


def _null_ids(conn: Connection) -> list:
    return [r[0] for r in conn.execute(text("SELECT id FROM appointments WHERE service_id IS NULL"))]


def _missing_by_name(conn: Connection) -> dict:
    rows = conn.execute(text("SELECT service, COUNT(*) FROM appointments WHERE service_id IS NULL "
                             "GROUP BY service ORDER BY service")).all()
    return {name: int(n) for name, n in rows}


def _snapshot(conn: Connection) -> dict:
    count, total = conn.execute(text("SELECT COUNT(*), COALESCE(SUM(price), 0) FROM appointments")).one()
    groups = conn.execute(text(
        "SELECT service, price, duration, status, employee_id, discount_id, discount_name, COUNT(*) "
        "FROM appointments GROUP BY service, price, duration, status, employee_id, discount_id, discount_name"
    )).all()
    return {"appointments": int(count), "price_sum": int(total),
            "groups": sorted((tuple(g) for g in groups), key=repr)}


def _link(conn: Connection) -> dict:
    """UPDATE por nombre exacto, uno por servicio del catálogo. Devuelve {nombre: turnos vinculados}."""
    services = conn.execute(text("SELECT id, name FROM services WHERE business_id = :b ORDER BY id"),
                            {"b": BUSINESS_ID}).all()
    linked = {}
    for service_id, name in services:
        n = conn.execute(text("UPDATE appointments SET service_id = :sid "
                              "WHERE service_id IS NULL AND service = :name"),
                         {"sid": service_id, "name": name}).rowcount
        if n:
            linked[name] = n
    return linked


def _verify(conn: Connection, before: dict, candidate_ids: list) -> None:
    """candidate_ids: los turnos que tenían service_id NULL antes de esta corrida (los únicos que pudo tocar)."""
    problems = []
    after = _snapshot(conn)
    for key in ("appointments", "price_sum", "groups"):
        if after[key] != before[key]:
            problems.append(f"{key} cambió: antes={before[key]} después={after[key]}")
    wrong = 0
    if candidate_ids:
        wrong = conn.execute(text(
            "SELECT COUNT(*) FROM appointments a JOIN services s ON s.id = a.service_id "
            "WHERE a.id IN :ids AND (s.name <> a.service OR s.business_id <> :b)"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": candidate_ids, "b": BUSINESS_ID}).scalar_one()
    if wrong:
        problems.append(f"{wrong} turnos vinculados a un servicio con otro nombre")
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
                                     "venv/bin/python -m scripts.migrate_e3")
            if _applied(conn, MIGRATION_ID):
                trans.rollback()
                out(f"La migración {MIGRATION_ID} ya estaba aplicada. No se hizo nada.")
                return {"status": "already_applied"}

            before = _snapshot(conn)
            candidate_ids = _null_ids(conn)
            missing_before = _missing_by_name(conn)
            linked = _link(conn)
            missing_after = _missing_by_name(conn)            # nombres que no están en el catálogo
            _verify(conn, before, candidate_ids)
            conn.execute(insert(models.SchemaMigration.__table__).values(id=MIGRATION_ID, applied_at=utc_now()))

            summary = {
                "status": "dry_run" if dry_run else "applied",
                "migration_id": MIGRATION_ID,
                "dialect": engine.dialect.name,
                "appointments": before["appointments"],
                "without_service_id_before": sum(missing_before.values()),
                "without_service_id_after": sum(missing_after.values()),
                "linked": linked,
                "unknown_names": missing_after,
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
        # Los nombres de servicio no son datos de clientes; igual van solo los conteos
        _log("info", "SCHEMA_MIGRATION_APPLIED", f"Migración de esquema {MIGRATION_ID} aplicada", {
            "migration_id": MIGRATION_ID, "dialect": summary["dialect"],
            "without_service_id_before": summary["without_service_id_before"],
            "without_service_id_after": summary["without_service_id_after"],
            "linked": sum(linked.values()), "duration_ms": summary["duration_ms"]})
    return summary


def _print_summary(s: dict, out):
    title = "SIMULACIÓN (--dry-run): se deshizo todo, la base quedó igual" if s["status"] == "dry_run" \
        else "MIGRACIÓN APLICADA"
    out(f"== {title} — {s['migration_id']} ({s['dialect']}, {s['duration_ms']} ms)")
    out(f"Turnos: {s['appointments']} (iguales antes y después)")
    out(f"Sin service_id: antes {s['without_service_id_before']}, después {s['without_service_id_after']}")
    out("Vinculados: " + (", ".join(f"{n!r} {c}" for n, c in s["linked"].items()) or "ninguno"))
    if s["unknown_names"]:
        out("[!] Nombres que no están en el catálogo (esos turnos quedan sin service_id, no se inventa nada): "
            + ", ".join(f"{n!r} ({c} turnos)" for n, c in s["unknown_names"].items()))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"Migración {MIGRATION_ID} (Entrega 3e)")
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
