"""Pruebas de navegador del Bloque 3 (seguridad), con Playwright + Firefox headless.

- Mismo armado que test_navegador_3f.py: la app corre aparte (tests/e2e_server.py) con base SQLite y log de
  seguridad TEMPORALES, en 127.0.0.1 y un puerto libre. La carpeta temporal se borra al terminar.
- Staff de `manage_staff init`: 1 Maxi (dueño), 2 Juampa, 3 Leo, 4 Pedro.
- Si Playwright o su Firefox no están instalados, estas pruebas se saltean.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="sin Playwright: se saltean las pruebas de navegador")
expect = sync_api.expect

from jose import jwt  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import Appointment, ScheduleException, User  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "clave-e2e-123"
E2E_SECRET = "e2e-" + "k" * 40                 # la SECRET_KEY de tests/e2e_server.py
XSS = "<img src=x onerror=alert(1)>"
BA = ZoneInfo("America/Argentina/Buenos_Aires")
_REAL_SOCKET = socket.socket
SHOTS = Path(os.environ["EB3_CAPTURAS"]).expanduser() if os.getenv("EB3_CAPTURAS") else None
MAXI, JUAMPA, LEO, PEDRO = 1, 2, 3, 4


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


# ---------- Ayudas ----------
def security_log(server):
    path = server.tmp / "security.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def open_exceptions(tab):
    tab.page.locator("#dashboardSection button[onclick='showExceptions()']").click()
    expect(tab.page.locator("#exceptionsSection")).to_be_visible()
    expect(tab.page.locator("#exceptionsList")).not_to_have_text("Cargando...")


# ---------- #3: excepciones con datos raros se ven como texto ----------
def test_excepciones_con_nombre_de_barbero_html_se_ven_como_texto(tabs, server):
    tomorrow = (datetime.now(BA).date() + timedelta(days=1)).isoformat()
    db = server.Session()
    leo = db.get(User, LEO)
    old_name, leo.name = leo.name, XSS
    db.add(ScheduleException(date=tomorrow, start_time="10:00", end_time="11:00", reason="Bloqueo", employee_id=LEO))
    db.commit()
    db.close()
    try:
        tab = tabs().login("maxi")
        open_exceptions(tab)
        box = tab.page.locator("#exceptionsList")
        expect(box).to_contain_text(f"• {XSS}")                       # el nombre se ve tal cual, como texto
        expect(box).to_contain_text("10:00 a 11:00 hs")
        assert box.locator("img").count() == 0
        tab.page.wait_for_timeout(300)                                 # por si un onerror tardara
        assert tab.dialogs == []
        tab.shot("01-excepciones-nombre-raro")
    finally:
        db = server.Session()
        db.query(ScheduleException).delete()
        db.get(User, LEO).name = old_name
        db.commit()
        db.close()


# ---------- #1: sesión vencida → login con aviso ----------
def test_sesion_vencida_vuelve_al_login_con_aviso(tabs, server):
    tab = tabs().login("maxi")
    page = tab.page
    # Un token como los de antes: 7 días, firma buena. Con el tope de 24 h, la API devuelve 401.
    old = jwt.encode({"sub": str(MAXI), "role": "owner",
                      "exp": datetime.now(timezone.utc) + timedelta(days=6)}, E2E_SECRET, algorithm="HS256")
    page.evaluate(f"localStorage.setItem('access_token', '{old}')")
    page.locator("#dashboardSection button[onclick='showExceptions()']").click()
    expect(page.locator("#loginSection")).to_be_visible()
    expect(page.locator("#loginError")).to_have_text("Tu sesión venció. Entrá de nuevo.")
    assert page.evaluate("localStorage.getItem('access_token')") is None
    tab.shot("02-sesion-vencida")
    page.reload()                                                      # el aviso sale una sola vez
    expect(page.locator("#loginSection")).to_be_visible()
    expect(page.locator("#loginError")).to_be_hidden()
    tab.login("maxi")                                                  # y se puede entrar de nuevo
    expect(page.locator("#loginError")).to_be_hidden()


# ---------- #2: clave mala y 429 dentro del formulario ----------
def test_login_errores_y_429_dentro_del_formulario(tabs, server):
    tab = tabs()
    page = tab.page
    page.goto(f"{server.url}/panel.html")
    msg = page.locator("#loginForm #loginError")

    def try_login(password):
        page.fill("#username", "pedro")
        page.fill("#password", password)
        page.click("#loginForm button[type=submit]")

    for i in range(4):
        try_login(f"mala-{i}")
        expect(msg).to_have_text("Usuario o contraseña incorrectos.")
        page.evaluate("document.getElementById('loginError').hidden = true")    # para ver que vuelve a salir
    try_login("mala-4")
    expect(msg).to_have_text("Demasiados intentos. Probá de nuevo en unos minutos.")
    expect(msg).to_be_visible()
    try_login(PASSWORD)                                               # bloqueado: ni con la clave buena
    expect(msg).to_have_text("Demasiados intentos. Probá de nuevo en unos minutos.")
    expect(page.locator("#dashboardSection")).to_be_hidden()
    assert tab.dialogs == []                                           # sin alert()
    tab.shot("03-login-429")

    events = security_log(server)
    limited = [e for e in events if e["event_type"] == "AUTH_RATE_LIMITED"]
    assert len(limited) == 1 and limited[0]["level"] == "WARNING"
    assert limited[0]["ip_address"] == "127.0.0.1" and limited[0]["metadata"]["scope"] == "user_ip"
    assert len([e for e in events if e["event_type"] == "AUTH_FAILED"
                and e["metadata"]["username_attempted"] == "pedro"]) == 5
    assert PASSWORD not in json.dumps(events) and "mala-3" not in json.dumps(events)


# ---------- #5: la IP real y el actor en los eventos del panel ----------
def test_eventos_del_panel_con_ip_y_actor_en_el_log_real(tabs, server):
    db = server.Session()
    db.add(Appointment(client_name="Cliente IP", client_phone="2610000000", service="Corte",
                       date=datetime.now(BA).date().isoformat(), time="23:30:00", duration=30, price=1000,
                       status="confirmed", employee_id=MAXI))
    db.commit()
    db.close()
    tab = tabs().login("maxi")
    tab.page.check("#onlyTodayCheck")
    tab.page.remove_listener("dialog", tab._dialog)                    # el confirm() de Cancelar se acepta
    tab.page.on("dialog", lambda d: d.accept())
    tab.page.locator("#agendaContent .btn-cancel").first.click()
    expect(tab.page.locator("#agendaContent .btn-cancel")).to_have_count(0)
    events = security_log(server)
    for event_type in ("AUTH_SUCCESS", "AGENDA_VIEWED", "APPOINTMENT_CANCELLED"):
        found = [e for e in events if e["event_type"] == event_type and e["actor_id"] == MAXI]
        assert found, event_type
        assert found[-1]["ip_address"] == "127.0.0.1" and found[-1]["request_id"], found[-1]
    # Todo evento que sale de un pedido HTTP (tiene request_id) lleva la IP. Sin IP quedan solo los que no
    # vienen de un pedido (migraciones al arrancar, hilo de WhatsApp), que es lo correcto.
    from_requests = [e for e in events if e["request_id"]]
    assert from_requests and all(e["ip_address"] == "127.0.0.1" for e in from_requests), \
        [e for e in from_requests if e["ip_address"] != "127.0.0.1"]
