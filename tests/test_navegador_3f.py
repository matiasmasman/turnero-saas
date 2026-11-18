"""Pruebas de navegador del 3f: la agenda del panel muestra precio final + descuento en solo lectura, con
Playwright + Firefox headless.

- La app corre como proceso aparte (tests/e2e_server.py, con sus dos frenos): uvicorn en 127.0.0.1 en un puerto
  libre, base SQLite TEMPORAL ya inicializada y migrada (e3a + e3e), WhatsApp apagado, log de seguridad de prueba.
  La carpeta temporal se borra al terminar.
- Staff de `manage_staff init`: 1 Maxi (dueño), 2 Juampa, 3 Leo, 4 Pedro.
- Los turnos se cargan directo en la base temporal para HOY (hora de Buenos Aires, la misma que usa el navegador),
  y la agenda se mira con "Solo Hoy" (así anda también un domingo, que no es día laborable).
- Si Playwright o su Firefox no están instalados, estas pruebas se saltean.
- Capturas: solo si E3F_CAPTURAS apunta a una carpeta (por ejemplo ~/3f-capturas).
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="sin Playwright: se saltean las pruebas de navegador")
expect = sync_api.expect

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import Appointment  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "clave-e2e-123"
XSS = "<img src=x onerror=alert(1)>"
LONG = "Promo de invierno para socios del club X"          # 40 caracteres: el máximo de la API
BA = ZoneInfo("America/Argentina/Buenos_Aires")
_REAL_SOCKET = socket.socket          # al importar el módulo todavía no corre `sin_sockets`
SHOTS = Path(os.environ["E3F_CAPTURAS"]).expanduser() if os.getenv("E3F_CAPTURAS") else None
MAXI, JUAMPA, LEO = 1, 2, 3


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
    """El panel en un contexto nuevo (localStorage vacío) que junta errores de consola, diálogos y pedidos."""

    def __init__(self, browser, server, width=1280, height=900):
        self.server = server
        self.context = browser.new_context(viewport={"width": width, "height": height}, locale="es-AR",
                                           timezone_id="America/Argentina/Buenos_Aires")
        self.page = self.context.new_page()
        self.errors, self.dialogs, self.requests = [], [], []
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}")
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on("dialog", self._dialog)
        self.page.on("request", lambda r: self.requests.append((r.method, r.url)))

    def _dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()

    def login(self, username):
        self.page.goto(f"{self.server.url}/panel.html")
        self.page.fill("#username", username)
        self.page.fill("#password", PASSWORD)
        self.page.click("#loginForm button[type=submit]")
        expect(self.page.locator("#dashboardSection")).to_be_visible()
        expect(self.page.locator("#agendaContent")).not_to_have_text("Cargando...")
        return self

    def only_today(self):
        self.page.check("#onlyTodayCheck")
        expect(self.page.locator("#agendaContent .day-title")).to_have_count(1)
        return self

    def item(self, hhmm):
        return self.page.locator("#agendaContent .appointment-item").filter(
            has=self.page.locator(".appt-time", has_text=re.compile(rf"^{hhmm} ")))   # la de inicio, no la de fin

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
def today() -> str:
    return datetime.now(BA).date().isoformat()


def add_appts(server, employee_id, rows):
    """rows = [(hora, servicio, precio, discount_name, status)]. discount_name va directo a la base: la API
    no acepta < > = en los nombres, pero un turno cargado a mano (o un dato viejo) podría tenerlos."""
    db = server.Session()
    for hhmm, service, price, discount_name, status in rows:
        db.add(Appointment(client_name=f"Cliente {hhmm}", client_phone="2610000000", service=service, date=today(),
                           time=f"{hhmm}:00", duration=30, price=price, status=status, employee_id=employee_id,
                           discount_name=discount_name))
    db.commit()
    db.close()


AGENDA = [
    ("10:00", "Corte", 9000, "VIP", "confirmed"),                  # con descuento
    ("10:30", "Barba", 10000, None, "confirmed"),                  # turno viejo (antes del 3e): sin descuento
    ("11:00", "Corte + Barba", 18000, "", "confirmed"),            # descuento vacío: solo el precio
    ("11:30", "Corte", 7000, XSS, "confirmed"),                    # nombre con caracteres raros
    ("12:00", "Corte", 12000, LONG, "confirmed"),                  # nombre largo (40)
    ("12:30", "Corte", 12000, "Promo 2x", "cancelled"),            # canceló por WhatsApp: sin precio, como antes
]


# ---------- Pruebas ----------
def test_la_agenda_muestra_precio_y_descuento_en_solo_lectura(tabs, server):
    add_appts(server, JUAMPA, AGENDA)
    tab = tabs().login("juampa").only_today()
    page = tab.page

    expect(tab.item("10:00").locator(".price-tag")).to_have_text("$9.000 · VIP")
    expect(tab.item("10:30").locator(".price-tag")).to_have_text("$10.000")
    expect(tab.item("11:00").locator(".price-tag")).to_have_text("$18.000")
    expect(tab.item("11:30").locator(".price-tag")).to_have_text(f"$7.000 · {XSS}")    # se ve como texto
    expect(tab.item("12:00").locator(".price-tag")).to_have_text(f"$12.000 · {LONG}")
    cancelled = tab.item("12:30")
    expect(cancelled.locator(".cancelled-tag")).to_have_text("Canceló por WhatsApp")
    expect(cancelled.locator(".price-tag")).to_have_count(0)

    agenda = page.locator("#agendaContent")
    assert agenda.locator("img").count() == 0                                   # el XSS no se volvió HTML
    assert agenda.locator(".price-tag").evaluate_all("els => els.map(e => e.tagName)") == ["SPAN"] * 5
    expect(agenda.locator("button.price-tag, .tariff-pills, .tariff-btn, .price-caret")).to_have_count(0)
    expect(agenda).not_to_contain_text("▾")
    expect(agenda.locator("[onclick*='Tarifa'], [onclick*='tarifa'], [title='Cambiar tarifa']")).to_have_count(0)

    # WhatsApp y Cancelar siguen como estaban (los cambia el Bloque 4)
    expect(agenda.locator(".btn-wpp")).to_have_count(6)
    expect(agenda.locator(".btn-cancel")).to_have_count(5)

    # Tocar el precio no hace nada: ni pedidos ni cambios
    before = len(tab.requests)
    tab.item("10:00").locator(".price-tag").click()
    page.wait_for_timeout(300)                     # por si un onerror o un pedido tardara en dispararse
    assert tab.requests[before:] == []
    assert tab.dialogs == []
    assert not [u for _, u in tab.requests if "/service-prices" in u or u.endswith("/price")]
    tab.shot("01-agenda-empleado-escritorio")


def test_el_duenio_ve_el_descuento_de_cada_barbero(tabs, server):
    add_appts(server, MAXI, [("10:00", "Corte", 15000, None, "confirmed")])
    add_appts(server, LEO, [("11:00", "Corte", 9000, "VIP", "confirmed")])
    tab = tabs().login("maxi").only_today()
    expect(tab.item("10:00").locator(".price-tag")).to_have_text("$15.000")
    expect(tab.item("11:00")).to_have_count(0)                                  # por defecto, solo los suyos
    tab.page.check("#onlyMineCheck")                                            # "Todo el Local"
    expect(tab.item("11:00").locator(".price-tag")).to_have_text("$9.000 · VIP")
    expect(tab.item("11:00").locator(".barber-tag")).to_have_text("Leo")
    tab.shot("02-agenda-duenio-todo-el-local-escritorio")


def test_celular_360_sin_scroll_horizontal(tabs, server):
    add_appts(server, JUAMPA, AGENDA)
    tab = tabs(width=360, height=800).login("juampa").only_today()
    expect(tab.item("12:00").locator(".price-tag")).to_have_text(f"$12.000 · {LONG}")
    tab.no_horizontal_scroll()
    box = tab.item("12:00").locator(".price-tag").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 360, box
    tab.shot("03-agenda-empleado-celular")
