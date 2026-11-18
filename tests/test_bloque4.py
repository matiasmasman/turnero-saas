"""Pruebas del Bloque 4: cancelar desde el panel sin borrar + turnos completados. Datos 100 % de prueba.

- API (fixture `api`: SQLite en memoria y, con E3_TEST_PG_URL, también PostgreSQL): cancelar deja el turno con
  status='cancelled', quién y cuándo; anula los avisos pendientes; permisos de siempre; 409 si ya estaba cancelado;
  evento APPOINTMENT_CANCELLED con source='panel'; el horario queda libre; métricas y conteos sin cancelados;
  GET /admin/appointments con cancelled_by_name y el header X-Business-Now.
- Migración b4_cancelled_by (SQLite en archivo y, con E3_TEST_PG_URL, PostgreSQL).
- WhatsApp: la cancelación del cliente anota cancelled_at y deja cancelled_by_id vacío.
- panel.js: controles estáticos (el navegador de verdad está en test_navegador_bloque4.py).
"""
import os
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

import app.catalog as catalog
from app import models
from app.core import schema as schema_guard
from app.models import Appointment, Discount, ScheduleException, Service, WaNotification, WaOptin
from scripts import migrate_b4 as mig
from test_bloque3 import log, of  # noqa: F401  (log es un fixture)
from test_reserva_3e import ANA, BETO, CORTE, DAY, DUENO, NOW, PROMO, book, shop, slots  # noqa: F401
from test_whatsapp_textos import JID_A, chat, make_appt, rows  # noqa: F401  (chat es un fixture)

ROOT = Path(__file__).resolve().parent.parent
PANEL_JS = (ROOT / "frontend/js/panel.js").read_text(encoding="utf-8")
PANEL_CSS = (ROOT / "frontend/css/panel.css").read_text(encoding="utf-8")
PANEL_HTML = (ROOT / "frontend/panel.html").read_text(encoding="utf-8")
CLIENT_NAME, CLIENT_PHONE = "Cliente Prueba", "2610000000"      # los de test_reserva_3e.CLIENT


# ---------- Ayudas ----------
def headers(user_id, role):
    from app.core.security import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user_id), 'role': role})}"}


@pytest.fixture
def h(shop):
    """Tokens del dueño, Ana y Beto (los de `shop`)."""
    return {"dueno": headers(DUENO, "owner"), "ana": headers(ANA, "employee"), "beto": headers(BETO, "employee")}


def booked_id(api, **fields):
    resp = book(api, **fields)
    assert resp.status_code == 201, resp.text
    db = api.Session()
    try:
        return db.query(Appointment.id).order_by(Appointment.id.desc()).first()[0]
    finally:
        db.close()


def get_appt(api, appt_id):
    db = api.Session()
    try:
        a = db.query(Appointment).filter(Appointment.id == appt_id).first()
        return None if a is None else {c.name: getattr(a, c.name) for c in Appointment.__table__.columns}
    finally:
        db.close()


def cancel(api, appt_id, who):
    return api.client.delete(f"/admin/appointments/{appt_id}", headers=who)


# =====================================================================
# Cancelar = status 'cancelled', nunca borrar
# =====================================================================
def test_cancelar_no_borra_y_anota_quien_y_cuando(api, h):
    appt_id = booked_id(api)
    before = get_appt(api, appt_id)
    assert before["status"] == "confirmed" and before["cancelled_by_id"] is None and before["cancelled_at"] is None

    resp = cancel(api, appt_id, h["ana"])
    assert resp.status_code == 204 and resp.content == b""

    after = get_appt(api, appt_id)
    assert after is not None                                            # sigue en la base
    assert after["status"] == "cancelled" and after["cancelled_by_id"] == ANA and after["cancelled_at"] is not None
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"status", "cancelled_by_id", "cancelled_at"}     # nada más cambió (precio, servicio...)
    db = api.Session()
    assert db.query(WaOptin).filter(WaOptin.appointment_id == appt_id).count() == 1   # el código no se borra
    db.close()


