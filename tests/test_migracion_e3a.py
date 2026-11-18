"""Pruebas del sub-bloque 3a: migración e3a_catalogo y protección de arranque. Datos 100 % de prueba.

Cada prueba arma su propia base con SQL que imita el esquema de hoy:
- "produccion": como PostgreSQL de producción (sin defaults, employee_id NOT NULL, con índices).
- "local": con la deriva de turnero_dev.db (defaults viejos, employee_id nulo, sin índices).
- "postgres": el esquema de producción en un PostgreSQL de prueba. Solo corre si existe la variable
  E3_TEST_PG_URL (ej. postgresql+psycopg2://usuario@/postgres?host=/ruta/socket&port=5499).
  Cada prueba usa un schema propio que se borra al final.
"""
import importlib
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from app.core import schema as schema_guard
from scripts import migrate_e3 as mig
from scripts import migrate_e3e as mig_e3e
from scripts import migrate_b4 as mig_b4

ROOT = mig.ROOT
CLIENT_NAMES = ["Cliente Uno", "Cliente Dos", "Cliente Tres"]
CLIENT_PHONES = ["2610000001", "2610000002", "2610000003"]

# ---------- Esquemas de hoy ----------
SQLITE_PRODUCCION = """
CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, username VARCHAR NOT NULL,
  hashed_password VARCHAR NOT NULL, role VARCHAR(8) NOT NULL, is_active BOOLEAN,
  duration_corte INTEGER NOT NULL, duration_barba INTEGER NOT NULL, duration_corte_barba INTEGER NOT NULL,
  price_corte INTEGER NOT NULL, price_corte_promo2x INTEGER NOT NULL, price_corte_vip INTEGER NOT NULL,
  price_barba INTEGER NOT NULL, price_corte_barba INTEGER NOT NULL);
CREATE INDEX ix_users_id ON users (id);
CREATE UNIQUE INDEX ix_users_username ON users (username);
CREATE TABLE appointments (id INTEGER NOT NULL PRIMARY KEY, client_name VARCHAR NOT NULL,
  client_phone VARCHAR NOT NULL, service VARCHAR NOT NULL, date VARCHAR NOT NULL, time VARCHAR NOT NULL,
  duration INTEGER NOT NULL, price INTEGER NOT NULL, status VARCHAR,
  employee_id INTEGER NOT NULL REFERENCES users (id));
CREATE INDEX ix_appointments_client_name ON appointments (client_name);
CREATE INDEX ix_appointments_date ON appointments (date);
CREATE INDEX ix_appointments_id ON appointments (id);
CREATE TABLE working_hours (id INTEGER NOT NULL PRIMARY KEY,
  employee_id INTEGER REFERENCES users (id) ON DELETE CASCADE, day_of_week INTEGER, is_working BOOLEAN,
  shift1_start TIME, shift1_end TIME, shift2_start TIME, shift2_end TIME);
CREATE TABLE schedule_exceptions (id INTEGER NOT NULL PRIMARY KEY, date VARCHAR NOT NULL, start_time VARCHAR,
  end_time VARCHAR, reason VARCHAR NOT NULL, employee_id INTEGER REFERENCES users (id));
CREATE TABLE wa_optins (appointment_id INTEGER NOT NULL PRIMARY KEY
  REFERENCES appointments (id) ON DELETE CASCADE, code VARCHAR(6) NOT NULL, jid VARCHAR,
  opted_in_at DATETIME, created_at DATETIME NOT NULL);
CREATE UNIQUE INDEX ix_wa_optins_code ON wa_optins (code);
CREATE TABLE wa_notifications (id INTEGER NOT NULL PRIMARY KEY,
  appointment_id INTEGER REFERENCES appointments (id) ON DELETE CASCADE, kind VARCHAR NOT NULL,
  to_jid VARCHAR NOT NULL, scheduled_at DATETIME NOT NULL, status VARCHAR NOT NULL, attempts INTEGER NOT NULL,
  last_error VARCHAR, sent_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, body TEXT,
  CONSTRAINT uq_wa_notif_appt_kind UNIQUE (appointment_id, kind))
"""

