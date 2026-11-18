"""Pruebas del sub-bloque 3f: agenda en solo lectura (precio final + descuento), endpoints viejos fuera y
el "ahora" del negocio (catalog.now_local) en métricas y excepciones. Datos 100 % de prueba.

Usa el fixture `api` de conftest.py (TestClient sobre SQLite en memoria y, con E3_TEST_PG_URL, también PostgreSQL).
La prueba de navegador de la agenda está en test_navegador_3f.py.
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import app.catalog as catalog
from app.models import Appointment, ScheduleException, User, UserRole

ROOT = Path(__file__).resolve().parent.parent
PANEL_JS = (ROOT / "frontend/js/panel.js").read_text(encoding="utf-8")
PANEL_CSS = (ROOT / "frontend/css/panel.css").read_text(encoding="utf-8")
PANEL_HTML = (ROOT / "frontend/panel.html").read_text(encoding="utf-8")
XSS = "<img src=x onerror=alert(1)>"
MENDOZA = ZoneInfo("America/Argentina/Mendoza")
# 31/03/2031 23:50 en Mendoza = 01/04/2031 02:50 UTC: en un servidor con reloj UTC ya sería otro día y otro mes.
# Lejos de la fecha real, así el código viejo (date.today() del servidor) daría otra cosa.
UTC_INSTANT = datetime(2031, 4, 1, 2, 50, tzinfo=timezone.utc)


@pytest.fixture
def staff(api):
    from app.core.security import create_access_token   # acá: necesita la SECRET_KEY que pone `api`
    db = api.Session()
    owner = User(name="Dueño", username="dueno", hashed_password="x", role=UserRole.OWNER)
    employee = User(name="Empleado", username="empleado", hashed_password="x", role=UserRole.EMPLOYEE)
    db.add_all([owner, employee])
    db.commit()
    ns = {
        "owner": {"Authorization": f"Bearer {create_access_token({'sub': str(owner.id), 'role': 'owner'})}"},
        "employee": {"Authorization": f"Bearer {create_access_token({'sub': str(employee.id), 'role': 'employee'})}"},
        "owner_id": owner.id, "employee_id": employee.id,
    }
    db.close()
    return ns


def add_appt(api, employee_id, date, time, price, discount_name=None, service="Corte", status="confirmed"):
    db = api.Session()
    appt = Appointment(client_name="Cliente Prueba", client_phone="2610000000", service=service, date=date,
                       time=time, duration=30, price=price, status=status, employee_id=employee_id,
                       discount_name=discount_name)
    db.add(appt)
    db.commit()
    appt_id = appt.id
    db.close()
    return appt_id


def users_snapshot(api):
    db = api.Session()
    try:
        return [(u.id, u.price_corte, u.price_corte_promo2x, u.price_corte_vip, u.price_barba, u.price_corte_barba,
                 u.duration_corte, u.duration_barba, u.duration_corte_barba) for u in db.query(User).order_by(User.id)]
    finally:
        db.close()


@pytest.fixture
def mendoza_midnight(monkeypatch):
    """El reloj de catalog.now_local() (y no now_local mismo): la zona es Mendoza y el instante es UTC_INSTANT."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return UTC_INSTANT.astimezone(tz) if tz else UTC_INSTANT.replace(tzinfo=None)
    monkeypatch.setattr(catalog, "LOCAL_TZ", MENDOZA)
    monkeypatch.setattr(catalog, "datetime", Frozen)
    assert catalog.now_local() == datetime(2031, 3, 31, 23, 50)


# ---------- Endpoints que se fueron ----------
GONE = [
    ("GET", "/admin/service-durations", None),
    ("POST", "/admin/service-durations", {"duration_corte": 40, "duration_barba": 20}),
    ("PUT", "/admin/service-durations", {"duration_corte": 40, "duration_barba": 20}),
    ("GET", "/admin/service-prices", None),
    ("POST", "/admin/service-prices", {"price_corte": 1, "price_barba": 1, "price_corte_barba": 1}),
    ("PUT", "/admin/service-prices", {"price_corte": 1, "price_barba": 1, "price_corte_barba": 1}),
    ("PATCH", "/admin/appointments/{id}/price", {"tier": "vip"}),
    ("PUT", "/admin/appointments/{id}/price", {"tier": "vip"}),
    ("POST", "/admin/appointments/{id}/price", {"tier": "vip"}),
]


