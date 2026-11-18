"""Pruebas del Bloque 3 (seguridad). Datos 100 % de prueba.

#1 JWT de 24 h, exp obligatorio, algoritmo fijo, tope de vida (los tokens viejos de 7 días no pasan).
#2 Límite de login en memoria (usuario+IP y solo IP), 429 sin revisar la contraseña, AUTH_RATE_LIMITED.
#3 Escapado: en panel.js ningún dato del servidor entra crudo a un template de HTML.
#5 Todos los eventos de un pedido llevan ip_address (la de request.client), request_id y, con login, actor_id.
#27 /test-alert ya no existe.  #28 /docs, /redoc y /openapi.json apagados salvo API_DOCS=true.

Usa el fixture `api` de conftest.py (SQLite en memoria y, con E3_TEST_PG_URL, también PostgreSQL).
Las pruebas de navegador del bloque están en test_navegador_bloque3.py.
"""
import importlib
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jose import jwt

import app.catalog as catalog
from app.core.telemetry import sec_logger
from app.models import Appointment, ScheduleException, User, UserRole
from test_reserva_3e import ANA, DAY, NOW, book, shop  # noqa: F401  (shop es un fixture)

ROOT = Path(__file__).resolve().parent.parent
PANEL_JS = (ROOT / "frontend/js/panel.js").read_text(encoding="utf-8")
PANEL_HTML = (ROOT / "frontend/panel.html").read_text(encoding="utf-8")
PASSWORD = "clave-de-prueba-123"
TOO_MANY = "Demasiados intentos. Probá de nuevo en unos minutos."


# ---------- Ayudas ----------
class _FullHandler(logging.Handler):
    """Como `events` de conftest, pero guarda también ip_address, request_id y el nivel."""

    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        self.events.append({"event_type": getattr(record, "event_type", None),
                            "level": record.levelname,
                            "ip_address": getattr(record, "ip_address", "UNKNOWN"),
                            "request_id": getattr(record, "request_id", None),
                            "actor_id": getattr(record, "actor_id", None),
                            "message": record.getMessage(),
                            "metadata": getattr(record, "metadata", {})})


@pytest.fixture
def log():
    handler = _FullHandler()
    sec_logger.addHandler(handler)
    yield handler.events
    sec_logger.removeHandler(handler)


def of(events, event_type):
    return [e for e in events if e["event_type"] == event_type]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def users(api, monkeypatch):
    """Dueño "maxi" y empleado "juampa" con la misma clave (bcrypt con pocas vueltas: más rápido).
    El hash "de mentira" (usuario inexistente) también rápido, salvo en la prueba que mide el tiempo."""
    from app.core.security import pwd_context
    import app.routers.auth as auth
    fast = pwd_context.using(bcrypt__rounds=4)
    monkeypatch.setattr(auth, "_dummy_hash", fast.hash("nadie-la-sabe"), raising=False)
    db = api.Session()
    owner = User(name="Maxi", username="maxi", hashed_password=fast.hash(PASSWORD), role=UserRole.OWNER)
    emp = User(name="Juampa", username="juampa", hashed_password=fast.hash(PASSWORD), role=UserRole.EMPLOYEE)
    db.add_all([owner, emp])
    db.commit()
    ids = {"owner": owner.id, "employee": emp.id}
    db.close()
    return ids


@pytest.fixture
def clock(api):
    """Limitador nuevo con un reloj de prueba (la app real usa time.monotonic)."""
    from app.core.login_limiter import LoginLimiter
    c = FakeClock()
    api.client.app.state.login_limiter = LoginLimiter(clock=c)
    return c


def login(client, username, password):
    return client.post("/auth/login", data={"username": username, "password": password})


def bearer(payload, key=None, algorithm="HS256"):
    from app.core.security import SECRET_KEY
    return {"Authorization": f"Bearer {jwt.encode(payload, key or SECRET_KEY, algorithm=algorithm)}"}