# Deriva de turnero_dev.db: defaults viejos agregados con ALTER, employee_id nulo, sin índices de appointments
SQLITE_LOCAL = (SQLITE_PRODUCCION
                .replace("duration_corte INTEGER NOT NULL,", "duration_corte INTEGER NOT NULL DEFAULT 45,")
                .replace("price_corte INTEGER NOT NULL,", "price_corte INTEGER NOT NULL DEFAULT 8000,")
                .replace("duration INTEGER NOT NULL, price INTEGER NOT NULL,",
                         "duration INTEGER NOT NULL DEFAULT 45, price INTEGER NOT NULL DEFAULT 0,")
                .replace("employee_id INTEGER NOT NULL REFERENCES users (id));",
                         "employee_id INTEGER REFERENCES users (id));")
                .replace("CREATE INDEX ix_appointments_client_name ON appointments (client_name);", "")
                .replace("CREATE INDEX ix_appointments_date ON appointments (date);", ""))

POSTGRES_PRODUCCION = (SQLITE_PRODUCCION
                       .replace("CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY",
                                "CREATE TYPE userrole AS ENUM ('OWNER', 'EMPLOYEE');\n"
                                "CREATE TABLE users (id SERIAL PRIMARY KEY")
                       .replace("role VARCHAR(8) NOT NULL", "role userrole NOT NULL")
                       .replace("CREATE TABLE appointments (id INTEGER NOT NULL PRIMARY KEY",
                                "CREATE TABLE appointments (id SERIAL PRIMARY KEY")
                       .replace("CREATE TABLE working_hours (id INTEGER NOT NULL PRIMARY KEY",
                                "CREATE TABLE working_hours (id SERIAL PRIMARY KEY")
                       .replace("CREATE TABLE schedule_exceptions (id INTEGER NOT NULL PRIMARY KEY",
                                "CREATE TABLE schedule_exceptions (id SERIAL PRIMARY KEY")
                       .replace("CREATE TABLE wa_notifications (id INTEGER NOT NULL PRIMARY KEY",
                                "CREATE TABLE wa_notifications (id SERIAL PRIMARY KEY")
                       .replace("DATETIME", "TIMESTAMP WITHOUT TIME ZONE"))

# ---------- Datos de prueba ----------
# OWNER con precios distintos a los defaults del código, para ver que se copian de verdad.
# id 2: Corte + Barba (60) distinto de Corte + Barba sumados (45 + 15). id 4: inactivo.
USERS = """
INSERT INTO users VALUES (1, 'Dueño', 'dueno', 'x', 'OWNER', TRUE, 35, 15, 50, 16000, 12500, 9500, 11000, 19000);
INSERT INTO users VALUES (2, 'Barbero A', 'barbero_a', 'x', 'EMPLOYEE', TRUE, 45, 15, 60, 8000, 12000, 10000, 5000, 12000);
INSERT INTO users VALUES (3, 'Barbero B', 'barbero_b', 'x', 'EMPLOYEE', TRUE, 30, 10, 40, 8000, 12000, 10000, 5000, 12000);
INSERT INTO users VALUES (4, 'Barbero C', 'barbero_c', 'x', 'EMPLOYEE', FALSE, 40, 20, 60, 8000, 12000, 10000, 5000, 12000)
"""
APPOINTMENTS = [
    # (id, service, duration, price, status, employee_id) — incluye duraciones viejas y uno cancelado
    (1, "Corte", 35, 16000, "confirmed", 1),
    (2, "Corte", 45, 15000, "confirmed", 2),
    (3, "Corte", 45, 12000, "confirmed", 2),       # le aplicaron Promo 2x desde la agenda
    (4, "Barba", 30, 10000, "confirmed", 3),       # duración vieja (antes Barba eran 30)
    (5, "Corte + Barba", 60, 18000, "confirmed", 2),
    (6, "Corte", 30, 15000, "cancelled", 3),       # canceló por WhatsApp
    (7, "Corte + Barba", 45, 18000, "confirmed", 4),
]
OTHERS = """
INSERT INTO working_hours VALUES (1, 1, 0, TRUE, '10:00:00', '13:00:00', '17:00:00', '20:30:00');
INSERT INTO working_hours VALUES (2, 2, 0, TRUE, '10:00:00', '13:00:00', NULL, NULL);
INSERT INTO working_hours VALUES (3, 3, 6, FALSE, NULL, NULL, NULL, NULL);
INSERT INTO schedule_exceptions VALUES (1, '2026-12-25', NULL, NULL, 'Bloqueo', NULL);
INSERT INTO schedule_exceptions VALUES (2, '2026-12-01', '10:00', '12:00', 'Bloqueo', 2);
INSERT INTO wa_optins VALUES (1, 'K2BF8T', '1000000000001@lid', '2026-11-14 12:00:00', '2026-11-14 11:00:00');
INSERT INTO wa_optins VALUES (6, 'M3CG9U', NULL, NULL, '2026-11-14 11:00:00');
INSERT INTO wa_notifications VALUES (1, 1, 'confirmation', '1000000000001@lid', '2026-11-14 12:00:00', 'sent', 1,
  NULL, '2026-11-14 12:00:05', '2026-11-14 12:00:00', '2026-11-14 12:00:05', NULL);
INSERT INTO wa_notifications VALUES (2, NULL, 'autoreply', '1000000000002@lid', '2026-11-14 12:00:00', 'sent', 1,
  NULL, '2026-11-14 12:00:05', '2026-11-14 12:00:00', '2026-11-14 12:00:05', NULL)
"""