@pytest.mark.parametrize("method, path, body", GONE, ids=[f"{m} {p}" for m, p, _ in GONE])
@pytest.mark.parametrize("who", ["owner", "employee"])
def test_los_endpoints_viejos_ya_no_existen(api, staff, events, method, path, body, who):
    appt_id = add_appt(api, staff["employee_id"], "2031-03-31", "10:00", 15000)
    before = users_snapshot(api)
    resp = api.client.request(method, path.format(id=appt_id), json=body, headers=staff[who])
    assert resp.status_code in (404, 405), resp.text
    assert users_snapshot(api) == before
    db = api.Session()
    assert db.get(Appointment, appt_id).price == 15000
    db.close()
    assert not [e for e in events if e["event_type"] in (
        "APPOINTMENT_PRICE_UPDATED", "IDOR_ATTEMPT_APPOINTMENT_PRICE",
        "SERVICE_PRICES_UPDATED", "SERVICE_DURATIONS_UPDATED")]


def test_tampoco_estan_en_openapi(api):
    # Bloque 3 (#28): /openapi.json está apagado por defecto; el esquema se arma igual con app.openapi()
    paths = api.client.app.openapi()["paths"]
    assert "/admin/service-durations" not in paths
    assert "/admin/service-prices" not in paths
    assert not [p for p in paths if p.endswith("/price")]
    assert "delete" in paths["/admin/appointments/{appointment_id}"]       # Cancelar sigue (lo cambia el Bloque 4)


# ---------- La agenda (GET /admin/appointments) trae precio final y descuento ----------
def test_la_agenda_trae_precio_final_y_nombre_del_descuento(api, staff):
    con = add_appt(api, staff["employee_id"], "2031-03-31", "10:00", 9000, discount_name="VIP")
    viejo = add_appt(api, staff["employee_id"], "2031-03-31", "11:00", 15000)                  # antes del 3e
    vacio = add_appt(api, staff["employee_id"], "2031-03-31", "12:00", 15000, discount_name="")
    raro = add_appt(api, staff["employee_id"], "2031-03-31", "13:00", 7000, discount_name=XSS)  # cargado a mano
    resp = api.client.get("/admin/appointments", headers=staff["employee"])
    assert resp.status_code == 200
    got = {a["id"]: (a["price"], a["discount_name"]) for a in resp.json()}
    assert got == {con: (9000, "VIP"), viejo: (15000, None), vacio: (15000, ""), raro: (7000, XSS)}


# ---------- Métricas ----------
def test_metricas_suman_el_precio_final_del_turno_con_descuento(api, staff, monkeypatch):
    monkeypatch.setattr(catalog, "now_local", lambda: datetime(2031, 3, 12, 16, 0))
    emp = staff["employee_id"]
    add_appt(api, emp, "2031-03-12", "10:00", 9000, discount_name="VIP")         # ya pasó: real, precio con descuento
    add_appt(api, emp, "2031-03-12", "18:00", 15000)                             # más tarde: pendiente, sin descuento
    add_appt(api, emp, "2031-03-12", "19:00", 12000, discount_name="Promo 2x")   # más tarde: pendiente, con descuento
    add_appt(api, emp, "2031-03-12", "11:00", 9000, discount_name="VIP", status="cancelled")   # no cuenta
    data = api.client.get("/admin/metrics?period=today", headers=staff["employee"]).json()
    assert (data["start_date"], data["end_date"]) == ("2031-03-12", "2031-03-12")
    assert (data["total_appointments"], data["completed_appointments"], data["pending_appointments"]) == (3, 1, 2)
    assert (data["real_revenue"], data["pending_revenue"], data["total_revenue"]) == (9000, 27000, 36000)
    blocks = {b["label"]: (b["real_revenue"], b["pending_revenue"]) for b in data["chart_series"]}
    assert blocks["Mañana"] == (9000, 0) and blocks["Tarde"] == (0, 27000)


def test_metricas_usan_la_fecha_de_mendoza_cerca_de_la_medianoche(api, staff, mendoza_midnight):
    emp = staff["employee_id"]
    add_appt(api, emp, "2031-03-31", "23:30", 9000, discount_name="VIP")   # hoy en Mendoza, ya pasó
    add_appt(api, emp, "2031-03-31", "23:55", 15000)                       # hoy en Mendoza, todavía no
    add_appt(api, emp, "2031-04-01", "00:10", 18000)                       # mañana (y otro mes): no es de hoy

    today = api.client.get("/admin/metrics?period=today", headers=staff["employee"]).json()
    assert (today["start_date"], today["end_date"]) == ("2031-03-31", "2031-03-31")
    assert (today["real_revenue"], today["pending_revenue"]) == (9000, 15000)

    month = api.client.get("/admin/metrics?period=month", headers=staff["employee"]).json()
    assert (month["start_date"], month["end_date"]) == ("2031-03-01", "2031-03-31")    # marzo, no abril
    assert month["total_revenue"] == 24000

    week = api.client.get("/admin/metrics?period=week", headers=staff["employee"]).json()
    assert (week["start_date"], week["end_date"]) == ("2031-03-31", "2031-04-06")      # 31/03/2031 es lunes
    assert (week["real_revenue"], week["pending_revenue"]) == (9000, 33000)