def ts(delta):
    return int((datetime.now(timezone.utc) + delta).timestamp())


# =====================================================================
# #1 JWT
# =====================================================================
def test_el_token_dura_24_horas_y_trae_iat(api, users):
    from app.core.security import SECRET_KEY
    resp = login(api.client, "maxi", PASSWORD)
    assert resp.status_code == 200, resp.text
    payload = jwt.decode(resp.json()["access_token"], SECRET_KEY, algorithms=["HS256"])
    assert payload["exp"] - payload["iat"] == 24 * 3600
    assert abs(payload["exp"] - ts(timedelta(hours=24))) <= 5


def test_duracion_y_algoritmo_fijos_en_el_codigo_no_en_el_env():
    from app.core import security
    assert security.ACCESS_TOKEN_EXPIRE_MINUTES == 24 * 60
    assert security.ALGORITHM == "HS256"
    src = (ROOT / "app/core/security.py").read_text(encoding="utf-8")
    assert re.search(r"getenv\(\s*['\"](ACCESS_TOKEN_EXPIRE_MINUTES|ALGORITHM)", src) is None
    assert re.search(r"environ.*(ACCESS_TOKEN_EXPIRE_MINUTES|ALGORITHM)", src) is None
    assert "algorithms=[ALGORITHM]" in src and '"require_exp": True' in src


def test_token_bueno_entra(api, users):
    resp = api.client.get("/admin/me", headers=bearer({"sub": str(users["owner"]), "exp": ts(timedelta(hours=1))}))
    assert resp.status_code == 200


@pytest.mark.parametrize("case", ["sin_exp", "sin_sub", "sub_no_numerico", "vencido", "viejo_de_7_dias",
                                  "otra_clave", "HS512", "alg_none", "basura"])
def test_tokens_que_no_pasan(api, users, case):
    sub = str(users["owner"])
    good_exp = ts(timedelta(hours=1))
    headers = {
        "sin_exp": lambda: bearer({"sub": sub}),
        "sin_sub": lambda: bearer({"exp": good_exp}),
        "sub_no_numerico": lambda: bearer({"sub": "abc", "exp": good_exp}),
        "vencido": lambda: bearer({"sub": sub, "exp": ts(timedelta(seconds=-5))}),
        # como los de antes: 7 días; con más de 24 h por delante no pasa aunque la firma sea buena
        "viejo_de_7_dias": lambda: bearer({"sub": sub, "role": "owner", "exp": ts(timedelta(days=6, hours=20))}),
        "otra_clave": lambda: bearer({"sub": sub, "exp": good_exp}, key="z" * 40),
        "HS512": lambda: bearer({"sub": sub, "exp": good_exp}, algorithm="HS512"),
        "alg_none": lambda: {"Authorization": "Bearer " + _unsigned({"sub": sub, "exp": good_exp})},
        "basura": lambda: {"Authorization": "Bearer no-es-un-token"},
    }[case]()
    resp = api.client.get("/admin/me", headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "No se pudo validar las credenciales"


def _unsigned(payload):
    import base64
    import json
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}."


def test_token_viejo_con_menos_de_24_h_restantes_sigue_hasta_vencer(api, users):
    """Un token de 7 días emitido hace más de 6 días ya tiene < 24 h por delante: anda hasta su exp (como uno nuevo)."""
    headers = bearer({"sub": str(users["owner"]), "exp": ts(timedelta(hours=23))})
    assert api.client.get("/admin/me", headers=headers).status_code == 200