def _run_sql(engine, script):
    with engine.begin() as conn:
        for stmt in script.split(";"):
            if stmt.strip():
                conn.execute(text(stmt))


def _load_data(engine):
    _run_sql(engine, USERS)
    with engine.begin() as conn:
        for i, (aid, service, duration, price, status, emp) in enumerate(APPOINTMENTS):
            conn.execute(text(
                "INSERT INTO appointments (id, client_name, client_phone, service, date, time, duration, price, "
                "status, employee_id) VALUES (:id, :n, :p, :s, '2026-11-20', '15:00:00', :d, :pr, :st, :e)"),
                {"id": aid, "n": CLIENT_NAMES[i % 3], "p": CLIENT_PHONES[i % 3], "s": service, "d": duration,
                 "pr": price, "st": status, "e": emp})
    _run_sql(engine, OTHERS)


def _new_db(kind, tmp_path, with_data=True):
    """Devuelve (engine, limpieza). kind: produccion | local | postgres."""
    if kind == "postgres":
        url = os.getenv("E3_TEST_PG_URL")
        if not url:
            pytest.skip("sin E3_TEST_PG_URL: se saltean las pruebas en PostgreSQL")
        name = f"t_{uuid.uuid4().hex[:10]}"
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {name}"))
        sep = "&" if "?" in url else "?"
        engine = mig.make_engine(f"{url}{sep}options=-csearch_path%3D{name}")
        ddl = POSTGRES_PRODUCCION

        def cleanup():
            engine.dispose()
            with admin.begin() as conn:
                conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
            admin.dispose()
    else:
        engine = mig.make_engine(f"sqlite:///{tmp_path / f'{kind}.db'}")
        ddl = SQLITE_PRODUCCION if kind == "produccion" else SQLITE_LOCAL
        cleanup = engine.dispose
    _run_sql(engine, ddl)
    if with_data:
        _load_data(engine)
    return engine, cleanup


@pytest.fixture(params=["produccion", "local", "postgres"])
def db(request, tmp_path):
    engine, cleanup = _new_db(request.param, tmp_path)
    yield engine
    cleanup()


@pytest.fixture
def sqlite_db(tmp_path):
    engine, cleanup = _new_db("produccion", tmp_path)
    yield engine
    cleanup()


def snapshot(engine):
    """Foto completa de la base: tablas, columnas, FKs, todas las filas y el DDL de índices
    (leído del catálogo, porque el inspector no ve los índices por expresión como lower(name))."""
    with engine.connect() as conn:
        insp = inspect(conn)
        result = {}
        for t in sorted(insp.get_table_names()):
            rows = conn.execute(text(f"SELECT * FROM {t}")).all()
            result[t] = {
                "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(t)],
                "fks": sorted(repr(sorted(f.items())) for f in insp.get_foreign_keys(t)),
                "rows": sorted((tuple(r) for r in rows), key=repr),
            }
        if conn.dialect.name == "sqlite":
            catalog = conn.execute(text("SELECT type, name, tbl_name, sql FROM sqlite_master")).all()
        else:
            catalog = conn.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                "UNION ALL SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = current_schema()::regnamespace")).all()
        result["_catalog"] = sorted((tuple(r) for r in catalog), key=repr)
        return result


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def quiet(*_args):
    pass


