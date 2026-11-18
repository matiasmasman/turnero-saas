"""Pruebas de navegador del Bloque 4: la agenda con cancelados (tachados, "Cancelado por"), completados y el orden;
cancelar desde el panel; "Error al cargar." sin cerrar la sesión. Playwright + Firefox headless.

- Mismo armado que test_navegador_3f.py: la app corre aparte (tests/e2e_server.py) con base SQLite y log de
  seguridad TEMPORALES, en 127.0.0.1 y un puerto libre. La carpeta temporal se borra al terminar.
- El "ahora" del negocio está congelado en el servidor (E2E_FAKE_NOW = mié 05/06/2030 15:00): "Completado" no
  depende de la hora en que se corren las pruebas. El navegador NO sabe esa hora: la toma del header
  X-Business-Now de GET /admin/appointments (#46). Una prueba usa además un navegador en la zona de Tokio.
- Staff de `manage_staff init`: 1 Maxi (dueño), 2 Juampa, 3 Leo, 4 Pedro.
- Si Playwright o su Firefox no están instalados, estas pruebas se saltean.
- Capturas: solo si EB4_CAPTURAS apunta a una carpeta (por ejemplo ~/bloque4-capturas).
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="sin Playwright: se saltean las pruebas de navegador")
expect = sync_api.expect

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import Appointment, User  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "clave-e2e-123"
FAKE_NOW = "2030-06-05T15:00:00"            # miércoles, 15:00 en el negocio
TODAY = FAKE_NOW[:10]
XSS = "<img src=x onerror=alert(1)>"
LONG_NAME = "Maximiliano Bartolomé de la Santísima Trinidad"
_REAL_SOCKET = socket.socket
SHOTS = Path(os.environ["EB4_CAPTURAS"]).expanduser() if os.getenv("EB4_CAPTURAS") else None
MAXI, JUAMPA, LEO = 1, 2, 3


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
    env = {**os.environ, "E2E_FAKE_NOW": FAKE_NOW}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "e2e_server.py"), str(tmp), PASSWORD],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
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
    """El panel en un contexto nuevo que junta errores de consola, diálogos y pedidos.
    Los diálogos se rechazan, salvo que la prueba diga accept_dialogs()."""

    def __init__(self, browser, server, width=1280, height=900, tz="America/Argentina/Buenos_Aires"):
        self.server = server
        self.context = browser.new_context(viewport={"width": width, "height": height}, locale="es-AR",
                                           timezone_id=tz)
        self.page = self.context.new_page()
        self.errors, self.dialogs, self.expected_dialogs = [], [], []
        self.accept = False
        self.page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}")
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on("dialog", self._dialog)

    def _dialog(self, dialog):
        if self.accept:
            self.expected_dialogs.append(dialog.message)
            dialog.accept()
        else:
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

    def items(self):
        return self.page.locator("#agendaContent .appointment-item")

    def item(self, hhmm):
        return self.items().filter(has=self.page.locator(".appt-time", has_text=re.compile(rf"^{hhmm} ")))

    def order(self):
        return [t[:5] for t in self.items().locator(".appt-time").all_inner_texts()]

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
    db = server.Session()                       # cada prueba empieza sin turnos y con los nombres de siempre
    db.query(Appointment).delete()
    db.query(User).filter(User.id == LEO).update({"name": "Leo"})
    db.query(User).filter(User.id == MAXI).update({"name": "Maxi"})
    db.commit()
    db.close()
    assert not problems, f"errores de consola o diálogos inesperados: {problems}"


# ---------- Datos directos en la base temporal ----------
def add_appts(server, employee_id, rows):
    """rows = [(hora, duración, status, cancelled_by_id)]. Todos hoy (el hoy del servidor)."""
    db = server.Session()
    for hhmm, duration, status, cancelled_by in rows:
        db.add(Appointment(client_name=f"Cliente {hhmm}", client_phone="2610000000", service="Corte", date=TODAY,
                           time=f"{hhmm}:00", duration=duration, price=10000, status=status,
                           employee_id=employee_id, cancelled_by_id=cancelled_by))
    db.commit()
    db.close()


def rename(server, user_id, name):
    db = server.Session()
    db.query(User).filter(User.id == user_id).update({"name": name})
    db.commit()
    db.close()


AGENDA = [                                   # (hora, duración, estado, quién canceló)
    ("16:00", 30, "confirmed", None),         # viene
    ("10:00", 30, "confirmed", None),         # completado
    ("11:00", 30, "cancelled", MAXI),         # cancelado desde el panel por el dueño
    ("14:45", 30, "confirmed", None),         # en curso (termina 15:15): todavía no está completado
    ("12:00", 30, "cancelled", None),         # canceló el cliente por WhatsApp
    ("14:30", 30, "confirmed", None),         # termina justo a las 15:00: completado
    ("13:00", 30, "cancelled", LEO),          # cancelado por un usuario con nombre raro (XSS)
]
EXPECTED_ORDER = ["14:45", "16:00", "10:00", "14:30", "11:00", "12:00", "13:00"]


def check_agenda(tab):
    assert tab.order() == EXPECTED_ORDER                       # vienen → completados → cancelados

    for hhmm in ("14:45", "16:00"):                            # los que vienen: como siempre
        it = tab.item(hhmm)
        expect(it).not_to_have_class(re.compile(r"appt-(completed|cancelled)"))
        expect(it.locator(".completed-tag, .cancelled-tag")).to_have_count(0)
        expect(it.locator(".price-tag")).to_have_text("$10.000")
        expect(it.locator(".btn-cancel")).to_have_count(1)

    for hhmm in ("10:00", "14:30"):                            # completados: más opacos, con etiqueta y Cancelar
        it = tab.item(hhmm)
        expect(it).to_have_class(re.compile(r"\bappt-completed\b"))
        expect(it.locator(".completed-tag")).to_have_text("Completado")
        expect(it.locator(".price-tag")).to_have_text("$10.000")
        expect(it.locator(".btn-cancel")).to_have_count(1)
        assert float(it.locator(".appt-details").evaluate("e => getComputedStyle(e).opacity")) < 1

    labels = {"11:00": "Cancelado por Maxi", "12:00": "Canceló por WhatsApp", "13:00": f"Cancelado por {XSS}"}
    for hhmm, label in labels.items():                         # cancelados: tachados, sin precio ni Cancelar
        it = tab.item(hhmm)
        expect(it).to_have_class(re.compile(r"\bappt-cancelled\b"))
        expect(it.locator(".cancelled-tag")).to_have_text(label)
        expect(it.locator(".price-tag")).to_have_count(0)
        expect(it.locator(".btn-cancel")).to_have_count(0)
        expect(it.locator(".btn-wpp")).to_have_count(1)
        assert "line-through" in it.locator(".name").evaluate("e => getComputedStyle(e).textDecorationLine")

    assert tab.page.locator("#agendaContent img").count() == 0  # el nombre raro se ve como texto


# ---------- Pruebas ----------
def test_cancelados_completados_y_orden(tabs, server):
    rename(server, LEO, XSS)
    add_appts(server, JUAMPA, AGENDA)
    tab = tabs().login("juampa").only_today()
    expect(tab.page.locator("#agendaContent .day-title")).to_contain_text("05/06 (Hoy)")   # el hoy del servidor
    check_agenda(tab)
    tab.shot("01-agenda-escritorio")


def test_la_hora_es_la_del_servidor_aunque_el_navegador_este_en_otra_zona(tabs, server):
    """En Tokio ya es 06/06 a las 03:00, pero manda el negocio: hoy es 05/06 y son las 15:00 (#46)."""
    rename(server, LEO, XSS)
    add_appts(server, JUAMPA, AGENDA)
    tab = tabs(tz="Asia/Tokyo").login("juampa").only_today()
    expect(tab.page.locator("#agendaContent .day-title")).to_contain_text("05/06 (Hoy)")
    check_agenda(tab)


def test_cancelar_desde_el_panel(tabs, server):
    add_appts(server, JUAMPA, [("16:00", 30, "confirmed", None), ("17:00", 30, "confirmed", None),
                               ("10:00", 30, "confirmed", None)])
    tab = tabs().login("juampa").only_today()
    assert tab.order() == ["16:00", "17:00", "10:00"]
    tab.accept = True
    tab.item("16:00").locator(".btn-cancel").click()
    expect(tab.item("16:00").locator(".cancelled-tag")).to_have_text("Cancelado por Juampa")
    assert tab.order() == ["17:00", "10:00", "16:00"]          # pasó al final, tachado
    expect(tab.item("16:00").locator(".btn-cancel")).to_have_count(0)
    expect(tab.item("16:00").locator(".price-tag")).to_have_count(0)
    assert len(tab.expected_dialogs) == 1 and "el cliente no recibe aviso" in tab.expected_dialogs[0]

    db = server.Session()                                      # sigue en la base, cancelado por Juampa
    rows = {a.time[:5]: (a.status, a.cancelled_by_id) for a in db.query(Appointment)}
    db.close()
    assert rows == {"16:00": ("cancelled", JUAMPA), "17:00": ("confirmed", None), "10:00": ("confirmed", None)}
    tab.shot("02-despues-de-cancelar")


def test_si_otro_ya_lo_cancelo_avisa_y_refresca(tabs, server):
    add_appts(server, JUAMPA, [("16:00", 30, "confirmed", None)])
    tab = tabs().login("juampa").only_today()
    db = server.Session()                                      # mientras tanto, lo cancela el dueño
    db.query(Appointment).update({"status": "cancelled", "cancelled_by_id": MAXI})
    db.commit()
    db.close()
    tab.accept = True
    tab.item("16:00").locator(".btn-cancel").click()
    expect(tab.item("16:00").locator(".cancelled-tag")).to_have_text("Cancelado por Maxi")
    assert tab.expected_dialogs[-1] == "Ese turno ya estaba cancelado."
    tab.errors = [e for e in tab.errors if "409" not in e]     # el 409 que Firefox anota en la consola


@pytest.mark.parametrize("failure", ["500", "sin conexión"])
def test_error_al_cargar_no_cierra_la_sesion(tabs, server, failure):
    add_appts(server, JUAMPA, [("16:00", 30, "confirmed", None)])
    tab = tabs().login("juampa")
    page = tab.page

    def broken(route):
        if failure == "500":
            route.fulfill(status=500, body="Internal Server Error")
        else:
            route.abort("internetdisconnected")
    page.route(re.compile(r".*/admin/appointments$"), broken)
    page.reload()
    expect(page.locator("#agendaContent")).to_have_text("Error al cargar.")
    expect(page.locator("#dashboardSection")).to_be_visible()   # sigue adentro
    expect(page.locator("#loginSection")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('access_token')")   # el token no se borró
    tab.shot(f"03-error-al-cargar-{failure.replace(' ', '-')}")

    page.unroute(re.compile(r".*/admin/appointments$"))       # vuelve el servidor: anda sin volver a entrar
    page.reload()
    expect(tab.item("16:00")).to_have_count(1)
    tab.errors = [e for e in tab.errors if "500" not in e and "NetworkError" not in e]


def test_un_401_sigue_yendo_al_login(tabs, server):
    tab = tabs().login("juampa")
    page = tab.page
    page.route(re.compile(r".*/admin/appointments$"), lambda r: r.fulfill(status=401, body="{}"))
    page.reload()
    expect(page.locator("#loginSection")).to_be_visible()
    expect(page.locator("#loginError")).to_have_text("Tu sesión venció. Entrá de nuevo.")
    assert page.evaluate("localStorage.getItem('access_token')") is None
    tab.errors = [e for e in tab.errors if "401" not in e]


def test_celular_360_sin_scroll_horizontal(tabs, server):
    rename(server, MAXI, LONG_NAME)
    add_appts(server, JUAMPA, AGENDA)
    tab = tabs(width=360, height=800).login("juampa").only_today()
    expect(tab.item("11:00").locator(".cancelled-tag")).to_have_text(f"Cancelado por {LONG_NAME}")
    tab.no_horizontal_scroll()
    for hhmm in ("11:00", "10:00", "16:00"):
        box = tab.item(hhmm).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 360, (hhmm, box)
    tab.shot("04-agenda-celular")