# =====================================================================
# #2 Límite de login
# =====================================================================
def test_cinco_fallidos_del_mismo_usuario_e_ip_bloquean(api, users, clock, log, monkeypatch):
    for i in range(4):
        resp = login(api.client, "maxi", f"mala-{i}")
        assert resp.status_code == 401
    resp = login(api.client, "maxi", "mala-4")                       # el 5.º ya avisa
    assert resp.status_code == 429 and resp.json() == {"detail": TOO_MANY}
    limited = of(log, "AUTH_RATE_LIMITED")
    assert len(limited) == 1 and limited[0]["level"] == "WARNING"
    assert limited[0]["metadata"]["scope"] == "user_ip" and limited[0]["metadata"]["failures"] == 5
    assert limited[0]["ip_address"] == "testclient" and limited[0]["request_id"]

    # Bloqueado: ni con la clave buena, y la contraseña NO se revisa
    import app.routers.auth as auth
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda *a: calls.append(1) or real(*a))
    for _ in range(3):
        resp = login(api.client, "maxi", PASSWORD)
        assert resp.status_code == 429 and resp.json()["detail"] == TOO_MANY
    assert calls == []
    assert len(of(log, "AUTH_FAILED")) == 5                          # los bloqueados no suman eventos
    assert len(of(log, "AUTH_RATE_LIMITED")) == 1

    clock.advance(15 * 60 - 1)
    assert login(api.client, "maxi", PASSWORD).status_code == 429
    clock.advance(2)                                                  # pasaron los 15 minutos
    assert login(api.client, "maxi", PASSWORD).status_code == 200


def test_el_usuario_se_normaliza(api, users, clock):
    for name in ["maxi", "MAXI", " Maxi", "maxi ", "mAxI"]:
        last = login(api.client, name, "mala")
    assert last.status_code == 429
    assert login(api.client, "maxi", PASSWORD).status_code == 429


def test_otro_usuario_y_otra_ip_no_quedan_bloqueados(api, users, clock):
    for _ in range(5):
        login(api.client, "maxi", "mala")
    assert login(api.client, "juampa", PASSWORD).status_code == 200       # otro usuario, misma IP
    other_ip = TestClient(api.client.app, client=("203.0.113.7", 50000))
    assert login(other_ip, "maxi", PASSWORD).status_code == 200            # mismo usuario, otra IP


def test_ventana_de_15_minutos(api, users, clock):
    for _ in range(4):
        login(api.client, "maxi", "mala")
    clock.advance(15 * 60 + 1)                                              # los 4 viejos ya no cuentan
    assert login(api.client, "maxi", "mala").status_code == 401


def test_un_login_correcto_limpia_el_contador_de_ese_usuario_e_ip(api, users, clock):
    for _ in range(4):
        login(api.client, "maxi", "mala")
    assert login(api.client, "maxi", PASSWORD).status_code == 200
    for _ in range(4):
        assert login(api.client, "maxi", "mala").status_code == 401        # empieza de cero
    assert login(api.client, "maxi", "mala").status_code == 429


def test_veinte_fallidos_desde_una_ip_bloquean_la_ip(api, users, clock, log):
    for i in range(19):
        assert login(api.client, f"nadie{i % 5}-{i // 5}", "mala").status_code == 401   # ninguno llega a 5
    resp = login(api.client, "otro-mas", "mala")
    assert resp.status_code == 429 and resp.json()["detail"] == TOO_MANY
    limited = of(log, "AUTH_RATE_LIMITED")
    assert [e["metadata"]["scope"] for e in limited] == ["ip"]
    assert limited[0]["metadata"]["username_attempted"] is None and limited[0]["metadata"]["failures"] == 20
    assert login(api.client, "juampa", PASSWORD).status_code == 429        # bloqueada para todos
    other_ip = TestClient(api.client.app, client=("203.0.113.8", 50000))
    assert login(other_ip, "juampa", PASSWORD).status_code == 200
    clock.advance(15 * 60 + 1)
    assert login(api.client, "juampa", PASSWORD).status_code == 200


def test_usuario_inexistente_y_clave_mala_responden_igual(api, users, clock, monkeypatch):
    import app.routers.auth as auth
    checked = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda pw, h: checked.append(h) or real(pw, h))
    a = login(api.client, "maxi", "mala")
    b = login(api.client, "no-existe", "mala")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"detail": "Usuario o contraseña incorrectos"}
    assert a.headers.get("www-authenticate") == b.headers.get("www-authenticate") == "Bearer"
    assert len(checked) == 2 and all(h.startswith("$2") for h in checked)    # en los dos se revisa un bcrypt