def test_anula_los_avisos_pendientes_como_whatsapp(api, h):
    appt_id = booked_id(api)
    other_id = booked_id(api, time="11:00")
    db = api.Session()
    db.add_all([
        WaNotification(appointment_id=appt_id, kind="confirmation", to_jid=JID_A, scheduled_at=NOW, status="sent"),
        WaNotification(appointment_id=appt_id, kind="reminder", to_jid=JID_A, scheduled_at=NOW, status="pending"),
        WaNotification(appointment_id=other_id, kind="reminder", to_jid=JID_A, scheduled_at=NOW, status="pending"),
    ])
    db.commit()
    db.close()

    assert cancel(api, appt_id, h["ana"]).status_code == 204
    db = api.Session()
    got = {(n.appointment_id, n.kind): (n.status, n.last_error) for n in db.query(WaNotification)}
    db.close()
    assert got == {
        (appt_id, "confirmation"): ("sent", None),                                     # lo ya enviado, igual
        (appt_id, "reminder"): ("cancelled", "turno cancelado desde el panel"),        # anulado
        (other_id, "reminder"): ("pending", None),                                     # el de otro turno, igual
    }


# ---------- Permisos (los de siempre) ----------
def test_permisos_de_siempre(api, h):
    ana_appt = booked_id(api)                                  # de Ana
    assert cancel(api, ana_appt, h["beto"]).status_code == 404  # un barbero no cancela turnos ajenos
    assert get_appt(api, ana_appt)["status"] == "confirmed"
    assert cancel(api, 999999, h["ana"]).status_code == 404
    assert api.client.delete(f"/admin/appointments/{ana_appt}").status_code == 401
    assert cancel(api, ana_appt, h["dueno"]).status_code == 204  # el dueño cancela cualquiera
    assert get_appt(api, ana_appt)["cancelled_by_id"] == DUENO


@pytest.mark.parametrize("first", ["panel", "whatsapp"])
def test_cancelar_uno_ya_cancelado_da_409_y_no_cambia_nada(api, h, log, first):
    appt_id = booked_id(api)
    if first == "panel":
        assert cancel(api, appt_id, h["ana"]).status_code == 204
    else:                                                       # como lo deja la cancelación por WhatsApp
        db = api.Session()
        db.query(Appointment).filter(Appointment.id == appt_id).update({"status": "cancelled"})
        db.commit()
        db.close()
    before = get_appt(api, appt_id)
    events_before = len(of(log, "APPOINTMENT_CANCELLED"))

    resp = cancel(api, appt_id, h["dueno"])
    assert resp.status_code == 409 and resp.json()["detail"] == "Ese turno ya estaba cancelado."
    assert get_appt(api, appt_id) == before                    # sigue "Cancelado por Ana" / "por WhatsApp"
    assert len(of(log, "APPOINTMENT_CANCELLED")) == events_before


# ---------- Evento ----------
def test_evento_con_source_panel_y_sin_datos_del_cliente(api, h, log):
    appt_id = booked_id(api)
    db = api.Session()
    db.add(WaNotification(appointment_id=appt_id, kind="reminder", to_jid=JID_A, scheduled_at=NOW, status="pending"))
    db.commit()
    db.close()
    assert cancel(api, appt_id, h["dueno"]).status_code == 204
    [event] = of(log, "APPOINTMENT_CANCELLED")
    assert event["actor_id"] == DUENO and event["ip_address"] == "testclient" and event["request_id"]
    assert event["metadata"] == {"appointment_id": appt_id, "source": "panel", "employee_id": ANA,
                                 "notifications_voided": 1}
    assert CLIENT_NAME not in str(event) and CLIENT_PHONE not in str(event) and JID_A not in str(event)


# =====================================================================
# Los cancelados no cuentan en ningún lado
# =====================================================================
def test_el_horario_queda_libre_para_volver_a_reservar(api, h):
    first = booked_id(api)
    assert "10:00" not in slots(api, employee_id=ANA).json()
    assert book(api).status_code == 409                                     # anti doble reserva
    assert cancel(api, first, h["ana"]).status_code == 204

    assert "10:00" in slots(api, employee_id=ANA).json()                    # disponibilidad
    assert "10:00" in slots(api).json()                                     # y con "Cualquiera"
    second = booked_id(api)                                                 # anti doble reserva (FOR UPDATE en PG)
    assert second != first
    db = api.Session()
    same = db.query(Appointment.status).filter(Appointment.employee_id == ANA, Appointment.date == DAY,
                                               Appointment.time == "10:00:00").order_by(Appointment.id).all()
    db.close()
    assert [s for (s,) in same] == ["cancelled", "confirmed"]               # sin índice único que lo impida
    assert "10:00" not in slots(api, employee_id=ANA).json()
    assert book(api).status_code == 409                                     # el nuevo sí bloquea


