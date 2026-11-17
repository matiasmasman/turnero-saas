"""Pruebas de navegador del 3e: la web pública de reservas con el catálogo, con Playwright + Firefox headless.

- La app corre como proceso aparte (tests/e2e_server.py, con sus dos frenos): uvicorn en 127.0.0.1 en un puerto
  libre, base SQLite TEMPORAL ya inicializada y migrada (e3a + e3e), WhatsApp apagado, log de seguridad de prueba.
  La carpeta temporal se borra al terminar.
- Staff de `manage_staff init`: 1 Maxi (dueño), 2 Juampa, 3 Leo, 4 Pedro; trabajan Lun-Sáb 10-13 y 17-20:30.
- Si Playwright o su Firefox no están instalados, estas pruebas se saltean.
- Capturas: solo si E3E_CAPTURAS apunta a una carpeta (por ejemplo ~/3e-capturas).
- Cada prueba crea sus servicios con nombres propios ("... E3E") y al final se borran los turnos.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="sin Playwright: se saltean las pruebas de navegador")
expect = sync_api.expect

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import Appointment, BarberService, Discount, DiscountPrice, Service, User  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "clave-e2e-123"
XSS = "<img src=x onerror=alert(1)>"
BA = ZoneInfo("America/Argentina/Buenos_Aires")
_REAL_SOCKET = socket.socket          # al importar el módulo todavía no corre `sin_sockets`
SHOTS = Path(os.environ["E3E_CAPTURAS"]).expanduser() if os.getenv("E3E_CAPTURAS") else None
JUAMPA, LEO, PEDRO = 2, 3, 4


def booking_day() -> str:
    """Un día que trabajan (Lun-Sáb), a 2 o más días de hoy en hora del negocio: nunca "ya pasó"."""
    day = datetime.now(BA).date() + timedelta(days=2)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


DAY = booking_day()


# ---------- Sockets: la excepción más chica posible (igual que en test_navegador_3d.py) ----------
@pytest.fixture(autouse=True)
def sin_sockets(monkeypatch):
    def _solo_socketpair(family=-1, type=-1, proto=-1, fileno=None):
        if fileno is None:
            raise AssertionError("Las pruebas no pueden abrir sockets")
        return _REAL_SOCKET(family, type, proto, fileno)
    monkeypatch.setattr(socket, "socket", _solo_socketpair)


# ---------- Servidor, base y navegador ----------
@pytest.fixture(scope="module")
def server():
    tmp = Path(tempfile.mkdtemp(prefix="turnero-e2e-"))
    log = open(tmp / "server.out", "w")
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "e2e_server.py"), str(tmp), PASSWORD],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while not (tmp / "port").exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                log.flush()
                pytest.fail("El servidor de prueba no arrancó:\n" + (tmp / "server.out").read_text()[-3000:])
            time.sleep(0.1)
        port = int((tmp / "port").read_text())
        engine = create_engine(f"sqlite:///{tmp / 'e2e.db'}")
        yield type("Server", (), {"url": f"http://127.0.0.1:{port}", "tmp": tmp,
                                  "Session": sessionmaker(bind=engine)})
        engine.dispose()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        if not Path(p.firefox.executable_path).exists():
            pytest.skip("sin el Firefox de Playwright (python -m playwright install firefox)")
        b = p.firefox.launch(headless=True)
        yield b
        b.close()


class Tab:
    """La página de reservas en un contexto nuevo, que junta errores de consola y diálogos (alert/confirm)."""

    def __init__(self, browser, server, width=1280, height=900, clock=None):
        self.server = server
        self.context = browser.new_context(viewport={"width": width, "height": height}, locale="es-AR",
                                           timezone_id="America/Argentina/Buenos_Aires")
        self.page = self.context.new_page()
        self.errors, self.dialogs = [], []
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}")
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on("dialog", self._dialog)
        if clock is not None:
            self.page.clock.install(time=clock)

    def _dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()

    def open(self):
        self.page.goto(f"{self.server.url}/")
        expect(self.page.locator("#service")).to_be_enabled()
        return self

    def choose(self, service_id, barber_id=None, day=DAY):
        page = self.page
        page.select_option("#service", str(service_id))
        page.select_option("#employee_id", "" if barber_id is None else str(barber_id))
        page.fill("#appointmentDate", day)
        return self

    def times(self):
        expect(self.page.locator("#timeSelect")).to_be_enabled()
        return self.page.locator("#timeSelect option").all_text_contents()[1:]   # sin "Elegí un horario"

    def fill_client(self, name="Cliente Prueba", phone="2610000000"):
        self.page.fill("#client_name", name)
        self.page.fill("#client_phone", phone)
        return self

    def pick_time(self, hhmm):
        expect(self.page.locator(f"#timeSelect option[value='{hhmm}']")).to_have_count(1)
        self.page.select_option("#timeSelect", hhmm)
        return self

    def submit(self):
        self.page.click("#appointmentForm button[type=submit]")
        return self

    def apply_discount(self, discount_id):
        self.page.select_option("#discount", str(discount_id))
        expect(self.page.locator("#discount option").first).to_have_text("Quitar descuento")
        return self

    def remove_discount(self):
        self.page.select_option("#discount", "")
        expect(self.page.locator("#discount option").first).to_have_text("Descuento")
        return self

    def options(self, selector):
        return self.page.locator(f"{selector} option").all_text_contents()

    def no_horizontal_scroll(self):
        width = self.page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        assert width[0] <= width[1], f"scroll horizontal: scrollWidth={width[0]} > clientWidth={width[1]}"

    def shot(self, name):
        if SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)

    def close(self):
        self.context.close()


@pytest.fixture
def tabs(browser, server):
    opened = []

    def open_tab(**kwargs):
        tab = Tab(browser, server, **kwargs)
        opened.append(tab)
        return tab

    yield open_tab
    problems = [(t.errors, t.dialogs) for t in opened if t.errors or t.dialogs]
    for t in opened:
        t.close()
    db = server.Session()                       # cada prueba empieza sin turnos
    db.query(Appointment).delete()
    db.commit()
    db.close()
    assert not problems, f"errores de consola o diálogos inesperados: {problems}"


# ---------- Datos directos en la base temporal ----------
def add_service(server, name, price, barbers, active=True):
    """barbers = {barbero: duración}. Devuelve el id."""
    db = server.Session()
    s = Service(business_id=1, name=name, price=price, base_duration=30, active=active)
    db.add(s)
    db.flush()
    for emp, duration in barbers.items():
        db.add(BarberService(business_id=1, employee_id=emp, service_id=s.id, duration=duration, active=True))
    db.commit()
    sid = s.id
    db.close()
    return sid


def add_discount(server, name, prices):
    db = server.Session()
    d = Discount(business_id=1, name=name, active=True)
    db.add(d)
    db.flush()
    for service_id, price in prices.items():
        db.add(DiscountPrice(business_id=1, discount_id=d.id, service_id=service_id, price=price))
    db.commit()
    did = d.id
    db.close()
    return did


def set_active(server, service_id, active):
    db = server.Session()
    db.get(Service, service_id).active = active
    db.commit()
    db.close()


def set_barber_name(server, employee_id, name):
    db = server.Session()
    db.get(User, employee_id).name = name
    db.commit()
    db.close()


def booked(server):
    db = server.Session()
    try:
        return [(a.service_id, a.service, a.discount_id, a.discount_name, a.price, a.duration, a.employee_id, a.time)
                for a in db.query(Appointment).order_by(Appointment.id)]
    finally:
        db.close()


def take_slot(server, employee_id, service_id, hhmm):
    db = server.Session()
    db.add(Appointment(client_name="Otro Cliente", client_phone="2610000001", service="Otro", service_id=service_id,
                       date=DAY, time=f"{hhmm}:00", duration=30, price=1000, status="confirmed",
                       employee_id=employee_id))
    db.commit()
    db.close()


# ---------- Pruebas ----------
def test_reservar_con_y_sin_descuento_viendo_el_precio_final(tabs, server):
    sid = add_service(server, "Corte Navaja E3E", 10000, {JUAMPA: 30})
    did = add_discount(server, "Promo E3E", {sid: 8000})
    tab = tabs().open()
    page = tab.page
    discount, price = page.locator("#discount"), page.locator("#servicePrice")
    tab.choose(sid, JUAMPA)
    expect(page.locator(f"#service option[value='{sid}']")).to_have_text("Corte Navaja E3E")   # solo el nombre
    expect(price).to_have_text("$10.000")
    expect(price.locator("s")).to_have_count(0)
    expect(discount).to_be_enabled()
    expect(discount).to_have_value("")
    assert tab.options("#discount") == ["Descuento", "Promo E3E — $8.000"]

    tab.apply_discount(did)
    assert tab.options("#discount") == ["Quitar descuento", "Promo E3E — $8.000"]
    expect(discount).to_have_value(str(did))
    expect(price.locator("s")).to_have_text("$10.000")
    expect(price.locator("strong")).to_have_text("$8.000")
    tab.shot("01-reserva-con-descuento-escritorio")

    tab.fill_client().pick_time("10:00").submit()
    panel = page.locator("#confirmPanel")
    expect(panel).to_be_visible()
    expect(panel.locator(".confirm-what")).to_have_text("Juampa · Corte Navaja E3E · Promo E3E")
    expect(panel).to_contain_text("10:00 hs · $8.000")
    expect(page.locator("#appointmentForm")).to_be_hidden()
    tab.shot("02-turno-confirmado-escritorio")
    assert booked(server) == [(sid, "Corte Navaja E3E", did, "Promo E3E", 8000, 30, JUAMPA, "10:00:00")]

    page.click("#confirmPanel .wa-back")                # "Reservar otro turno": vuelve sin descuento
    tab.choose(sid, JUAMPA)
    expect(discount).to_have_value("")
    expect(page.locator("#discount option").first).to_have_text("Descuento")
    expect(price).to_have_text("$10.000")

    tab.apply_discount(did).remove_discount()           # "Quitar descuento": se reserva sin descuento
    expect(price).to_have_text("$10.000")
    tab.fill_client().pick_time("10:30").submit()
    expect(page.locator("#confirmPanel")).to_contain_text("10:30 hs · $10.000")
    assert booked(server)[-1] == (sid, "Corte Navaja E3E", None, None, 10000, 30, JUAMPA, "10:30:00")


def test_un_barbero_que_no_hace_el_servicio_no_aparece_y_sin_descuentos_queda_deshabilitado(tabs, server):
    sid = add_service(server, "Barba Fina E3E", 5000, {LEO: 20})
    tab = tabs().open()
    tab.page.select_option("#service", str(sid))
    assert tab.options("#employee_id") == ["Cualquiera disponible", "Leo"]
    expect(tab.page.locator("#discount")).to_be_visible()             # no se oculta: la pantalla no salta
    expect(tab.page.locator("#discount")).to_be_disabled()
    assert tab.options("#discount") == ["Sin descuentos"]
    expect(tab.page.locator("#servicePrice")).to_have_text("$5.000")


def test_cambiar_de_servicio_quita_el_descuento(tabs, server):
    a = add_service(server, "Servicio A E3E", 9000, {JUAMPA: 30})
    b = add_service(server, "Servicio B E3E", 7000, {JUAMPA: 30})
    promo_a = add_discount(server, "Promo A E3E", {a: 6000})
    add_discount(server, "Promo B E3E", {b: 5000})
    tab = tabs().open()
    page = tab.page
    tab.choose(a, JUAMPA).apply_discount(promo_a)
    expect(page.locator("#servicePrice strong")).to_have_text("$6.000")

    page.select_option("#service", str(b))               # otro servicio: sin descuento y con sus opciones
    expect(page.locator("#discount")).to_have_value("")
    assert tab.options("#discount") == ["Descuento", "Promo B E3E — $5.000"]
    expect(page.locator("#servicePrice")).to_have_text("$7.000")

    page.select_option("#service", str(a))               # volver al primero tampoco lo recupera
    expect(page.locator("#discount")).to_have_value("")
    expect(page.locator("#discount option").first).to_have_text("Descuento")
    expect(page.locator("#servicePrice")).to_have_text("$9.000")
    tab.fill_client().pick_time("11:00").submit()
    expect(page.locator("#confirmPanel")).to_contain_text("11:00 hs · $9.000")
    assert booked(server)[-1][2:5] == (None, None, 9000)


def test_horarios_distintos_segun_la_duracion_de_cada_barbero(tabs, server):
    sid = add_service(server, "Afeitado E3E", 4000, {JUAMPA: 30, LEO: 60})
    tab = tabs().open()
    tab.choose(sid, JUAMPA)
    assert tab.times()[:3] == ["10:00", "10:30", "11:00"]
    tab.choose(sid, LEO)
    expect(tab.page.locator("#timeSelect option").nth(2)).to_have_text("11:00")
    assert tab.times()[:3] == ["10:00", "11:00", "12:00"]


def test_un_servicio_sin_barberos_o_desactivado_no_aparece(tabs, server):
    nobody = add_service(server, "Sin Nadie E3E", 1000, {})
    off = add_service(server, "Apagado E3E", 1000, {JUAMPA: 30}, active=False)
    on = add_service(server, "Prendido E3E", 1000, {JUAMPA: 30})
    tab = tabs().open()
    values = tab.page.locator("#service option").evaluate_all("os => os.map(o => o.value)")
    assert str(on) in values and str(nobody) not in values and str(off) not in values


def test_nombres_con_signos_se_ven_como_texto(tabs, server):
    sid = add_service(server, XSS, 1000, {PEDRO: 30})                  # solo tocando la base (la API no deja < >)
    did = add_discount(server, "<b>D'Angelo</b> E3E", {sid: 900})
    set_barber_name(server, PEDRO, "<b>Pedro</b> O'Neil")
    try:
        tab = tabs().open()
        page = tab.page
        expect(page.locator(f"#service option[value='{sid}']")).to_have_text(XSS)
        tab.choose(sid, PEDRO)
        assert tab.options("#employee_id") == ["Cualquiera disponible", "<b>Pedro</b> O'Neil"]
        expect(page.locator(f"#discount option[value='{did}']")).to_have_text("<b>D'Angelo</b> E3E — $900")
        tab.apply_discount(did)
        expect(page.locator("#servicePrice")).to_have_text("$1.000$900")
        # nada se interpretó como HTML: en el precio solo <s> y <strong>, y ningún <img>/<b> en el formulario
        assert page.locator("#servicePrice *").evaluate_all("ns => ns.map(n => n.tagName)") == ["S", "STRONG"]
        assert page.locator("#appointmentForm img, #appointmentForm b").count() == 0
        tab.fill_client().pick_time("10:00").submit()
        expect(page.locator("#confirmPanel .confirm-what")).to_have_text(
            f"<b>Pedro</b> O'Neil · {XSS} · <b>D'Angelo</b> E3E")
        assert page.locator("#confirmPanel img, #confirmPanel b").count() == 0
    finally:
        set_barber_name(server, PEDRO, "Pedro")


def test_los_errores_se_ven_dentro_del_formulario(tabs, server):
    sid = add_service(server, "Error E3E", 3000, {JUAMPA: 30})
    tab = tabs().open()
    page = tab.page
    error = page.locator("#appointmentForm #formError")
    tab.choose(sid, JUAMPA).fill_client().pick_time("10:00")

    take_slot(server, JUAMPA, sid, "10:00")             # alguien lo reservó mientras tanto
    tab.submit()
    expect(error).to_have_text("No se pudo reservar: Ese horario se acaba de ocupar. Elegí otro.")
    expect(page.locator("#appointmentForm")).to_be_visible()
    expect(page.locator("#timeSelect option[value='10:00']")).to_have_count(0)   # se actualizaron los horarios
    tab.shot("03-error-ocupado-escritorio")

    tab.fill_client(name="J").pick_time("10:30").submit()                       # pasa el "required" del navegador
    expect(error).to_have_text("No se pudo reservar: El nombre tiene que tener entre 2 y 40 caracteres.")

    set_active(server, sid, False)                      # el dueño lo desactivó mientras tanto
    tab.fill_client().submit()
    expect(error).to_have_text("No se pudo reservar: El servicio elegido no está disponible.")
    assert len(booked(server)) == 1                     # solo el de "alguien"


def test_fecha_por_defecto_a_las_22_hora_local(tabs, server):
    """A las 22:00 de Argentina ya es el día siguiente en UTC: toISOString mostraba "mañana"."""
    tab = tabs(clock="2030-06-07T22:00:00-03:00").open()
    expect(tab.page.locator("#appointmentDate")).to_have_value("2030-06-07")
    assert tab.page.locator("#appointmentDate").get_attribute("min") == "2030-06-07"
    assert tab.page.locator("#appointmentDate").get_attribute("max") == "2030-08-06"   # + 60 días


def test_celular_360_sin_scroll_horizontal(tabs, server):
    sid = add_service(server, "Corte degradé con diseño y navaja XL E3E", 999999, {LEO: 30})
    promo = add_discount(server, "Promo de invierno para socios club E3E", {sid: 888888})
    add_discount(server, "Jubilados y estudiantes con libreta E3E", {sid: 666666})
    set_barber_name(server, LEO, "Leonardo Bartolomeo de las Mercedes")
    try:
        tab = tabs(width=360, height=800).open()
        page = tab.page
        tab.no_horizontal_scroll()
        tab.shot("11-reserva-celular")
        tab.choose(sid, LEO)
        # los dos desplegables lado a lado, mitad y mitad, dentro del formulario
        service = page.locator("#service").bounding_box()
        discount = page.locator("#discount").bounding_box()
        form = page.locator("#appointmentForm").bounding_box()
        assert abs(service["y"] - discount["y"]) < 1 and service["x"] < discount["x"]
        assert abs(service["width"] - discount["width"]) < 2
        assert discount["x"] + discount["width"] <= form["x"] + form["width"] + 0.5
        tab.apply_discount(promo)
        tab.no_horizontal_scroll()
        tab.shot("12-reserva-descuento-elegido-celular")
        tab.fill_client().pick_time("10:00").submit()
        expect(page.locator("#appointmentForm #formError")).to_be_hidden()
        expect(page.locator("#confirmPanel")).to_be_visible()
        tab.no_horizontal_scroll()
        tab.shot("13-turno-confirmado-celular")
    finally:
        set_barber_name(server, LEO, "Leo")