def test_el_hash_de_mentira_tiene_el_mismo_costo_que_los_reales(monkeypatch):
    import app.routers.auth as auth
    from app.core.security import get_password_hash
    monkeypatch.setattr(auth, "_dummy_hash", None)
    real_cost = get_password_hash("x").split("$")[2]
    assert auth._dummy_password_hash().split("$")[2] == real_cost


def test_nunca_se_registra_la_contrasena(api, users, clock, log):
    secret = "Contraseña-Secreta-XYZ"
    for _ in range(25):
        login(api.client, "maxi", secret)
    login(api.client, "maxi", PASSWORD)
    assert log and secret not in repr(log)


def test_el_usuario_intentado_se_recorta_en_el_log(api, users, clock, log):
    login(api.client, "a" * 5000, "mala")
    failed = of(log, "AUTH_FAILED")[0]
    assert len(failed["metadata"]["username_attempted"]) == 100 and failed["ip_address"] == "testclient"


def test_la_memoria_tiene_tope_y_se_limpia_lo_vencido():
    from app.core import login_limiter as ll
    from app.core.login_limiter import LoginLimiter
    c = FakeClock()
    limiter = LoginLimiter(clock=c, max_keys=50)
    for i in range(500):
        limiter.record_failure(f"user{i}", f"10.0.{i // 250}.{i % 250}")
    users_ip, ips = limiter.sizes()
    assert users_ip <= 50 and ips <= 50
    c.advance(ll.WINDOW_SECONDS + ll.PURGE_EVERY_SECONDS + 1)
    limiter.is_blocked("x", "1.1.1.1")                                      # dispara la limpieza
    assert limiter.sizes() == (0, 0)


def test_un_bloqueo_vencido_se_limpia_de_la_memoria():
    from app.core import login_limiter as ll
    from app.core.login_limiter import LoginLimiter
    c = FakeClock()
    limiter = LoginLimiter(clock=c)
    for _ in range(5):
        limiter.record_failure("maxi", "1.2.3.4")
    assert limiter.is_blocked("maxi", "1.2.3.4")
    c.advance(ll.BLOCK_SECONDS + ll.PURGE_EVERY_SECONDS + 1)
    assert not limiter.is_blocked("maxi", "1.2.3.4")
    assert limiter.sizes() == (0, 0)


def test_el_comentario_del_worker_unico_esta():
    src = (ROOT / "app/core/login_limiter.py").read_text(encoding="utf-8")
    assert "UN SOLO worker" in src


# =====================================================================
# #5 IP real y actor_id en los eventos
# =====================================================================
@pytest.fixture
def staff(api, users):
    from app.core.security import create_access_token
    return {
        "owner": {"Authorization": f"Bearer {create_access_token({'sub': str(users['owner']), 'role': 'owner'})}"},
        "employee": {"Authorization": f"Bearer {create_access_token({'sub': str(users['employee']), 'role': 'employee'})}"},
        "ids": users,
    }


def _add_appt(api, employee_id):
    db = api.Session()
    appt = Appointment(client_name="Cliente", client_phone="2610000000", service="Corte", date="2031-03-31",
                       time="10:00:00", duration=30, price=1000, status="confirmed", employee_id=employee_id)
    db.add(appt)
    db.commit()
    appt_id = appt.id
    db.close()
    return appt_id


def _assert_from_request(event, actor_id):
    assert event["ip_address"] == "testclient", event                     # la IP del TestClient (request.client)
    assert event["request_id"], event
    assert event["actor_id"] == actor_id, event