def test_metricas_y_facturacion_sin_cancelados(api, h):
    ana_appt = booked_id(api)                                               # Corte $10.000
    booked_id(api, employee_id=BETO)                                        # Corte $10.000
    week = lambda: api.client.get("/admin/metrics?period=week", headers=h["dueno"]).json()  # noqa: E731
    assert (week()["total_appointments"], week()["total_revenue"]) == (2, 20000)
    assert cancel(api, ana_appt, h["ana"]).status_code == 204
    m = week()
    assert (m["total_appointments"], m["total_revenue"], m["pending_revenue"]) == (1, 10000, 10000)
    assert sum(p["appointments"] for p in m["chart_series"]) == 1
    mine = api.client.get("/admin/metrics?period=week", headers=h["ana"]).json()
    assert (mine["total_appointments"], mine["total_revenue"]) == (0, 0)


def test_conteos_de_turnos_futuros_sin_cancelados(api, h):
    ana_appt = booked_id(api, discount_id=PROMO)
    db = api.Session()
    corte = db.query(Service).get(CORTE)
    promo = db.query(Discount).get(PROMO)
    assert catalog.future_appointments_for_service(db, corte) == 1                     # catálogo (3b)
    assert catalog.future_appointments_for_discount(db, promo) == 1
    db.close()
    my = lambda: {s["service_id"]: s["future_appointments"]  # noqa: E731
                  for s in api.client.get("/admin/my-services", headers=h["ana"]).json()["services"]}
    assert my()[CORTE] == 1                                                            # Mis servicios (3c)

    assert cancel(api, ana_appt, h["ana"]).status_code == 204
    db = api.Session()
    assert catalog.future_appointments_for_service(db, db.query(Service).get(CORTE)) == 0
    assert catalog.future_appointments_for_discount(db, db.query(Discount).get(PROMO)) == 0
    db.close()
    assert my()[CORTE] == 0
    # Desactivar el servicio o el descuento ya no avisa de turnos futuros (pantalla "Servicios del local")
    resp = api.client.post(f"/admin/catalog/services/{CORTE}/deactivate", headers=h["dueno"])
    assert resp.status_code == 200 and resp.json()["future_appointments"] == 0
    resp = api.client.post(f"/admin/catalog/discounts/{PROMO}/deactivate", headers=h["dueno"])
    assert resp.status_code == 200 and resp.json()["future_appointments"] == 0


def test_bloqueo_de_horario_no_cuenta_los_cancelados(api, h):
    ana_appt = booked_id(api)
    booked_id(api, time="11:00")
    assert cancel(api, ana_appt, h["ana"]).status_code == 204
    resp = api.client.post("/admin/exceptions", headers=h["ana"], json={"date": DAY})
    assert resp.status_code == 201 and resp.json()["affected_appointments"] == 1


# =====================================================================
# Agenda: GET /admin/appointments
# =====================================================================
def test_agenda_trae_quien_cancelo_y_la_hora_del_negocio(api, h):
    by_panel = booked_id(api)
    by_wa = booked_id(api, time="11:00")
    still = booked_id(api, time="12:00")
    assert cancel(api, by_panel, h["dueno"]).status_code == 204
    db = api.Session()
    db.query(Appointment).filter(Appointment.id == by_wa).update({"status": "cancelled"})   # como WhatsApp
    db.commit()
    db.close()

    resp = api.client.get("/admin/appointments", headers=h["ana"])
    assert resp.status_code == 200
    assert resp.headers["X-Business-Now"] == "2030-06-05T09:00:00"            # catalog.now_local(), no el reloj real
    body = resp.json()
    assert isinstance(body, list)                                              # la forma de siempre
    got = {a["id"]: (a["status"], a["cancelled_by_name"], a["cancelled_at"] is not None) for a in body}
    assert got == {by_panel: ("cancelled", "Dueño", True), by_wa: ("cancelled", None, False),
                   still: ("confirmed", None, False)}
    assert api.client.get("/admin/appointments", headers=h["beto"]).json() == []   # cada barbero, lo suyo