# ---------- Excepciones ----------
def test_excepciones_usan_la_fecha_de_mendoza_cerca_de_la_medianoche(api, staff, mendoza_midnight):
    emp = staff["employee_id"]
    # Crear: ayer (en Mendoza) ya pasó; hoy (aunque en UTC ya sea 01/04) se puede
    resp = api.client.post("/admin/exceptions", headers=staff["employee"], json={"date": "2031-03-30"})
    assert resp.status_code == 422 and "fechas pasadas" in resp.text
    resp = api.client.post("/admin/exceptions", headers=staff["employee"],
                           json={"date": "2031-03-31", "start_time": "23:00", "end_time": "23:59"})
    assert resp.status_code == 201, resp.text

    # Listar: desde hoy en Mendoza (ni ayer ni nada antes)
    db = api.Session()
    db.add(ScheduleException(date="2031-03-30", reason="Bloqueo", employee_id=emp))
    db.add(ScheduleException(date="2031-04-01", reason="Bloqueo", employee_id=emp))
    db.commit()
    db.close()
    listed = api.client.get("/admin/exceptions", headers=staff["employee"]).json()
    assert [e["date"] for e in listed] == ["2031-03-31", "2031-04-01"]


def test_no_quedan_ahoras_sin_zona_en_admin_ni_en_las_excepciones():
    admin = (ROOT / "app/routers/admin.py").read_text(encoding="utf-8")
    schemas = (ROOT / "app/schemas.py").read_text(encoding="utf-8")
    for src in (admin, schemas):
        assert "date.today()" not in src and "datetime.now()" not in src
    # 3 desde el Bloque 4: el header X-Business-Now de GET /admin/appointments (ver test_bloque4.py)
    assert admin.count("catalog.now_local()") == 3 and schemas.count("catalog.now_local()") == 1


# ---------- Frontend (controles estáticos; el navegador de verdad, en test_navegador_3f.py) ----------
def test_la_agenda_no_tiene_boton_ni_menu_de_tarifa():
    for old in ("shopPrices", "loadServicePrices", "toggleTariffMenu", "cambiarTarifaTurno", "tariff",
                "/admin/service-prices", "/admin/service-durations", "/price`", "Promo 2x", "VIP",
                "price-caret", "Cambiar tarifa", "price-tag clickable"):
        assert old not in PANEL_JS, old
    for old in (".tariff-", ".price-caret", ".price-tag.clickable", ".price-tag-promo", ".price-tag-vip", "Promo 2x"):
        assert old not in PANEL_CSS, old


def test_el_precio_y_el_descuento_van_escapados():
    body = PANEL_JS[PANEL_JS.index("function renderAgenda"):PANEL_JS.index("function abrirWpp")]
    assert "const discountName = String(app.discount_name || '').trim();" in body
    assert "const priceText = formatARS(app.price) + (discountName ? ` · ${discountName}` : '');" in body
    assert 'const priceBadgeHtml = `<span class="price-tag">${escapeHtml(priceText)}</span>`;' in body
    assert re.search(r"\$\{\s*discountName\s*\}", body.replace("` · ${discountName}`", "")) is None   # nunca crudo


def test_whatsapp_y_cancelar_siguen_como_estaban():
    body = PANEL_JS[PANEL_JS.index("function renderAgenda"):PANEL_JS.index("function abrirWpp")]
    assert """<button class="btn-wpp" onclick="abrirWpp('${safePhone}')">WhatsApp</button>""" in body
    assert """`<button class="btn-cancel" onclick="cancelarTurno(${safeId})">Cancelar</button>`""" in body


def test_versiones_de_cache_subidas():
    assert 'href="css/panel.css?v=14"' in PANEL_HTML      # v=14 en el Bloque 4 (ver test_bloque4.py)
    assert 'src="js/panel.js?v=15"' in PANEL_HTML        # v=14 en el Bloque 3, v=15 en el Bloque 4