def test_eventos_del_panel_llevan_ip_request_id_y_actor(api, staff, log, monkeypatch):
    monkeypatch.setattr(catalog, "now_local", lambda: datetime(2031, 3, 30, 9, 0))
    owner, emp = staff["owner"], staff["employee"]
    c = api.client
    appt_id = _add_appt(api, staff["ids"]["employee"])
    assert c.get("/admin/appointments", headers=owner).status_code == 200
    assert c.delete(f"/admin/appointments/{appt_id}", headers=emp).status_code == 204
    assert c.post("/admin/schedule", json={"schedule": []}, headers=emp).status_code == 200
    resp = c.post("/admin/exceptions", json={"date": "2031-04-02", "start_time": None, "end_time": None,
                                              "is_global": False}, headers=owner)
    assert resp.status_code == 201, resp.text
    exc_id = resp.json()["id"]
    assert c.post("/admin/exceptions", json={"date": "2031-04-02", "is_global": True}, headers=emp).status_code == 403
    assert c.delete(f"/admin/exceptions/{exc_id}", headers=emp).status_code == 403
    assert c.delete(f"/admin/exceptions/{exc_id}", headers=owner).status_code == 200
    assert c.get("/admin/metrics", headers=emp).status_code == 200
    assert c.get(f"/admin/metrics?employee_id={staff['ids']['owner']}", headers=emp).status_code == 403
    assert c.get("/admin/catalog/services", headers=emp).status_code == 403            # RBAC_VIOLATION

    expected = [("AGENDA_VIEWED", "owner"), ("APPOINTMENT_CANCELLED", "employee"), ("SCHEDULE_UPDATED", "employee"),
                ("EXCEPTION_CREATED", "owner"), ("RBAC_VIOLATION_GLOBAL_EXCEPTION", "employee"),
                ("IDOR_ATTEMPT_EXCEPTION_DELETE", "employee"), ("EXCEPTION_DELETED", "owner"),
                ("METRICS_VIEWED", "employee"), ("IDOR_ATTEMPT_METRICS_ACCESS", "employee"),
                ("RBAC_VIOLATION", "employee")]
    for event_type, who in expected:
        found = of(log, event_type)
        assert len(found) == 1, event_type
        _assert_from_request(found[0], staff["ids"][who])
    # Bloque 4: el evento suma source, el barbero del turno y cuántos avisos anuló (ver test_bloque4.py)
    assert of(log, "APPOINTMENT_CANCELLED")[0]["metadata"] == {
        "appointment_id": appt_id, "source": "panel", "employee_id": staff["ids"]["employee"],
        "notifications_voided": 0}


def test_eventos_de_login_llevan_ip(api, users, clock, log):
    login(api.client, "maxi", "mala")
    login(api.client, "maxi", PASSWORD)
    db = api.Session()
    db.query(User).filter(User.id == users["employee"]).update({"is_active": False})
    db.commit()
    db.close()
    login(api.client, "juampa", PASSWORD)
    _assert_from_request(of(log, "AUTH_FAILED")[0], None)
    _assert_from_request(of(log, "AUTH_SUCCESS")[0], users["owner"])
    _assert_from_request(of(log, "AUTH_BLOCKED_INACTIVE")[0], users["employee"])


def test_sesion_de_usuario_inactivo_lleva_ip(api, staff, log):
    db = api.Session()
    db.query(User).filter(User.id == staff["ids"]["employee"]).update({"is_active": False})
    db.commit()
    db.close()
    assert api.client.get("/admin/me", headers=staff["employee"]).status_code == 401
    _assert_from_request(of(log, "SESSION_REVOKED_INACTIVE_USER")[0], staff["ids"]["employee"])


def test_reserva_publica_lleva_ip_sin_actor(api, shop, log):
    assert book(api).status_code == 201
    _assert_from_request(of(log, "APPOINTMENT_CREATED")[0], None)
    assert book(api).status_code == 409                                    # mismo horario
    _assert_from_request(of(log, "APPOINTMENT_SLOT_TAKEN")[0], None)


