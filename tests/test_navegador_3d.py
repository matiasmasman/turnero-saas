"""Pruebas de navegador del 3d: "Servicios del local" y "Descuentos" (solo dueño), con Playwright + Firefox headless.

- La app corre como proceso aparte (tests/e2e_server.py): uvicorn en 127.0.0.1 en un puerto libre, base SQLite
  TEMPORAL ya inicializada y migrada, WhatsApp apagado, SECRET_KEY y log de seguridad de prueba.
  La carpeta temporal se borra al terminar.
- Si Playwright o su Firefox no están instalados, estas pruebas se saltean (como las de PostgreSQL).
- Capturas: solo si E3D_CAPTURAS apunta a una carpeta (por ejemplo ~/3d-capturas).
- Cada prueba usa nombres propios ("... E2E"), así no dependen del orden.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="sin Playwright: se saltean las pruebas de navegador")
expect = sync_api.expect

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import Appointment, Discount, DiscountPrice, Service  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "clave-e2e-123"
XSS = "<img src=x onerror=alert(1)>"
_REAL_SOCKET = socket.socket          # al importar el módulo todavía no corre `sin_sockets`
SHOTS = Path(os.environ["E3D_CAPTURAS"]).expanduser() if os.getenv("E3D_CAPTURAS") else None


# ---------- Sockets: la excepción más chica posible ----------
@pytest.fixture(autouse=True)
def sin_sockets(monkeypatch):
    """Reemplaza al de conftest SOLO en este archivo. Igual que en el fixture `api`: se permite envolver un
    descriptor que ya existe (el socketpair que usa asyncio dentro de Playwright) y crear un socket nuevo
    sigue prohibido. Las conexiones de verdad las hacen otros procesos (uvicorn y Firefox)."""
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
    """Una pestaña con un contexto nuevo (localStorage vacío) que junta errores de consola y diálogos."""

    def __init__(self, browser, server, width=1280, height=900):
        self.server = server
        self.context = browser.new_context(viewport={"width": width, "height": height}, locale="es-AR")
        self.page = self.context.new_page()
        self.errors, self.dialogs, self.requests = [], [], []
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}")
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on("dialog", self._dialog)
        self.page.on("request", lambda r: self.requests.append(r.url))

    def _dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()

    def login(self, username):
        self.page.goto(f"{self.server.url}/panel.html")
        self.page.fill("#username", username)
        self.page.fill("#password", PASSWORD)
        self.page.click("#loginForm button[type=submit]")
        expect(self.page.locator("#dashboardSection")).to_be_visible()
        return self

    def open_services(self, owner=True):
        self.page.click("#dashboardSection button:has-text('Servicios')")
        expect(self.page.locator("#myServicesList .my-svc-row").first).to_be_visible()
        if owner:
            expect(self.page.locator("#ownerServicesList .owner-row").first).to_be_visible()
            expect(self.page.locator("#ownerDiscountsList")).not_to_contain_text("Cargando")
        return self

    def service_row(self, name):
        return self.page.locator("#ownerServicesList .owner-row").filter(
            has=self.page.locator(".owner-name", has_text=name))

    def discount_row(self, name):
        return self.page.locator("#ownerDiscountsList .owner-row").filter(
            has=self.page.locator(".owner-name", has_text=name))

    def my_service_row(self, name):
        return self.page.locator("#myServicesList .my-svc-row").filter(
            has=self.page.locator(".my-svc-name", has_text=name))

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
    assert not problems, f"errores de consola o diálogos inesperados: {problems}"


# ---------- Datos directos en la base temporal ----------
def add_service(server, name, price=8000, duration=30, active=True):
    db = server.Session()
    s = Service(business_id=1, name=name, price=price, base_duration=duration, active=active)
    db.add(s)
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


def add_future_appointment(server, service_id, service_name):
    db = server.Session()
    db.add(Appointment(client_name="Cliente E2E", client_phone="2610000000", service=service_name,
                       date=(date.today() + timedelta(days=10)).isoformat(), time="15:00:00", duration=30,
                       price=8000, status="confirmed", employee_id=2, service_id=service_id))
    db.commit()
    db.close()


def service_by_name(server, name):
    db = server.Session()
    try:
        s = db.query(Service).filter(Service.name == name).one_or_none()
        return None if s is None else (s.price, s.base_duration, s.active)
    finally:
        db.close()


def discount_prices(server, discount_id):
    db = server.Session()
    try:
        return {dp.service_id: dp.price
                for dp in db.query(DiscountPrice).filter(DiscountPrice.discount_id == discount_id)}
    finally:
        db.close()


def service_id(server, name):
    db = server.Session()
    try:
        return db.query(Service.id).filter(Service.name == name).scalar()
    finally:
        db.close()


# ---------- Pruebas ----------
def test_el_dueno_ve_servicios_y_descuentos_y_el_empleado_no(tabs, server):
    owner = tabs().login("maxi").open_services()
    page = owner.page
    expect(page.locator("#ownerServicesCard")).to_be_visible()
    expect(page.locator("#ownerDiscountsCard")).to_be_visible()
    expect(page.locator("#myServicesCard")).to_be_visible()
    expect(page.locator("#empServicesNavPills")).to_be_hidden()            # los botones van en la tarjeta del dueño
    expect(owner.service_row("Corte + Barba").locator(".owner-meta")).to_have_text("$18.000 · 45 min (duración base)")
    expect(owner.service_row("Corte + Barba").locator(".owner-warn")).to_have_count(0)   # la migración: todos lo hacen
    assert page.evaluate("escapeHtml(`<a href='x'>&\"`)") == "&lt;a href=&#39;x&#39;&gt;&amp;&quot;"
    owner.shot("01-duenio-servicios-escritorio")

    emp = tabs().login("juampa").open_services(owner=False)
    epage = emp.page
    expect(epage.locator("#ownerServicesCard")).to_be_hidden()
    expect(epage.locator("#ownerDiscountsCard")).to_be_hidden()
    expect(epage.locator("#empServicesNavPills")).to_be_visible()
    expect(emp.my_service_row("Corte").first).to_be_visible()                # "Mis servicios" (3c) sigue igual
    assert not [u for u in emp.requests if "/admin/catalog" in u]          # ni siquiera pide el catálogo
    status = epage.evaluate("""fetch('/admin/catalog/services', {headers: {Authorization:
        'Bearer ' + localStorage.getItem('access_token')}}).then(r => r.status)""")
    assert status == 403
    emp.shot("02-empleado-servicios-escritorio")
    epage.click("#empServicesNavPills [data-go=schedule]")                  # la navegación del 3c sigue andando
    expect(epage.locator("#scheduleSection")).to_be_visible()


def test_crear_servicio_con_apostrofo_y_aparece_sin_marcar_en_mis_servicios(tabs, server):
    tab = tabs().login("maxi").open_services()
    page = tab.page
    name = "Cejas d'autor E2E (premium)"
    page.click("#ownerServiceAddBtn")
    form = page.locator("#ownerServiceNew form")
    form.locator(".owner-name-input").fill(name)
    form.locator(".owner-price-input").fill("5.000")
    form.locator("select").select_option("20")
    tab.shot("03-duenio-nuevo-servicio-escritorio")
    form.locator("button[type=submit]").click()

    expect(page.locator("#ownerServicesStatus")).to_contain_text(f"Servicio “{name}” creado")
    row = tab.service_row(name)
    expect(row.locator(".owner-name")).to_have_text(name)
    expect(row.locator(".owner-meta")).to_have_text("$5.000 · 20 min (duración base)")
    expect(row.locator(".owner-warn")).to_contain_text("Todavía ningún barbero hace este servicio")
    assert service_by_name(server, name) == (5000, 20, True)

    mine = tab.my_service_row(name)
    expect(mine).to_be_visible()
    expect(mine.locator(".my-svc-check")).not_to_be_checked()
    expect(mine.locator(".my-svc-duration")).to_have_value("20")


def test_nombres_con_html_se_ven_como_texto_y_no_abren_dialogos(tabs, server):
    tab = tabs().login("maxi").open_services()
    page = tab.page
    # 1) La API no acepta < > = en el nombre: el mensaje del servidor se ve en el formulario
    page.click("#ownerServiceAddBtn")
    form = page.locator("#ownerServiceNew form")
    form.locator(".owner-name-input").fill(XSS)
    form.locator(".owner-price-input").fill("1000")
    form.locator("button[type=submit]").click()
    expect(form.locator(".owner-form-error")).to_contain_text("El nombre solo puede tener letras")
    assert service_by_name(server, XSS) is None

    # 2) Si alguien lo carga a mano en la base, se muestra como texto en las tres tarjetas
    sid = add_service(server, XSS, price=1000)
    add_discount(server, "Promo XSS E2E", {sid: 900})
    page.reload()
    tab.open_services()
    expect(tab.service_row(XSS).locator(".owner-name")).to_have_text(XSS)
    expect(tab.discount_row("Promo XSS E2E").locator(".owner-price-name")).to_have_text(XSS)
    expect(tab.my_service_row(XSS).locator(".my-svc-name")).to_have_text(XSS)
    page.click("#ownerDiscountAddBtn")
    expect(page.locator("#ownerDiscountNew .owner-price-name", has_text=XSS)).to_have_text(XSS)
    assert page.locator("#servicesSection img").count() == 0
    page.wait_for_timeout(300)                     # por si un onerror tardara en dispararse
    assert tab.dialogs == []

    db = server.Session()                          # que no moleste a las otras pruebas
    db.get(Service, sid).active = False
    db.commit()
    db.close()


def test_editar_precio_y_el_409_por_un_descuento_mas_caro(tabs, server):
    sid = add_service(server, "Peinado E2E", price=8000)
    add_discount(server, "Promo Peinado E2E", {sid: 6000})
    tab = tabs().login("maxi").open_services()
    page = tab.page

    tab.service_row("Peinado E2E").get_by_role("button", name="Editar").click()
    form = page.locator("#ownerServicesList form")
    form.locator(".owner-price-input").fill("9000")
    form.locator("button[type=submit]").click()
    expect(page.locator("#ownerServicesStatus")).to_contain_text("Servicio “Peinado E2E” guardado")
    expect(tab.service_row("Peinado E2E").locator(".owner-meta")).to_have_text("$9.000 · 30 min (duración base)")
    expect(tab.discount_row("Promo Peinado E2E")).to_contain_text("(base $9.000)")   # descuentos al día
    assert service_by_name(server, "Peinado E2E")[0] == 9000

    tab.service_row("Peinado E2E").get_by_role("button", name="Editar").click()
    form = page.locator("#ownerServicesList form")
    form.locator(".owner-price-input").fill("5.000")
    form.locator("button[type=submit]").click()
    error = form.locator(".owner-form-error")
    expect(error).to_contain_text("No se puede poner $5.000")
    expect(error).to_contain_text("Promo Peinado E2E ($6.000)")
    tab.shot("04-duenio-error-409-escritorio")
    assert service_by_name(server, "Peinado E2E")[0] == 9000

    form.locator(".owner-price-input").fill("12,50")                        # formato inválido: ni se manda
    form.locator("button[type=submit]").click()
    expect(error).to_contain_text("sin centavos")
    form.get_by_role("button", name="Cancelar").click()
    expect(tab.service_row("Peinado E2E").locator(".owner-meta")).to_be_visible()


def test_desactivar_con_un_turno_futuro_cuenta_bien_y_reactivar(tabs, server):
    sid = add_service(server, "Color E2E", price=20000)
    add_future_appointment(server, sid, "Color E2E")
    tab = tabs().login("maxi").open_services()
    page = tab.page
    expect(tab.my_service_row("Color E2E")).to_be_visible()

    tab.service_row("Color E2E").get_by_role("button", name="Desactivar").click()
    status = page.locator("#ownerServicesStatus")
    expect(status).to_contain_text("Servicio “Color E2E” desactivado. Tiene 1 turno futuro reservado: sigue en pie.")
    expect(status).to_have_class("my-services-status is-warn")
    row = tab.service_row("Color E2E")
    expect(row.locator(".owner-badge")).to_have_text("Desactivado")
    expect(row).to_have_class("schedule-day-item owner-row day-off")
    expect(tab.my_service_row("Color E2E")).to_have_count(0)                # deja de aparecer en Mis servicios
    assert service_by_name(server, "Color E2E")[2] is False
    tab.shot("05-duenio-desactivado-escritorio")

    row.get_by_role("button", name="Reactivar").click()
    expect(status).to_contain_text("Servicio “Color E2E” reactivado.")
    expect(tab.service_row("Color E2E").locator(".owner-badge")).to_have_count(0)
    expect(tab.my_service_row("Color E2E")).to_be_visible()
    assert service_by_name(server, "Color E2E")[2] is True


def test_descuento_con_un_servicio_si_y_otro_vacio_y_errores(tabs, server):
    tab = tabs().login("maxi").open_services()
    page = tab.page
    corte, barba = service_id(server, "Corte"), service_id(server, "Barba")

    page.click("#ownerDiscountAddBtn")
    form = page.locator("#ownerDiscountNew form")
    form.locator(".owner-name-input").fill("Martes E2E")
    form.locator(".owner-form-error").wait_for(state="hidden")
    form.locator("button[type=submit]").click()                                  # sin ningún precio
    expect(form.locator(".owner-form-error")).to_have_text("Poné el precio con descuento de al menos un servicio.")
    form.get_by_label("Precio con descuento de Corte", exact=True).fill("12.000")
    tab.shot("06-duenio-nuevo-descuento-escritorio")
    form.locator("button[type=submit]").click()
    expect(page.locator("#ownerDiscountsStatus")).to_contain_text("Descuento “Martes E2E” creado")
    row = tab.discount_row("Martes E2E")
    expect(row.locator("li")).to_have_count(1)
    expect(row.locator("li")).to_contain_text("$12.000 (base $15.000)")
    db = server.Session()
    did = db.query(Discount.id).filter(Discount.name == "Martes E2E").scalar()
    db.close()
    assert discount_prices(server, did) == {corte: 12000}

    # Precio mayor al base: lo frena el servidor y el mensaje se ve en el formulario
    row.get_by_role("button", name="Editar").click()
    form = page.locator("#ownerDiscountsList form")
    expect(form.get_by_label("Precio con descuento de Corte", exact=True)).to_have_value("12000")
    form.get_by_label("Precio con descuento de Barba", exact=True).fill("20000")
    form.locator("button[type=submit]").click()
    expect(form.locator(".owner-form-error")).to_contain_text(
        "El precio de “Martes E2E” para “Barba” ($20.000) no puede ser mayor al precio del servicio ($10.000).")
    assert discount_prices(server, did) == {corte: 12000}

    # Con un precio válido, el PATCH reemplaza la lista: Corte vacío deja de aplicar
    form.get_by_label("Precio con descuento de Corte", exact=True).fill("")
    form.get_by_label("Precio con descuento de Barba", exact=True).fill("8000")
    form.locator("button[type=submit]").click()
    expect(page.locator("#ownerDiscountsStatus")).to_contain_text("Descuento “Martes E2E” guardado")
    assert discount_prices(server, did) == {barba: 8000}


def test_desactivar_y_reactivar_descuento_y_precio_de_servicio_desactivado(tabs, server):
    navaja = add_service(server, "Navaja E2E", price=9000)
    did = add_discount(server, "Socios E2E", {navaja: 7000, service_id(server, "Corte"): 13000})
    tab = tabs().login("maxi").open_services()
    page = tab.page

    tab.service_row("Navaja E2E").get_by_role("button", name="Desactivar").click()
    expect(page.locator("#ownerServicesStatus")).to_contain_text("No tiene turnos futuros.")
    row = tab.discount_row("Socios E2E")
    expect(row.locator("li.owner-price-off")).to_contain_text("$7.000 · servicio desactivado (se conserva)")

    row.get_by_role("button", name="Editar").click()
    form = page.locator("#ownerDiscountsList form")
    expect(form.locator(".owner-kept")).to_contain_text("Navaja E2E")
    expect(form.get_by_label("Precio con descuento de Navaja E2E")).to_have_count(0)   # no se puede editar
    form.get_by_label("Precio con descuento de Corte", exact=True).fill("12500")
    form.locator("button[type=submit]").click()
    expect(page.locator("#ownerDiscountsStatus")).to_contain_text("guardado")
    assert discount_prices(server, did) == {navaja: 7000, service_id(server, "Corte"): 12500}   # Navaja se conserva

    tab.discount_row("Socios E2E").get_by_role("button", name="Desactivar").click()
    expect(page.locator("#ownerDiscountsStatus")).to_contain_text("Descuento “Socios E2E” desactivado.")
    expect(tab.discount_row("Socios E2E").locator(".owner-badge")).to_have_text("Desactivado")
    tab.shot("07-duenio-descuentos-escritorio")
    tab.discount_row("Socios E2E").get_by_role("button", name="Reactivar").click()
    expect(page.locator("#ownerDiscountsStatus")).to_contain_text("Descuento “Socios E2E” reactivado.")
    expect(tab.discount_row("Socios E2E").locator(".owner-badge")).to_have_count(0)


def test_celular_360_sin_scroll_horizontal(tabs, server):
    sid = add_service(server, "Diseño de cejas con navaja premium XL", price=999999, duration=180)
    add_discount(server, "Promo de invierno para socios del club", {sid: 888888})
    tab = tabs(width=360, height=800).login("maxi").open_services()
    page = tab.page
    tab.no_horizontal_scroll()
    tab.shot("11-duenio-servicios-celular")

    tab.service_row("Diseño de cejas con navaja premium XL").get_by_role("button", name="Editar").click()
    tab.no_horizontal_scroll()
    tab.shot("12-duenio-editar-servicio-celular")
    page.locator("#ownerServicesList form").get_by_role("button", name="Cancelar").click()

    page.click("#ownerServiceAddBtn")
    tab.no_horizontal_scroll()
    page.locator("#ownerServiceNew form").get_by_role("button", name="Cancelar").click()

    tab.discount_row("Promo de invierno para socios del club").get_by_role("button", name="Editar").click()
    tab.no_horizontal_scroll()
    page.locator("#ownerDiscountsCard").scroll_into_view_if_needed()
    tab.shot("13-duenio-editar-descuento-celular")
    page.locator("#ownerDiscountsList form").get_by_role("button", name="Cancelar").click()

    page.click("#ownerDiscountAddBtn")
    tab.no_horizontal_scroll()
    tab.shot("14-duenio-nuevo-descuento-celular")

    emp = tabs(width=360, height=800).login("leo").open_services(owner=False)
    emp.no_horizontal_scroll()
    emp.shot("15-empleado-servicios-celular")


def test_sesion_vencida_al_guardar_vuelve_al_login_sin_guardar(tabs, server):
    add_service(server, "Lavado E2E", price=3000)
    tab = tabs().login("maxi").open_services()
    page = tab.page
    tab.service_row("Lavado E2E").get_by_role("button", name="Editar").click()
    form = page.locator("#ownerServicesList form")
    form.locator(".owner-price-input").fill("3500")
    page.evaluate("localStorage.setItem('access_token', 'token-vencido')")
    form.locator("button[type=submit]").click()
    expect(page.locator("#loginSection")).to_be_visible()
    expect(page.locator("#servicesSection")).to_be_hidden()
    assert service_by_name(server, "Lavado E2E")[0] == 3000

    tab.login("maxi").open_services()                                       # con un descuento, igual
    page.click("#ownerDiscountAddBtn")
    form = page.locator("#ownerDiscountNew form")
    form.locator(".owner-name-input").fill("Vencido E2E")
    form.get_by_label("Precio con descuento de Corte", exact=True).fill("10000")
    page.evaluate("localStorage.removeItem('access_token')")
    form.locator("button[type=submit]").click()
    expect(page.locator("#loginSection")).to_be_visible()
    db = server.Session()
    assert db.query(Discount).filter(Discount.name == "Vencido E2E").count() == 0
    db.close()