def test_el_header_de_la_hora_es_el_del_negocio_cerca_de_la_medianoche(api, h, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(catalog, "now_local", lambda: datetime(2030, 6, 5, 23, 59, 30, 123456))
    resp = api.client.get("/admin/appointments", headers=h["ana"])
    assert resp.headers["X-Business-Now"] == "2030-06-05T23:59:30"            # sin microsegundos ni zona


# =====================================================================
# WhatsApp: el cliente cancela → cancelled_at sí, cancelled_by_id no ("Canceló por WhatsApp")
# =====================================================================
def test_cancelacion_por_whatsapp_anota_cuando_pero_no_quien(Session, clock, chat, events):
    appt_id = make_appt(Session, jid=JID_A)
    chat.send(JID_A, "CANCELAR")
    chat.send(JID_A, "SI")
    [appt] = rows(Session, Appointment, id=appt_id)
    assert appt.status == "cancelled" and appt.cancelled_at is not None and appt.cancelled_by_id is None
    [event] = [e for e in events if e["event_type"] == "WA_APPOINTMENT_CANCELLED"]
    assert event["metadata"]["source"] == "whatsapp"


def test_el_bot_no_cancela_de_nuevo_uno_cancelado_desde_el_panel(Session, clock, chat):
    appt_id = make_appt(Session, jid=JID_A)
    db = Session()
    db.query(Appointment).filter(Appointment.id == appt_id).update(
        {"status": "cancelled", "cancelled_by_id": 1, "cancelled_at": models.utc_now()})   # como el panel
    db.commit()
    db.close()
    chat.send(JID_A, "CANCELAR")
    assert len(rows(Session, WaNotification, kind="cancel_none")) == 1      # "No encontré turnos activos..."
    assert rows(Session, Appointment, id=appt_id)[0].cancelled_by_id == 1


# =====================================================================
# Migración b4_cancelled_by
# =====================================================================
OLD_APPOINTMENTS_DDL = (
    "CREATE TABLE appointments (id INTEGER PRIMARY KEY, client_name VARCHAR NOT NULL, "
    "client_phone VARCHAR NOT NULL, service VARCHAR NOT NULL, date VARCHAR NOT NULL, time VARCHAR NOT NULL, "
    "duration INTEGER NOT NULL, price INTEGER NOT NULL, status VARCHAR, "
    "employee_id INTEGER NOT NULL REFERENCES users (id), service_id INTEGER REFERENCES services (id), "
    "discount_id INTEGER REFERENCES discounts (id), discount_name VARCHAR(40))")
SECRET_NAME, SECRET_PHONE = "Cliente Secreto", "2619999999"


def _load_old(engine, applied=("e3a_catalogo", "e3e_service_ids")):
    """Una base como la de producción antes del Bloque 4: appointments sin las columnas nuevas."""
    tables = models.Base.metadata.sorted_tables
    first = [t for t in tables if t.name not in ("appointments", "wa_optins", "wa_notifications")]
    models.Base.metadata.create_all(engine, tables=first)
    with engine.begin() as conn:
        conn.execute(text(OLD_APPOINTMENTS_DDL))
    models.Base.metadata.create_all(engine, tables=[models.WaOptin.__table__, models.WaNotification.__table__])
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, name, username, hashed_password, role, is_active, duration_corte, "
            "duration_barba, duration_corte_barba, price_corte, price_corte_promo2x, price_corte_vip, price_barba, "
            "price_corte_barba) VALUES (1, 'Dueño', 'dueno', 'x', 'OWNER', true, 30, 15, 45, 1, 1, 1, 1, 1)"))
        for aid, st in [(1, "confirmed"), (2, "cancelled"), (3, "cancelled"), (4, "confirmed")]:
            conn.execute(text(
                "INSERT INTO appointments (id, client_name, client_phone, service, date, time, duration, price, "
                "status, employee_id) VALUES (:id, :cn, :cp, 'Corte', '2026-11-20', '15:00:00', 30, 1000, :st, 1)"),
                {"id": aid, "cn": SECRET_NAME, "cp": SECRET_PHONE, "st": st})
        for migration_id in applied:
            conn.execute(text("INSERT INTO schema_migrations (id, applied_at) VALUES (:i, CURRENT_TIMESTAMP)"),
                         {"i": migration_id})


@pytest.fixture(params=["sqlite", "postgres"])
def old_db(request, tmp_path):
    admin = None
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
        engine = mig.make_engine(f"sqlite:///{tmp_path / 'b4.db'}")
    _load_old(engine)
    yield engine
    engine.dispose()
    if admin is not None:
        with admin.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '10s'"))
            conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        admin.dispose()