def test_x_forwarded_for_no_se_lee_a_mano(api, staff, log):
    """La IP sale de request.client (uvicorn ya la resolvió). Un X-Forwarded-For inventado no cambia nada."""
    api.client.get("/admin/appointments", headers={**staff["owner"], "X-Forwarded-For": "6.6.6.6"})
    assert of(log, "AGENDA_VIEWED")[0]["ip_address"] == "testclient"
    for path in (ROOT / "app").rglob("*.py"):
        src = path.read_text(encoding="utf-8").lower()
        assert re.search(r"headers(\.get\(|\[)\s*['\"]x-forwarded-for", src) is None, path


def test_todos_los_eventos_de_los_routers_usan_log_extra():
    """Nadie arma ip_address a mano: todo pasa por app/core/request_info.log_extra (o _log de catalog.py)."""
    for path in list((ROOT / "app/routers").glob("*.py")) + [ROOT / "app/dependencies.py"]:
        src = path.read_text(encoding="utf-8")
        assert '"ip_address"' not in src and "client_ip\", None" not in src, path
        for call in re.finditer(r"sec_logger\.\w+\(", src):
            tail = src[call.end():call.end() + 400]
            assert "extra=log_extra(" in tail.split("sec_logger.")[0], (path, src[call.start():call.start() + 120])


# =====================================================================
# #27 /test-alert  ·  #28 documentación de la API
# =====================================================================
def test_test_alert_ya_no_existe(api, log):
    assert api.client.get("/test-alert").status_code == 404
    assert not of(log, "SECURITY_PROBE")
    assert "/test-alert" not in api.client.app.openapi()["paths"]
    for path in ["README.md", "app/main.py"]:
        assert "test-alert" not in (ROOT / path).read_text(encoding="utf-8"), path


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_documentacion_apagada_por_defecto(api, path):
    assert api.client.get(path).status_code == 404


@pytest.mark.parametrize("value, on", [(None, False), ("", False), ("false", False), ("True", False),
                                       ("TRUE", False), ("1", False), ("yes", False), (" true", False),
                                       ("true", True)])
def test_api_docs_solo_con_true_exacto(api, monkeypatch, value, on):
    main_module = sys.modules["app.main"]                     # el que importó `api`
    if value is None:
        monkeypatch.delenv("API_DOCS", raising=False)
    else:
        monkeypatch.setenv("API_DOCS", value)
    assert (main_module.docs_settings() == {}) is on


@pytest.fixture
def api_with_docs(api, monkeypatch):
    """La app importada de nuevo con API_DOCS=true (la base de prueba de `api` sigue puesta)."""
    monkeypatch.setenv("API_DOCS", "true")
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    yield TestClient(main.app)
    sys.modules.pop("app.main", None)


def test_con_api_docs_true_se_prenden(api_with_docs):
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert api_with_docs.get(path).status_code == 200, path
    assert "/auth/login" in api_with_docs.get("/openapi.json").json()["paths"]


# =====================================================================
# #3 Escapado en panel.js (control estático; el de navegador está en test_navegador_bloque3.py)
# =====================================================================
SERVER_OBJECTS = r"\b(app|exc|item|data|dia|currentUser|appointments|exceptions|series)\b\s*(\.|\[)"
SAFE_WRAPPERS = ("escapeHtml(", "Number(", "formatARS(", "formatCompactARS(")


NOT_HTML = (".textContent", "alert(", "confirm(", "fetch(", "Bearer ")


