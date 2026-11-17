"""Pruebas del sub-bloque 3e: migración e3e_service_ids (completa appointments.service_id). Datos 100 % de prueba.

Cada prueba arma una base "ya migrada con e3a" con turnos sin service_id. Corre en SQLite (archivo temporal)
y, si existe E3_TEST_PG_URL, también en PostgreSQL (schema propio que se borra al final).
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from app import models
from app.core import schema as schema_guard
from scripts import migrate_e3 as mig_e3a
from scripts import migrate_e3e as mig

CLIENT_NAME, CLIENT_PHONE = "Cliente Secreto", "2619999999"

# (id, servicio, service_id ya puesto, precio, duración, estado, barbero, descuento)
APPOINTMENTS = [
    (1, "Corte", 1, 15000, 30, "confirmed", 1, None),           # de antes de e3a: ya vinculado
    (2, "Corte", None, 15000, 30, "confirmed", 1, None),        # reservado después de e3a
    (3, "Corte", None, 12000, 45, "cancelled", 2, None),
    (4, "Corte + Barba", None, 18000, 45, "confirmed", 2, None),
    (5, "Navaja", None, 9000, 20, "confirmed", 1, None),        # servicio DESACTIVADO: igual se vincula
    (6, "Peinado", None, 7000, 30, "confirmed", 1, None),       # no está en el catálogo: queda NULL
    (7, "corte", None, 15000, 30, "confirmed", 2, None),        # otra mayúscula: no es el nombre exacto
    (8, "Barba", None, 9000, 15, "confirmed", 2, "Promo vieja"),
]


def quiet(*_args):
    pass


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def service_ids(engine):
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT id, service_id FROM appointments ORDER BY id")).all())


def everything(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT id, client_name, client_phone, service, date, time, duration, price, "
                                 "status, employee_id, discount_id, discount_name FROM appointments ORDER BY id")).all()


def _load(engine, with_e3a=True):
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO businesses (id, slug, name, active, created_at) "
                          "VALUES (1, 'mensclub', 'Men''s Club', true, CURRENT_TIMESTAMP), "
                          "(2, 'otro', 'Otro', true, CURRENT_TIMESTAMP)"))
        for name, uname, role in [("Dueño", "dueno", "OWNER"), ("Empleado", "emp", "EMPLOYEE")]:
            conn.execute(text(
                "INSERT INTO users (name, username, hashed_password, role, is_active, duration_corte, "
                "duration_barba, duration_corte_barba, price_corte, price_corte_promo2x, price_corte_vip, "
                "price_barba, price_corte_barba) VALUES (:n, :u, 'x', :r, true, 30, 15, 45, 15000, 12000, "
                "10000, 10000, 18000)"), {"n": name, "u": uname, "r": role})
        for business, name, active in [(1, "Corte", True), (1, "Barba", True), (1, "Corte + Barba", True),
                                       (1, "Navaja", False), (2, "Peinado", True)]:   # Peinado es de OTRO negocio
            conn.execute(text("INSERT INTO services (business_id, name, price, base_duration, active, created_at, "
                              "updated_at) VALUES (:b, :n, 1000, 30, :a, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                         {"b": business, "n": name, "a": active})
        for aid, service, sid, price, duration, st, emp, disc in APPOINTMENTS:
            conn.execute(text(
                "INSERT INTO appointments (id, client_name, client_phone, service, date, time, duration, price, "
                "status, employee_id, service_id, discount_name) VALUES (:id, :cn, :cp, :s, '2026-11-20', "
                "'15:00:00', :d, :p, :st, :e, :sid, :dn)"),
                {"id": aid, "cn": CLIENT_NAME, "cp": CLIENT_PHONE, "s": service, "d": duration, "p": price,
                 "st": st, "e": emp, "sid": sid, "dn": disc})
        if with_e3a:
            conn.execute(text("INSERT INTO schema_migrations (id, applied_at) VALUES ('e3a_catalogo', CURRENT_TIMESTAMP)"))


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    if request.param == "postgres":
        url = os.getenv("E3_TEST_PG_URL")
        if not url:
            pytest.skip("sin E3_TEST_PG_URL: se saltean las pruebas en PostgreSQL")
        name = f"t_{uuid.uuid4().hex[:10]}"
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {name}"))
        sep = "&" if "?" in url else "?"
        engine = mig.make_engine(f"{url}{sep}options=-csearch_path%3D{name}")
    else:
        engine = mig.make_engine(f"sqlite:///{tmp_path / 'e3e.db'}")
    _load(engine)
    yield engine
    engine.dispose()
    if request.param == "postgres":
        with admin.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '10s'"))
            conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        admin.dispose()


def test_completa_por_nombre_exacto_y_no_inventa(db, events):
    before = everything(db)
    result = mig.migrate(db, out=quiet)
    assert result["status"] == "applied"
    assert result["without_service_id_before"] == 7 and result["without_service_id_after"] == 2
    assert result["linked"] == {"Corte": 2, "Barba": 1, "Corte + Barba": 1, "Navaja": 1}
    assert result["unknown_names"] == {"Peinado": 1, "corte": 1}
    assert service_ids(db) == {1: 1, 2: 1, 3: 1, 4: 3, 5: 4, 6: None, 7: None, 8: 2}
    # Solo cambió service_id: todo lo demás (precio, duración, estado, descuento congelado...) igual
    assert everything(db) == before
    assert scalar(db, "SELECT COUNT(*) FROM schema_migrations WHERE id = 'e3e_service_ids'") == 1
    [event] = [e for e in events if e["event_type"] == "SCHEMA_MIGRATION_APPLIED"]
    assert event["metadata"]["without_service_id_before"] == 7 and event["metadata"]["linked"] == 5
    assert CLIENT_NAME not in str(event) and CLIENT_PHONE not in str(event)


def test_un_servicio_renombrado_con_turnos_ya_vinculados_no_impide_migrar(db):
    """Desde el 3d el dueño puede renombrar: el turno 1 (ya vinculado, "Corte") apunta a "Corte clásico".
    La verificación mira solo los turnos que vincula esta corrida, así que aplica igual."""
    with db.begin() as conn:
        conn.execute(text("UPDATE services SET name = 'Corte clásico' WHERE id = 1"))
    before = everything(db)
    result = mig.migrate(db, out=quiet)
    assert result["status"] == "applied"
    assert service_ids(db)[1] == 1                                   # el ya vinculado queda como estaba
    # Los "Corte" sin service_id ya no tienen un servicio con ese nombre exacto: se informan, no se inventan
    assert result["unknown_names"] == {"Corte": 2, "Peinado": 1, "corte": 1}
    assert result["linked"] == {"Barba": 1, "Corte + Barba": 1, "Navaja": 1}
    assert everything(db) == before


def test_dry_run_no_cambia_nada(db, events):
    before = everything(db), service_ids(db)
    result = mig.migrate(db, dry_run=True, out=quiet)
    assert result["status"] == "dry_run" and result["without_service_id_after"] == 2
    assert (everything(db), service_ids(db)) == before
    assert scalar(db, "SELECT COUNT(*) FROM schema_migrations WHERE id = 'e3e_service_ids'") == 0
    assert [e for e in events if e["event_type"] == "SCHEMA_MIGRATION_APPLIED"] == []


def test_dos_veces_no_hace_nada_la_segunda(db):
    mig.migrate(db, out=quiet)
    after = service_ids(db)
    assert mig.migrate(db, out=quiet) == {"status": "already_applied"}
    assert service_ids(db) == after


def test_sin_e3a_aborta(tmp_path):
    engine = mig.make_engine(f"sqlite:///{tmp_path / 'sin-e3a.db'}")
    _load(engine, with_e3a=False)
    with pytest.raises(mig.MigrationError, match="Falta la migración e3a_catalogo"):
        mig.migrate(engine, out=quiet)
    assert service_ids(engine)[2] is None
    engine.dispose()


def test_si_la_verificacion_falla_se_deshace_todo(db, monkeypatch):
    def link_and_break(conn):
        linked = original(conn)
        conn.execute(text("UPDATE appointments SET price = price + 1 WHERE id = 2"))
        return linked
    original = mig._link
    monkeypatch.setattr(mig, "_link", link_and_break)
    before = everything(db), service_ids(db)
    with pytest.raises(mig.MigrationError, match="Verificación falló"):
        mig.migrate(db, out=quiet)
    assert (everything(db), service_ids(db)) == before
    assert scalar(db, "SELECT COUNT(*) FROM schema_migrations WHERE id = 'e3e_service_ids'") == 0


def test_cli_dry_run_aplica_y_avisa_los_nombres_desconocidos(db, monkeypatch, capsys):
    import app.database
    monkeypatch.setattr(app.database, "SQLALCHEMY_DATABASE_URL", db.url.render_as_string(hide_password=False))
    assert mig.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "SIMULACIÓN" in out and "'Peinado' (1 turnos)" in out
    assert mig.main([]) == 0
    assert "MIGRACIÓN APLICADA" in capsys.readouterr().out
    assert mig.main([]) == 0
    assert "ya estaba aplicada" in capsys.readouterr().out


def test_la_app_exige_e3e(tmp_path):
    engine = mig.make_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    _load(engine)
    assert schema_guard.missing_migrations(engine) == ["e3e_service_ids"]
    with pytest.raises(RuntimeError, match="scripts.migrate_e3e"):
        schema_guard.ensure_schema_ready(engine)
    mig.migrate(engine, out=quiet)
    schema_guard.ensure_schema_ready(engine)
    engine.dispose()


def test_base_nueva_con_las_dos_migraciones(tmp_path):
    """init + e3a + e3e en una base sin turnos: e3e no tiene nada que completar y queda anotada."""
    engine = mig.make_engine(f"sqlite:///{tmp_path / 'nueva.db'}")
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (name, username, hashed_password, role, is_active, duration_corte, duration_barba, "
            "duration_corte_barba, price_corte, price_corte_promo2x, price_corte_vip, price_barba, "
            "price_corte_barba) VALUES ('Dueño', 'dueno', 'x', 'OWNER', true, 30, 15, 45, 15000, 12000, 10000, "
            "10000, 18000)"))
    mig_e3a.migrate(engine, out=quiet)
    result = mig.migrate(engine, out=quiet)
    assert result["status"] == "applied" and result["without_service_id_before"] == 0 and result["linked"] == {}
    assert schema_guard.missing_migrations(engine) == []
    engine.dispose()