def quiet(*_args):
    pass


def columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("appointments")}


def everything(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT id, client_name, client_phone, service, date, time, duration, price, "
                                 "status, employee_id, service_id FROM appointments ORDER BY id")).all()


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_migracion_agrega_las_columnas_y_no_toca_los_turnos(old_db, events):
    before = everything(old_db)
    assert "cancelled_by_id" not in columns(old_db)
    result = mig.migrate(old_db, out=quiet)
    assert result["status"] == "applied" and result["columns_added"] == ["cancelled_by_id", "cancelled_at"]
    assert result["appointments"] == 4 and result["cancelled"] == 2
    cols = columns(old_db)
    assert cols["cancelled_by_id"]["nullable"] and cols["cancelled_at"]["nullable"]
    if old_db.dialect.name == "postgresql":
        assert cols["cancelled_at"]["type"].timezone is True
    fks = [fk for fk in inspect(old_db).get_foreign_keys("appointments") if fk["constrained_columns"] == ["cancelled_by_id"]]
    assert len(fks) == 1 and fks[0]["referred_table"] == "users"
    assert everything(old_db) == before
    # Los cancelados de antes quedan sin quién: se muestran "Canceló por WhatsApp"
    assert scalar(old_db, "SELECT COUNT(*) FROM appointments WHERE cancelled_by_id IS NOT NULL "
                          "OR cancelled_at IS NOT NULL") == 0
    assert scalar(old_db, "SELECT COUNT(*) FROM schema_migrations WHERE id = 'b4_cancelled_by'") == 1
    assert schema_guard.missing_migrations(old_db) == []
    [event] = [e for e in events if e["event_type"] == "SCHEMA_MIGRATION_APPLIED"]
    assert event["metadata"]["migration_id"] == "b4_cancelled_by"
    assert SECRET_NAME not in str(event) and SECRET_PHONE not in str(event)


def test_migracion_idempotente(old_db, capsys):
    assert mig.migrate(old_db, out=quiet)["status"] == "applied"
    assert mig.migrate(old_db)["status"] == "already_applied"
    assert "ya estaba aplicada" in capsys.readouterr().out
    assert scalar(old_db, "SELECT COUNT(*) FROM schema_migrations WHERE id = 'b4_cancelled_by'") == 1


def test_dry_run_no_cambia_nada(old_db, capsys):
    before = everything(old_db)
    result = mig.migrate(old_db, dry_run=True)
    assert result["status"] == "dry_run" and result["columns_added"] == ["cancelled_by_id", "cancelled_at"]
    assert "SIMULACIÓN" in capsys.readouterr().out
    assert "cancelled_by_id" not in columns(old_db) and "cancelled_at" not in columns(old_db)   # DDL deshecho
    assert everything(old_db) == before
    assert scalar(old_db, "SELECT COUNT(*) FROM schema_migrations WHERE id = 'b4_cancelled_by'") == 0


def test_exige_e3e(old_db):
    with old_db.begin() as conn:
        conn.execute(text("DELETE FROM schema_migrations WHERE id = 'e3e_service_ids'"))
    with pytest.raises(mig.MigrationError, match="Falta la migración e3e_service_ids"):
        mig.migrate(old_db, out=quiet)
    assert "cancelled_by_id" not in columns(old_db)


def test_si_ya_hay_datos_en_las_columnas_aborta_sin_cambiar_nada(old_db):
    with old_db.begin() as conn:
        conn.execute(text("ALTER TABLE appointments ADD COLUMN cancelled_by_id INTEGER"))
        conn.execute(text("UPDATE appointments SET cancelled_by_id = 1 WHERE id = 2"))
    with pytest.raises(mig.MigrationError, match="ya tienen cancelled_by_id"):
        mig.migrate(old_db, out=quiet)
    assert "cancelled_at" not in columns(old_db)                       # se deshizo el ALTER de cancelled_at
    assert scalar(old_db, "SELECT COUNT(*) FROM schema_migrations WHERE id = 'b4_cancelled_by'") == 0


