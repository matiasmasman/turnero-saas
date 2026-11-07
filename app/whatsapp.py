"""
Avisos por WhatsApp del turnero.

Cómo funciona:
1. Al reservar, se genera un código para el turno y la página muestra el botón
   "Quiero el recordatorio por WhatsApp" (link wa.me con el código ya escrito).
2. El cliente manda ese mensaje. El cartero (whatsapp.service en Node) lo deja en su /inbox.
3. Este dispatcher (un hilo dentro de FastAPI) lee el inbox, reconoce el código,
   y encola dos avisos en la tabla wa_notifications: la confirmación (ya) y el recordatorio (2 h antes).
4. El mismo hilo manda los avisos vencidos a través del cartero.
"""
import fcntl
import http.client
import json
import os
import re
import secrets
import socket
import threading
import time as time_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.core.telemetry import sec_logger
from app.database import SessionLocal
from app.models import Appointment, WaNotification, WaOptin

# ---------- Configuración (.env) ----------
ENABLED = os.getenv("WHATSAPP_ENABLED", "false").lower() == "true"
WA_NUMBER = re.sub(r"\D", "", os.getenv("WA_NUMBER", ""))
WA_SOCKET = os.getenv("WA_SOCKET", "/run/whatsapp/wa.sock")
WA_API_TOKEN = os.getenv("WA_API_TOKEN", "")
BUSINESS_NAME = os.getenv("WA_BUSINESS_NAME", "Men's Club")
TZ = ZoneInfo(os.getenv("WA_TIMEZONE", "America/Argentina/Buenos_Aires"))

REMINDER_BEFORE = timedelta(hours=2)
QUIET_UNTIL_HOUR = 8      # si el recordatorio cae antes de las 8...
NIGHT_BEFORE_HOUR = 20    # ...se manda a las 20 h del día anterior
MAX_ATTEMPTS = 8
LOCK_FILE = Path(__file__).resolve().parent.parent / "logs" / ".wa_dispatcher.lock"

# Sin 0/O, 1/I/L para que no se confundan
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"\b[A-Za-z0-9]{6}\b")
WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