def html_template_interpolations(js: str):
    """Los ${...} de los template literals de panel.js que pueden terminar en HTML. Se saltean:
    - los de sentencias que no arman HTML (textContent, alert/confirm, URLs de fetch), aunque ocupen varias líneas;
    - los de un template que ya va entero adentro de escapeHtml(`...`)."""
    for m in re.finditer(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", js):
        statement = js[js.rfind(";", 0, m.start()) + 1:m.start()]
        if any(skip in statement for skip in NOT_HTML):
            continue
        opening = js.rfind("`", 0, m.start())
        if js[:opening].rstrip().endswith("escapeHtml("):
            continue
        yield js.count("\n", 0, m.start()) + 1, m.group(1).strip()


def test_panel_js_no_mete_datos_del_servidor_crudos_en_html():
    raw = [(n, expr) for n, expr in html_template_interpolations(PANEL_JS)
           if re.search(SERVER_OBJECTS, expr) and not expr.startswith(SAFE_WRAPPERS)]
    assert raw == [], f"datos del servidor sin escapar en panel.js: {raw}"


def test_el_control_detecta_lo_que_habia_antes():
    """El control de arriba, contra el loadExceptions de antes del Bloque 3, encuentra el problema."""
    old = "                            <span>• ${exc.employee_name}</span>\n"
    found = [expr for _, expr in html_template_interpolations(old) if re.search(SERVER_OBJECTS, expr)]
    assert found == ["exc.employee_name"]


def test_excepciones_escapan_todo():
    body = PANEL_JS[PANEL_JS.index("async function loadExceptions"):PANEL_JS.index("document.getElementById('exceptionForm')")]
    assert "const employeeName = escapeHtml(exc.employee_name);" in body
    assert "<span>• ${employeeName}</span>" in body
    assert "const formattedDate = escapeHtml(" in body and "? escapeHtml(`${String(exc.start_time)" in body
    assert 'onclick="deleteException(${safeId})"' in body and "const safeId = Number(exc.id);" in body


@pytest.mark.parametrize("name", ["index.js", "my-services.js", "owner-catalog.js"])
def test_los_otros_js_no_usan_innerhtml(name):
    from test_frontend_3d import code_only
    code = code_only((ROOT / "frontend/js" / name).read_text(encoding="utf-8"))
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in code, (name, sink)


# =====================================================================
# Panel: login con mensajes en el formulario y sesión vencida
# =====================================================================
def test_login_muestra_los_errores_en_el_formulario_sin_alert():
    handler = PANEL_JS[PANEL_JS.index("document.getElementById('loginForm')"):PANEL_JS.index("async function fetchAppointments")]
    assert "alert(" not in handler
    assert "response.status === 429" in handler and "showLoginMessage(LOGIN_MESSAGES[429])" in handler
    assert "429: 'Demasiados intentos. Probá de nuevo en unos minutos.'" in PANEL_JS
    form = PANEL_HTML[PANEL_HTML.index('<form id="loginForm">'):PANEL_HTML.index("</form>")]
    assert '<p id="loginError" class="my-services-status is-error" role="alert" hidden></p>' in form
    assert "el.textContent = text || '';" in PANEL_JS


def test_todo_401_del_panel_va_a_sesion_vencida():
    js_files = {name: (ROOT / "frontend/js" / name).read_text(encoding="utf-8")
                for name in ("panel.js", "my-services.js", "owner-catalog.js")}
    for name, js in js_files.items():
        for m in re.finditer(r"status === 401\)", js):
            after = js[m.end():m.end() + 60]
            assert "sessionExpired()" in after or "return 'expired';" in after, (name, after)
    assert "expired: 'Tu sesión venció. Entrá de nuevo.'" in PANEL_JS
    assert "sessionStorage.setItem('login_notice', 'expired');" in PANEL_JS
    # cada fetch con token de panel.js mira el 401 (salvo /auth/login, que no lleva token)
    fetches = PANEL_JS.count("'Authorization': `Bearer ${token}`")
    assert PANEL_JS.count("status === 401) return sessionExpired();") + PANEL_JS.count("return 'expired';") >= fetches


def test_versiones_de_cache_subidas():
    assert 'src="js/panel.js?v=15"' in PANEL_HTML          # v=15 en el Bloque 4
    assert 'src="js/my-services.js?v=3"' in PANEL_HTML
    assert 'src="js/owner-catalog.js?v=2"' in PANEL_HTML