def test_base_nueva_hecha_con_el_modelo_no_agrega_columnas(tmp_path):
    """manage_staff init (create_all) ya crea las columnas: la migración solo se anota."""
    engine = mig.make_engine(f"sqlite:///{tmp_path / 'nueva.db'}")
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for migration_id in ("e3a_catalogo", "e3e_service_ids"):
            conn.execute(text("INSERT INTO schema_migrations (id, applied_at) VALUES (:i, CURRENT_TIMESTAMP)"),
                         {"i": migration_id})
    assert schema_guard.missing_migrations(engine) == ["b4_cancelled_by"]
    result = mig.migrate(engine, out=quiet)
    assert result["status"] == "applied" and result["columns_added"] == []
    schema_guard.ensure_schema_ready(engine)
    engine.dispose()


def test_la_app_no_arranca_sin_b4(old_db):
    with pytest.raises(RuntimeError, match="scripts.migrate_b4"):
        schema_guard.ensure_schema_ready(old_db)
    mig.migrate(old_db, out=quiet)
    schema_guard.ensure_schema_ready(old_db)


def test_cli_dry_run_y_aplicar(old_db, monkeypatch, capsys):
    import app.database
    monkeypatch.setattr(app.database, "SQLALCHEMY_DATABASE_URL", old_db.url.render_as_string(hide_password=False))
    assert mig.main(["--dry-run"]) == 0
    assert "SIMULACIÓN" in capsys.readouterr().out
    assert mig.main([]) == 0
    assert "MIGRACIÓN APLICADA" in capsys.readouterr().out
    assert mig.main([]) == 0
    assert "ya estaba aplicada" in capsys.readouterr().out


def test_el_orden_de_las_migraciones():
    assert schema_guard.REQUIRED_MIGRATIONS == ["e3a_catalogo", "e3e_service_ids", "b4_cancelled_by"]
    assert "scripts.migrate_b4 --dry-run" in schema_guard.HOW_TO_MIGRATE


# =====================================================================
# panel.js (controles estáticos)
# =====================================================================
def _body(start, end):
    return PANEL_JS[PANEL_JS.index(start):PANEL_JS.index(end)]


def test_f2_un_error_que_no_es_401_no_cierra_la_sesion():
    body = _body("async function fetchAppointments", "function showAgendaError")
    assert "if (response.status === 401) return sessionExpired();" in body
    assert "if (!response.ok) return showAgendaError();" in body
    assert "logout()" not in body
    assert "document.getElementById('agendaContent').textContent = 'Error al cargar.';" in PANEL_JS


def test_la_agenda_usa_la_hora_del_negocio():
    assert "response.headers.get('X-Business-Now')" in PANEL_JS
    for fn in ("function getWeekDaysArray", "function getMinWeekOffset", "function renderAgenda"):
        body = PANEL_JS[PANEL_JS.index(fn):]
        body = body[:body.index("\n}\n")]
        assert "new Date()" not in body and "businessNow()" in body, fn


def test_cancelado_por_va_escapado_y_sin_precio():
    body = _body("function renderAgenda", "function abrirWpp")
    assert "`Cancelado por ${escapeHtml(app.cancelled_by_name)}`" in body
    assert "'Canceló por WhatsApp'" in body
    assert "? `<span class=\"cancelled-tag\">${cancelledText}</span>`" in body       # sin priceBadgeHtml
    assert "${isCancelled ? '' : `<button class=\"btn-cancel\"" in body                 # sin botón Cancelar
    assert re.search(r"\$\{\s*app\.cancelled_by_name\s*\}", PANEL_JS) is None


def test_completado_y_orden():
    assert "const APPT_ORDER = { upcoming: 0, completed: 1, cancelled: 2 };" in PANEL_JS
    assert "const end = startMinutes(app) + (Number(app.duration) || 30);" in PANEL_JS
    assert "'<span class=\"completed-tag\">Completado</span>'" in PANEL_JS
    assert ".appointment-item.appt-completed .appt-details {\n    opacity: 0.6;" in PANEL_CSS


def test_cancelar_avisa_que_el_cliente_no_recibe_aviso_y_maneja_el_409():
    body = _body("async function cancelarTurno", "function hideAllSections")
    assert "el cliente no recibe aviso" in body
    assert "response.status === 409" in body


def test_versiones_de_cache_subidas():
    assert 'href="css/panel.css?v=14"' in PANEL_HTML and 'src="js/panel.js?v=15"' in PANEL_HTML
    assert 'src="js/my-services.js?v=3"' in PANEL_HTML and 'src="js/owner-catalog.js?v=2"' in PANEL_HTML