# ---------- La migración ----------
def test_migra_y_cumple_todas_las_verificaciones(db, events):
    before = snapshot(db)
    result = mig.migrate(db, out=quiet)
    assert result["status"] == "applied"

    after = snapshot(db)
    # Las tablas de hoy tienen exactamente las mismas filas (appointments además gana 3 columnas)
    for t in ["users", "working_hours", "schedule_exceptions", "wa_optins", "wa_notifications"]:
        assert after[t]["rows"] == before[t]["rows"], t
    assert [c[0] for c in after["appointments"]["columns"]] == \
        [c[0] for c in before["appointments"]["columns"]] + ["service_id", "discount_id", "discount_name"]
    assert scalar(db, "SELECT SUM(price) FROM appointments") == sum(a[3] for a in APPOINTMENTS)

    with db.connect() as conn:
        assert [tuple(r) for r in conn.execute(text("SELECT id, slug, name FROM businesses"))] == \
            [(1, "mensclub", "Men's Club")]
        services = {r.name: (r.price, r.base_duration, r.business_id, bool(r.active)) for r in conn.execute(
            text("SELECT name, price, base_duration, business_id, active FROM services"))}
        assert services == {"Corte": (16000, 30, 1, True), "Barba": (11000, 15, 1, True),
                            "Corte + Barba": (19000, 45, 1, True)}
        ids = dict(conn.execute(text("SELECT name, id FROM services")).all())

        bs = {(r.employee_id, r.name): r.duration for r in conn.execute(text(
            "SELECT bs.employee_id, s.name, bs.duration FROM barber_services bs "
            "JOIN services s ON s.id = bs.service_id WHERE bs.active = :t AND bs.business_id = 1"), {"t": True})}
        assert bs == {
            (1, "Corte"): 35, (1, "Barba"): 15, (1, "Corte + Barba"): 50,
            (2, "Corte"): 45, (2, "Barba"): 15, (2, "Corte + Barba"): 60,   # 60, no 45 + 15
            (3, "Corte"): 30, (3, "Barba"): 10, (3, "Corte + Barba"): 40,
            (4, "Corte"): 40, (4, "Barba"): 20, (4, "Corte + Barba"): 60,   # el inactivo también
        }

        appts = {r.id: (r.service_id, r.discount_id, r.discount_name, r.price, r.duration, r.status)
                 for r in conn.execute(text("SELECT * FROM appointments"))}
        for aid, service, duration, price, status, _ in APPOINTMENTS:
            assert appts[aid] == (ids[service], None, None, price, duration, status)

        assert conn.execute(text("SELECT COUNT(*) FROM discounts")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM discount_prices")).scalar_one() == 0
        assert [r[0] for r in conn.execute(text("SELECT id FROM schema_migrations"))] == ["e3a_catalogo"]

    applied = [e for e in events if e["event_type"] == "SCHEMA_MIGRATION_APPLIED"]
    assert len(applied) == 1
    assert applied[0]["metadata"]["counts"] == {"appointments": 7, "users": 4, "working_hours": 3,
                                                "schedule_exceptions": 2, "wa_optins": 2, "wa_notifications": 2}
    assert applied[0]["metadata"]["appointments_linked"] == {"Corte": 4, "Barba": 1, "Corte + Barba": 2}
    # Decisión J: los precios viejos de Promo 2x y VIP del OWNER quedan registrados, pero no se migran
    assert applied[0]["metadata"]["old_discount_prices"] == {"promo2x": 12500, "vip": 9500}
    dump = repr(events)
    for secret in CLIENT_NAMES + CLIENT_PHONES + ["1000000000001@lid"]:
        assert secret not in dump


def test_nombre_unico_sin_distinguir_mayusculas_y_negocio_obligatorio(db):
    from sqlalchemy.exc import IntegrityError
    mig.migrate(db, out=quiet)
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(text("INSERT INTO services (business_id, name, price, base_duration, active, "
                              "created_at, updated_at) VALUES (1, 'CORTE', 1, 30, TRUE, '2026-11-15', '2026-11-15')"))
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(text("INSERT INTO services (name, price, base_duration, active, created_at, updated_at) "
                              "VALUES ('Sin negocio', 1, 30, TRUE, '2026-11-15', '2026-11-15')"))
    with db.begin() as conn:   # mismo nombre de descuento distinto en mayúsculas: también choca
        conn.execute(text("INSERT INTO discounts (business_id, name, active, created_at, updated_at) "
                          "VALUES (1, 'Promo 2x', TRUE, '2026-11-15', '2026-11-15')"))
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(text("INSERT INTO discounts (business_id, name, active, created_at, updated_at) "
                              "VALUES (1, 'PROMO 2X', TRUE, '2026-11-15', '2026-11-15')"))


def test_postgres_descuento_y_servicio_tienen_que_ser_del_mismo_negocio(db):
    if db.dialect.name != "postgresql":
        pytest.skip("SQLite no aplica claves foráneas (PRAGMA foreign_keys = 0)")
    from sqlalchemy.exc import IntegrityError
    mig.migrate(db, out=quiet)
    with db.begin() as conn:
        # Sin id: así también probamos que setval dejó la secuencia de businesses después del 1
        biz2 = conn.execute(text("INSERT INTO businesses (slug, name) VALUES ('otro', 'Otro') RETURNING id")
                            ).scalar_one()
        svc2 = conn.execute(text(
            "INSERT INTO services (business_id, name, price, base_duration, created_at, updated_at) "
            "VALUES (:b, 'Corte', 1, 30, now(), now()) RETURNING id"), {"b": biz2}).scalar_one()
        disc1 = conn.execute(text(
            "INSERT INTO discounts (business_id, name, created_at, updated_at) "
            "VALUES (1, 'VIP', now(), now()) RETURNING id")).scalar_one()
    assert biz2 == 2
    with pytest.raises(IntegrityError, match="fk_discount_prices_service"):
        with db.begin() as conn:
            conn.execute(text("INSERT INTO discount_prices (business_id, discount_id, service_id, price, "
                              "created_at, updated_at) VALUES (1, :d, :s, 1, now(), now())"),
                         {"d": disc1, "s": svc2})
    with pytest.raises(IntegrityError, match="foreign key"):   # servicios no se borran: se desactivan
        with db.begin() as conn:
            conn.execute(text("DELETE FROM services WHERE name = 'Barba' AND business_id = 1"))


def test_segunda_vez_no_cambia_nada(db, events):
    mig.migrate(db, out=quiet)
    before = snapshot(db)
    printed = []
    assert mig.migrate(db, out=printed.append) == {"status": "already_applied"}
    assert snapshot(db) == before
    assert "ya estaba aplicada" in printed[0]
    assert len([e for e in events if e["event_type"] == "SCHEMA_MIGRATION_APPLIED"]) == 1


def test_dry_run_no_cambia_nada(db, events):
    before = snapshot(db)
    printed = []
    result = mig.migrate(db, dry_run=True, out=printed.append)
    assert result["status"] == "dry_run"
    assert result["appointments_linked"] == {"Corte": 4, "Barba": 1, "Corte + Barba": 2}
    assert snapshot(db) == before
    assert "SIMULACIÓN" in printed[0]
    assert [e for e in events if e["event_type"] == "SCHEMA_MIGRATION_APPLIED"] == []
    # y después se puede aplicar de verdad
    assert mig.migrate(db, out=quiet)["status"] == "applied"


@pytest.mark.parametrize("paso", ["_seed", "_link_appointments", "_verify"])
def test_error_a_mitad_deja_la_base_exactamente_igual(db, monkeypatch, paso):
    """Falla después de CREATE TABLE y ALTER TABLE: tienen que deshacerse también."""
    before = snapshot(db)

    def falla(*args, **kwargs):
        raise RuntimeError("error forzado")
    monkeypatch.setattr(mig, paso, falla)
    with pytest.raises(RuntimeError, match="error forzado"):
        mig.migrate(db, out=quiet)
    assert snapshot(db) == before
    assert "services" not in inspect(db).get_table_names()


def test_verificacion_que_no_cierra_aborta(db, monkeypatch):
    before = snapshot(db)
    real_link = mig._link_appointments

    def link_y_rompe(conn, service_ids):
        linked = real_link(conn, service_ids)
        conn.execute(text("UPDATE appointments SET price = price + 1 WHERE id = 1"))
        return linked
    monkeypatch.setattr(mig, "_link_appointments", link_y_rompe)
    with pytest.raises(mig.MigrationError, match="appointments_price_sum cambió"):
        mig.migrate(db, out=quiet)
    assert snapshot(db) == before


def test_servicio_desconocido_aborta_sin_cambiar_nada(db):
    with db.begin() as conn:
        conn.execute(text("UPDATE appointments SET service = 'Corte y Barba' WHERE id = 5"))
    before = snapshot(db)
    with pytest.raises(mig.MigrationError, match="'Corte y Barba' \\(1 turnos\\)"):
        mig.migrate(db, out=quiet)
    assert snapshot(db) == before


@pytest.mark.parametrize("cambio, cuantos", [
    ("UPDATE users SET role = 'EMPLOYEE' WHERE id = 1", 0),
    ("UPDATE users SET role = 'OWNER' WHERE id = 2", 2),
])
def test_cero_o_dos_owner_aborta_sin_cambiar_nada(db, cambio, cuantos):
    with db.begin() as conn:
        conn.execute(text(cambio))
    before = snapshot(db)
    with pytest.raises(mig.MigrationError, match=f"exactamente 1 usuario OWNER y hay {cuantos}"):
        mig.migrate(db, out=quiet)
    assert snapshot(db) == before


def test_base_vacia_aborta_y_dice_que_hacer(tmp_path):
    engine = mig.make_engine(f"sqlite:///{tmp_path / 'vacia.db'}")
    with pytest.raises(mig.MigrationError, match="manage_staff.py init"):
        mig.migrate(engine, out=quiet)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_sqlite_sin_make_engine_se_rechaza(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with pytest.raises(mig.MigrationError, match="make_engine"):
        mig.migrate(engine, out=quiet)
    engine.dispose()


def test_sqlite_sin_el_arreglo_no_deshace_el_ddl(tmp_path):
    """Documenta por qué existe make_engine(): con el driver tal como viene, el rollback no
    deshace un CREATE TABLE. Si un día SQLAlchemy o pysqlite cambian esto, esta prueba avisa."""
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with pytest.raises(RuntimeError):
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
            raise RuntimeError("error forzado")
    assert inspect(engine).has_table("t")      # quedó creada a pesar del rollback
    engine.dispose()


def test_cli_lee_la_base_de_la_config_de_la_app(sqlite_db, monkeypatch, capsys):
    import app.database
    monkeypatch.setattr(app.database, "SQLALCHEMY_DATABASE_URL", str(sqlite_db.url))
    assert mig.main(["--dry-run"]) == 0
    assert "SIMULACIÓN" in capsys.readouterr().out
    assert mig.main([]) == 0
    assert mig.main([]) == 0                    # segunda vez: ya aplicada, sale bien
    assert "ya estaba aplicada" in capsys.readouterr().out
    with sqlite_db.begin() as conn:
        conn.execute(text("DELETE FROM schema_migrations"))
    assert mig.main([]) == 1                    # tablas con datos sin la migración anotada: aborta
    assert "ABORTADO" in capsys.readouterr().out


# ---------- Base nueva y manage_staff ----------
@pytest.fixture
def staff_on(monkeypatch):
    """manage_staff.py apuntando a la base de prueba que se le pase.
    Se importa acá (no arriba) porque importa app.core.security, que exige SECRET_KEY."""
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    import manage_staff

    def use(engine):
        monkeypatch.setattr(manage_staff, "engine", engine)
        monkeypatch.setattr(manage_staff, "SessionLocal", sessionmaker(bind=engine))
        return manage_staff
    return use


def test_base_nueva_init_y_despues_migracion(tmp_path, staff_on, capsys):
    engine = mig.make_engine(f"sqlite:///{tmp_path / 'nueva.db'}")
    manage_staff = staff_on(engine)
    manage_staff.cmd_init("clave-de-prueba")
    assert "Falta la migración" in capsys.readouterr().out
    assert schema_guard.missing_migrations(engine) == ["e3a_catalogo", "e3e_service_ids", "b4_cancelled_by"]

    result = mig.migrate(engine, out=quiet)
    assert result["status"] == "applied"
    assert result["tables_created"] == []               # ya las había creado init (create_all)
    assert result["appointment_columns_added"] == []    # idem
    assert scalar(engine, "SELECT COUNT(*) FROM barber_services") == 4 * 3
    assert schema_guard.missing_migrations(engine) == ["e3e_service_ids", "b4_cancelled_by"]   # van después
    assert mig_e3e.migrate(engine, out=quiet)["status"] == "applied"
    assert schema_guard.missing_migrations(engine) == ["b4_cancelled_by"]
    assert mig_b4.migrate(engine, out=quiet)["columns_added"] == []      # ya las había creado init
    assert schema_guard.missing_migrations(engine) == []
    engine.dispose()


def test_barbero_creado_despues_de_migrar_no_tiene_servicios(sqlite_db, staff_on, capsys):
    mig.migrate(sqlite_db, out=quiet)
    manage_staff = staff_on(sqlite_db)
    manage_staff.cmd_add("Nuevo", "nuevo", "clave-de-prueba", "employee")
    assert "no tiene servicios asignados" in capsys.readouterr().out
    new_id = scalar(sqlite_db, "SELECT id FROM users WHERE username = 'nuevo'")
    assert scalar(sqlite_db, "SELECT COUNT(*) FROM barber_services WHERE employee_id = :i", i=new_id) == 0
    assert scalar(sqlite_db, "SELECT COUNT(*) FROM barber_services") == 4 * 3


# ---------- Protección de arranque ----------
def test_proteccion_sin_migracion_no_arranca(sqlite_db):
    with pytest.raises(RuntimeError, match="no tiene aplicada la migración e3a_catalogo"):
        schema_guard.ensure_schema_ready(sqlite_db)


def test_proteccion_base_vacia_no_arranca(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'vacia.db'}")
    with pytest.raises(RuntimeError, match="manage_staff.py init"):
        schema_guard.ensure_schema_ready(engine)
    engine.dispose()


def test_proteccion_con_migracion_arranca(sqlite_db):
    mig.migrate(sqlite_db, out=quiet)
    with pytest.raises(RuntimeError, match="no tiene aplicada la migración e3e_service_ids"):
        schema_guard.ensure_schema_ready(sqlite_db)  # falta la del 3e
    mig_e3e.migrate(sqlite_db, out=quiet)
    with pytest.raises(RuntimeError, match="no tiene aplicada la migración b4_cancelled_by"):
        schema_guard.ensure_schema_ready(sqlite_db)  # falta la del Bloque 4
    mig_b4.migrate(sqlite_db, out=quiet)
    schema_guard.ensure_schema_ready(sqlite_db)      # no levanta error


def _import_app_main(monkeypatch, engine):
    """Importa app.main de cero contra `engine` (es lo que hace uvicorn al arrancar)."""
    import app.database
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setattr(app.database, "engine", engine)
    monkeypatch.chdir(ROOT)                          # StaticFiles("frontend") es relativo
    sys.modules.pop("app.main", None)
    try:
        return importlib.import_module("app.main")
    finally:
        sys.modules.pop("app.main", None)


def test_app_no_arranca_sin_migracion(sqlite_db, monkeypatch):
    with pytest.raises(RuntimeError, match="scripts.migrate_e3"):
        _import_app_main(monkeypatch, sqlite_db)
    assert "services" not in inspect(sqlite_db).get_table_names()   # y no creó tablas por su cuenta


def test_app_arranca_con_migracion(sqlite_db, monkeypatch):
    mig.migrate(sqlite_db, out=quiet)
    mig_e3e.migrate(sqlite_db, out=quiet)
    mig_b4.migrate(sqlite_db, out=quiet)
    main = _import_app_main(monkeypatch, sqlite_db)
    assert main.app.title.startswith("Turnero SaaS")