# ---------- Utilidades ----------
def utcnow() -> datetime:
    """UTC sin zona, que es como lo guardamos en la base."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mask(jid: Optional[str]) -> Optional[str]:
    if not jid:
        return None
    user, _, server = jid.partition("@")
    masked = user[:4] + "*" * max(len(user) - 6, 3) + user[-2:] if len(user) > 6 else "***"
    return f"{masked}@{server}" if server else masked


def log(level: str, event_type: str, message: str, **metadata):
    getattr(sec_logger, level)(message, extra={"event_type": event_type, "metadata": metadata})


def generate_code() -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if sum(c.isdigit() for c in code) >= 2 and any(c.isalpha() for c in code):
            return code


def extract_code(text: str) -> Optional[str]:
    """Busca un código válido en el texto (6 caracteres del alfabeto, con al menos 2 números)."""
    for token in CODE_RE.findall(text or ""):
        t = token.upper()
        if all(c in CODE_ALPHABET for c in t) and sum(c.isdigit() for c in t) >= 2:
            return t
    return None


def appointment_start_utc(appt: Appointment) -> datetime:
    local = datetime.strptime(f"{appt.date} {appt.time[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def reminder_time_utc(appt: Appointment) -> datetime:
    start_local = datetime.strptime(f"{appt.date} {appt.time[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    r = start_local - REMINDER_BEFORE
    if r.hour < QUIET_UNTIL_HOUR:
        r = (start_local - timedelta(days=1)).replace(hour=NIGHT_BEFORE_HOUR, minute=0)
    return r.astimezone(timezone.utc).replace(tzinfo=None)


def wa_link_for(code: str) -> str:
    text = f"Hola! Quiero el recordatorio de mi turno. Código: {code}"
    return f"https://wa.me/{WA_NUMBER}?text={quote(text)}"


# ---------- Se llama desde create_appointment ----------
def create_optin_link(db, appointment: Appointment) -> Optional[str]:
    """Crea el código del turno y devuelve el link del botón (o None si WhatsApp está apagado)."""
    if not ENABLED or not WA_NUMBER:
        return None
    for _ in range(5):
        code = generate_code()
        if not db.query(WaOptin).filter(WaOptin.code == code).first():
            break
    db.add(WaOptin(appointment_id=appointment.id, code=code))
    db.commit()
    return wa_link_for(code)


# ---------- Textos ----------
def first_name(appt: Appointment) -> str:
    return (appt.client_name or "").strip().split(" ")[0][:30] or "hola"


def render(kind: str, appt: Optional[Appointment]) -> str:
    if kind == "autoreply":
        return (f"Hola! Este número solo envía confirmaciones y recordatorios de turnos de {BUSINESS_NAME}. "
                "Para consultas o cambios, comunicate directamente con la barbería.")
    if kind == "invalid_code":
        return "No encontré ese turno. Volvé a tocar el botón de WhatsApp que aparece al reservar en la página."
    start = datetime.strptime(f"{appt.date} {appt.time[:5]}", "%Y-%m-%d %H:%M")
    hora = start.strftime("%H:%M")
    barber = appt.employee_name
    if kind == "confirmation":
        fecha = f"{WEEKDAYS[start.weekday()]} {start.strftime('%d/%m')}"
        return (f"¡Hola {first_name(appt)}! Tu turno en {BUSINESS_NAME} quedó confirmado ✂️\n"
                f"📅 {fecha} a las {hora}\n"
                f"💈 {barber} · {appt.service}\n"
                "Te vamos a recordar 2 horas antes por acá.")
    # reminder
    reminder_local = datetime.now(TZ)
    dia = "hoy" if reminder_local.date() == start.date() else "mañana"
    return (f"Hola {first_name(appt)}, te recordamos tu turno de {dia} a las {hora} "
            f"en {BUSINESS_NAME} con {barber}. ¡Te esperamos! "
            "Si no podés venir, avisale a la barbería.")


# ---------- Cliente del cartero (HTTP sobre socket Unix) ----------
class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def wa_request(method: str, path: str, body: Optional[dict] = None, timeout: float = 90):
    """Devuelve (status_http, json). Si el cartero no responde, status 0."""
    conn = _UnixHTTPConnection(WA_SOCKET, timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {WA_API_TOKEN}", "Content-Type": "application/json"}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        try:
            return resp.status, json.loads(data or b"{}")
        except ValueError:
            return resp.status, {}
    except OSError as e:
        return 0, {"error": str(e)}
    finally:
        conn.close()


# ---------- Dispatcher ----------
class Dispatcher:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock_fd = None
        self._last_autoreply = {}   # jid -> momento (solo 1 por día)
        self._invalid = {}          # jid -> (día, intentos)
        self._was_down = False

    def start(self):
        if not ENABLED:
            return
        if not WA_API_TOKEN or not WA_NUMBER:
            log("error", "WA_CONFIG_ERROR", "WhatsApp activado pero falta WA_API_TOKEN o WA_NUMBER")
            return
        # Un solo dispatcher aunque Uvicorn corra varios procesos
        LOCK_FILE.parent.mkdir(exist_ok=True)
        self._lock_fd = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_fd.close()
            self._lock_fd = None
            return
        self._reset_stuck()
        self._thread = threading.Thread(target=self._run, name="wa-dispatcher", daemon=True)
        self._thread.start()
        log("info", "WA_DISPATCHER_START", "Dispatcher de WhatsApp iniciado")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._lock_fd:
            self._lock_fd.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.process_inbox()
                self.send_due()
            except Exception as e:  # el hilo nunca debe morir
                log("error", "WA_DISPATCHER_ERROR", "Error en el dispatcher", error=str(e)[:300])
            self._stop.wait(5)

    def _reset_stuck(self):
        db = SessionLocal()
        try:
            cutoff = utcnow() - timedelta(minutes=5)
            db.query(WaNotification).filter(
                WaNotification.status == "sending", WaNotification.updated_at < cutoff
            ).update({"status": "pending"})
            db.commit()
        finally:
            db.close()

    def _mark_service(self, up: bool):
        if up and self._was_down:
            log("info", "WA_SERVICE_REACHABLE", "El servicio de WhatsApp volvió a responder")
        if not up and not self._was_down:
            log("warning", "WA_SERVICE_UNREACHABLE", "No se puede hablar con el servicio de WhatsApp")
        self._was_down = not up

    # --- Mensajes entrantes ---
    def process_inbox(self):
        status, data = wa_request("GET", "/inbox", timeout=10)
        self._mark_service(status == 200)
        if status != 200:
            return
        items = data.get("items", [])
        if not items:
            return
        db = SessionLocal()
        done = []
        try:
            for item in items:
                self._handle_inbound(db, item)
                done.append(item["id"])
        finally:
            db.close()
            if done:
                wa_request("POST", "/inbox/ack", {"ids": done}, timeout=10)

    def _queue(self, db, kind: str, jid: str, appointment_id: Optional[int] = None, when: Optional[datetime] = None):
        db.add(WaNotification(appointment_id=appointment_id, kind=kind, to_jid=jid,
                              scheduled_at=when or utcnow(), status="pending"))

    def _handle_inbound(self, db, item: dict):
        jid = item.get("jid")
        code = extract_code(item.get("text", ""))
        now = utcnow()

        if not code:
            last = self._last_autoreply.get(jid)
            if not last or now - last > timedelta(hours=24):
                self._last_autoreply[jid] = now
                self._queue(db, "autoreply", jid)
                db.commit()
            return

        optin = db.query(WaOptin).filter(WaOptin.code == code).first()
        appt = db.query(Appointment).filter(Appointment.id == optin.appointment_id).first() if optin else None
        valid = appt is not None and appt.status == "confirmed" and appointment_start_utc(appt) > now

        if valid and optin.jid == jid:
            return  # ya estaba anotado (tocó el botón dos veces)

        if not valid or optin.jid is not None:
            reused = valid and optin.jid is not None
            day, count = self._invalid.get(jid, (now.date(), 0))
            count = count + 1 if day == now.date() else 1
            self._invalid[jid] = (now.date(), count)
            log("warning", "WA_OPTIN_CODE_REUSE" if reused else "WA_OPTIN_INVALID_CODE",
                "Código de turno ajeno o inválido", jid=mask(jid), attempts_today=count)
            if count <= 5:
                self._queue(db, "invalid_code", jid)
                db.commit()
            return

        optin.jid = jid
        optin.opted_in_at = now
        self._queue(db, "confirmation", jid, appt.id)
        reminder_at = reminder_time_utc(appt)
        if reminder_at > now + timedelta(minutes=5):
            self._queue(db, "reminder", jid, appt.id, reminder_at)
        db.commit()
        log("info", "WA_OPTIN", "Cliente pidió avisos por WhatsApp", appointment_id=appt.id, jid=mask(jid))

    # --- Envíos ---
    def send_due(self):
        db = SessionLocal()
        try:
            due = (db.query(WaNotification)
                   .filter(WaNotification.status == "pending", WaNotification.scheduled_at <= utcnow())
                   .order_by(WaNotification.scheduled_at)
                   .with_for_update(skip_locked=True)
                   .limit(3).all())
            for n in due:
                if self._stop.is_set():
                    break
                self._send_one(db, n)
        finally:
            db.close()

    def _send_one(self, db, n: WaNotification):
        now = utcnow()
        appt = None
        if n.appointment_id:
            appt = db.query(Appointment).filter(Appointment.id == n.appointment_id).first()
            if appt is None or appt.status != "confirmed" or appointment_start_utc(appt) <= now:
                n.status, n.last_error = "cancelled", "turno cancelado o pasado"
                db.commit()
                return
            if n.kind == "reminder":
                correct = reminder_time_utc(appt)
                if abs((correct - n.scheduled_at).total_seconds()) > 60 and correct > now:
                    n.scheduled_at = correct  # el turno se reprogramó
                    db.commit()
                    return

        text = render(n.kind, appt)
        n.status, n.attempts = "sending", (n.attempts or 0) + 1
        db.commit()

        status, data = wa_request("POST", "/send", {"to": n.to_jid, "text": text, "key": f"n{n.id}"})
        meta = {"notification_id": n.id, "kind": n.kind, "appointment_id": n.appointment_id, "jid": mask(n.to_jid)}

        if status == 200:
            n.status, n.sent_at, n.last_error = "sent", utcnow(), None
            db.commit()
            log("info", "WA_NOTIFICATION_SENT", "Aviso de WhatsApp enviado", **meta)
            return

        error = str(data.get("error", "sin respuesta"))[:200]
        permanent = status in (400, 403)
        if permanent or n.attempts >= MAX_ATTEMPTS:
            n.status, n.last_error = "failed", error
            db.commit()
            log("error", "WA_NOTIFICATION_FAILED", "No se pudo enviar el aviso", error=error, **meta)
            return
        wait = int(data.get("retry_after") or min(60 * 2 ** n.attempts, 1800))
        n.status, n.last_error = "pending", error
        n.scheduled_at = utcnow() + timedelta(seconds=wait)
        db.commit()
        log("warning", "WA_NOTIFICATION_RETRY", "Aviso reprogramado", error=error, retry_in_s=wait, **meta)


dispatcher = Dispatcher()
