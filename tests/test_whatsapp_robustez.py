"""Pruebas del Bloque 1 – robustez de WhatsApp (app/whatsapp.py). Datos 100 % de prueba."""
import http.client
from datetime import timedelta

import pytest

import app.whatsapp as wa
from app.models import Appointment, User, WaNotification, WaOptin

REAL_WA_REQUEST = wa.wa_request   # el original (al importar este archivo ningún fixture lo reemplazó)

JID_A = "1000000000001@lid"
JID_B = "1000000000002@lid"
JID_C = "1000000000003@lid"


# ---------- Ayudas ----------
def rows(Session, model, **filters):
    db = Session()
    try:
        return db.query(model).filter_by(**filters).order_by(*model.__table__.primary_key.columns).all()
    finally:
        db.close()


def count_calls(monkeypatch, dispatcher, fail_for=None):
    """Envuelve _handle_inbound: anota cada llamada y hace fallar los ids pedidos.
    fail_for = {id: cantidad de fallas} (None = nunca falla; float('inf') = falla siempre)."""
    calls = []
    fails_left = dict(fail_for or {})
    original = dispatcher._handle_inbound

    def wrapper(db, item):
        calls.append(item["id"])
        if fails_left.get(item["id"], 0) > 0:
            fails_left[item["id"]] -= 1
            raise RuntimeError("falla de prueba")
        return original(db, item)

    monkeypatch.setattr(dispatcher, "_handle_inbound", wrapper)
    return calls


def assert_sin_datos_privados(events, *secretos):
    for e in events:
        dump = repr(e)
        for s in secretos:
            assert s not in dump, f"{e['event_type']} contiene un dato privado"


def make_appointment(Session, jid, code="K2BF8T"):
    """Turno confirmado el viernes 20/11 a las 15:00 (hora local), ya anotado a WhatsApp desde jid."""
    db = Session()
    try:
        barber = User(name="Barbero", username="barbero", hashed_password="x")
        db.add(barber)
        db.flush()
        appt = Appointment(client_name="Cliente Prueba", client_phone="000", service="Corte",
                           date="2026-11-20", time="15:00:00", employee_id=barber.id, status="confirmed")
        db.add(appt)
        db.flush()
        db.add(WaOptin(appointment_id=appt.id, code=code, jid=jid))
        db.commit()
        return appt.id
    finally:
        db.close()


# ---------- Arreglo 1: _reset_stuck ----------
def test_reset_stuck_devuelve_sending_reciente_y_no_toca_otros(Session, clock, cartero, events, dispatcher):
    db = Session()
    ids = {}
    for name, status in [("s1", "sending"), ("s2", "sending"), ("pend", "pending"),
                         ("sent", "sent"), ("fail", "failed"), ("canc", "cancelled")]:
        n = WaNotification(kind="autoreply", to_jid=JID_A, status=status, attempts=1,
                           scheduled_at=clock.now - timedelta(minutes=2),
                           updated_at=clock.now - timedelta(minutes=1))  # menos de 5 min
        db.add(n)
        db.flush()
        ids[name] = n.id
    db.commit()
    db.close()

    dispatcher._reset_stuck()

    status = {n.id: (n.status, n.attempts) for n in rows(Session, WaNotification)}
    assert status[ids["s1"]] == ("pending", 1)
    assert status[ids["s2"]] == ("pending", 1)
    assert status[ids["pend"]] == ("pending", 1)
    assert status[ids["sent"]] == ("sent", 1)
    assert status[ids["fail"]] == ("failed", 1)
    assert status[ids["canc"]] == ("cancelled", 1)

    reset = [e for e in events if e["event_type"] == "WA_STUCK_RESET"]
    assert len(reset) == 1
    assert reset[0]["metadata"] == {"count": 2, "notification_ids": [ids["s1"], ids["s2"]]}
    assert_sin_datos_privados(events, JID_A)

    # El reintento manda la misma llave n{id}, así el cartero no lo repite
    dispatcher.send_due()
    keys = {s["key"] for s in cartero.sent}
    assert {f"n{ids['s1']}", f"n{ids['s2']}"} <= keys


def test_reset_stuck_sin_trabados_no_hace_nada(Session, clock, events, dispatcher):
    dispatcher._reset_stuck()
    assert [e for e in events if e["event_type"] == "WA_STUCK_RESET"] == []


# ---------- Arreglo 2: mensaje que falla siempre ----------
def test_mensaje_que_falla_siempre_se_reintenta_y_se_descarta(Session, clock, cartero, events, dispatcher, monkeypatch):
    cartero.add("m1", JID_A, "hola")
    cartero.add("m2", JID_B, "hola")
    cartero.add("m3", JID_C, "hola")
    calls = count_calls(monkeypatch, dispatcher, fail_for={"m1": float("inf")})

    # Vuelta 1: falla m1; los otros chats se procesan y se marcan leídos enseguida
    dispatcher.process_inbox()
    assert cartero.acks == [["m2", "m3"]]
    assert [i["id"] for i in cartero.inbox] == ["m1"]
    assert dispatcher._inbound_failures["m1"][0] == 1

    # Antes de 60 s no se reintenta
    clock.advance(59)
    dispatcher.process_inbox()
    assert calls.count("m1") == 1

    # A los 60 s: 2.º intento
    clock.advance(1)
    dispatcher.process_inbox()
    assert calls.count("m1") == 2
    assert [i["id"] for i in cartero.inbox] == ["m1"]

    # 60 s después: 3.er intento, se descarta y se marca leído
    clock.advance(60)
    dispatcher.process_inbox()
    assert calls.count("m1") == 3
    assert cartero.acks[-1] == ["m1"]
    assert cartero.inbox == []
    assert dispatcher._inbound_failures == {}

    retry = [e for e in events if e["event_type"] == "WA_INBOUND_RETRY"]
    failed = [e for e in events if e["event_type"] == "WA_INBOUND_FAILED"]
    assert [e["metadata"]["attempts"] for e in retry] == [1, 2]
    assert len(failed) == 1
    assert failed[0]["metadata"] == {"message_id": "m1", "error_type": "RuntimeError",
                                     "attempts": 3, "jid": wa.mask(JID_A)}
    # Nunca el texto del error, ni el jid completo
    assert_sin_datos_privados(events, JID_A, "falla de prueba")


def test_contador_de_fallos_se_limpia_si_el_mensaje_desaparece(Session, clock, cartero, dispatcher, monkeypatch):
    cartero.add("m1", JID_A, "hola")
    count_calls(monkeypatch, dispatcher, fail_for={"m1": float("inf")})
    dispatcher.process_inbox()
    assert "m1" in dispatcher._inbound_failures
    cartero.inbox = []           # el cartero lo perdió (por ejemplo, se reinició)
    dispatcher.process_inbox()
    assert dispatcher._inbound_failures == {}


# ---------- Orden por chat ----------
@pytest.mark.parametrize("fallas, vuelta_que_resuelve", [
    (1, 60),               # el anterior sale bien en el 2.º intento
    (float("inf"), 120),   # el anterior se descarta en el 3.er intento
])
def test_orden_por_chat(Session, clock, cartero, events, dispatcher, monkeypatch, fallas, vuelta_que_resuelve):
    cartero.add("m1", JID_A, "hola")
    cartero.add("m2", JID_A, "otro mensaje")   # mismo chat, va atrás
    cartero.add("m3", JID_B, "hola")
    calls = count_calls(monkeypatch, dispatcher, fail_for={"m1": fallas})

    t = 0
    while t <= 150:
        dispatcher.process_inbox()
        acked = {i for ack in cartero.acks for i in ack}
        if t < vuelta_que_resuelve:
            assert "m2" not in calls, "m2 se procesó antes que el mensaje anterior de su chat"
            assert "m2" not in acked, "m2 se marcó leído sin procesarse"
            assert "m2" not in dispatcher._inbound_failures, "esperar no cuenta como fallo"
        if t == 0:
            assert cartero.acks[0] == ["m3"]       # otro chat: no espera
        if t == vuelta_que_resuelve:
            assert cartero.acks[-1] == ["m1", "m2"]  # se resuelve m1 y m2 sale en la misma vuelta
        clock.advance(30)
        t += 30

    assert calls.count("m2") == 1
    assert calls[-1] == "m2"                       # m2 fue lo último que se procesó
    assert cartero.inbox == []
    # m2 no generó ningún evento de reintento
    assert all(e["metadata"].get("message_id") != "m2"
               for e in events if e["event_type"].startswith("WA_INBOUND"))


# ---------- Rollback + memoria ----------
def test_rollback_y_last_autoreply_vuelven_al_valor_anterior(Session, clock, cartero, dispatcher, monkeypatch):
    anterior = clock.now - timedelta(days=2)        # hace más de 24 h: corresponde mandar autoreply
    dispatcher._last_autoreply[JID_A] = anterior
    cartero.add("m1", JID_A, "hola")

    original_queue = dispatcher._queue
    state = {"fallar": True}

    def queue_que_falla(db, *args, **kwargs):
        original_queue(db, *args, **kwargs)
        db.flush()                                 # la fila llega a la base (a medias)
        if state.pop("fallar", False):
            raise RuntimeError("falla de prueba")

    monkeypatch.setattr(dispatcher, "_queue", queue_que_falla)

    dispatcher.process_inbox()
    assert rows(Session, WaNotification) == []                 # rollback: no quedó la fila a medias
    assert dispatcher._last_autoreply[JID_A] == anterior       # memoria restaurada

    clock.advance(60)
    dispatcher.process_inbox()
    autoreplies = rows(Session, WaNotification, to_jid=JID_A, kind="autoreply")
    assert len(autoreplies) == 1
    assert dispatcher._last_autoreply[JID_A] == clock.now
    assert cartero.inbox == []


def test_rollback_y_pending_cancel_vuelven_al_valor_anterior(Session, clock, cartero, dispatcher, monkeypatch):
    appt_id = make_appointment(Session, JID_A)
    db = Session()
    db.add(WaNotification(appointment_id=appt_id, kind="reminder", to_jid=JID_A, status="pending",
                          scheduled_at=clock.now + timedelta(days=5)))
    db.commit()
    db.close()

    cartero.add("m1", JID_A, "CANCELAR")
    dispatcher.process_inbox()
    assert [n.kind for n in rows(Session, WaNotification, kind="cancel_ask")] == ["cancel_ask"]
    pendiente = dispatcher._pending_cancel[JID_A]
    assert pendiente[0] == appt_id

    # El "SI" falla justo antes de terminar (después de marcar el turno y los avisos como cancelados)
    original_render = wa.render_cancel
    state = {"fallar": True}

    def render_que_falla(kind, appt=None):
        if kind == "cancel_done" and state.pop("fallar", False):
            raise RuntimeError("falla de prueba")
        return original_render(kind, appt)

    monkeypatch.setattr(wa, "render_cancel", render_que_falla)

    clock.advance(30)
    cartero.add("m2", JID_A, "SI")
    dispatcher.process_inbox()
    assert rows(Session, Appointment, id=appt_id)[0].status == "confirmed"         # rollback del turno
    assert rows(Session, WaNotification, kind="reminder")[0].status == "pending"   # rollback de los avisos
    assert rows(Session, WaNotification, kind="cancel_done") == []
    assert dispatcher._pending_cancel[JID_A] == pendiente                           # memoria restaurada

    # Reintento: cancela una sola vez y sin mensajes contradictorios
    clock.advance(60)
    dispatcher.process_inbox()
    assert rows(Session, Appointment, id=appt_id)[0].status == "cancelled"
    assert rows(Session, WaNotification, kind="reminder")[0].status == "cancelled"
    assert len(rows(Session, WaNotification, kind="cancel_done")) == 1
    assert rows(Session, WaNotification, kind="cancel_none") == []
    assert rows(Session, WaNotification, kind="autoreply") == []
    assert JID_A not in dispatcher._pending_cancel
    assert cartero.inbox == []


# ---------- Bloque 1b: ack que falla ----------
def test_ack_que_falla_se_reenvia_sin_reprocesar(Session, clock, cartero, dispatcher, monkeypatch):
    cartero.add("m1", JID_A, "hola")
    cartero.fail_acks = 1
    calls = count_calls(monkeypatch, dispatcher)

    dispatcher.process_inbox()
    assert calls == ["m1"]
    assert cartero.acks == [["m1"]]                 # se intentó, pero falló
    assert [i["id"] for i in cartero.inbox] == ["m1"]
    assert dispatcher._unacked == {"m1"}

    clock.advance(5)
    dispatcher.process_inbox()
    assert calls == ["m1"]                          # no se volvió a procesar
    assert cartero.acks[-1] == ["m1"]               # solo se mandó el ack
    assert cartero.inbox == []
    assert dispatcher._unacked == set()
    assert len(rows(Session, WaNotification, kind="autoreply")) == 1   # sin respuesta duplicada


def test_unacked_se_limpia_si_el_mensaje_desaparece(Session, clock, cartero, dispatcher, monkeypatch):
    cartero.add("m1", JID_A, "hola")
    cartero.fail_acks = 1
    count_calls(monkeypatch, dispatcher)
    dispatcher.process_inbox()
    assert dispatcher._unacked == {"m1"}
    cartero.inbox = []
    dispatcher.process_inbox()
    assert dispatcher._unacked == set()


# ---------- wa_request ante respuestas rotas ----------
class _ConexionRota:
    """Reemplaza la conexión por socket Unix: no abre nada y falla al leer la respuesta."""
    error = None

    def __init__(self, path, timeout):
        pass

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        raise self.error

    def close(self):
        pass


@pytest.mark.parametrize("error", [
    http.client.BadStatusLine("basura"),
    http.client.IncompleteRead(b"{\"ite"),
    http.client.RemoteDisconnected("cortó"),
])
def test_wa_request_devuelve_0_ante_httpexception(monkeypatch, error):
    monkeypatch.setattr(_ConexionRota, "error", error)
    monkeypatch.setattr(wa, "_UnixHTTPConnection", _ConexionRota)
    status, data = REAL_WA_REQUEST("POST", "/send", {"to": JID_A, "text": "x", "key": "n1"})
    assert status == 0
    assert "error" in data


def test_send_one_con_respuesta_rota_no_queda_en_sending(Session, clock, monkeypatch):
    monkeypatch.setattr(_ConexionRota, "error", http.client.BadStatusLine("basura"))
    monkeypatch.setattr(wa, "_UnixHTTPConnection", _ConexionRota)
    monkeypatch.setattr(wa, "wa_request", REAL_WA_REQUEST)
    db = Session()
    db.add(WaNotification(kind="autoreply", to_jid=JID_A, status="pending", scheduled_at=clock.now))
    db.commit()
    db.close()

    wa.Dispatcher().send_due()

    n = rows(Session, WaNotification)[0]
    assert n.status == "pending"          # vuelve a la cola con espera, no queda trabado en "sending"
    assert n.attempts == 1
    assert n.scheduled_at > clock.now
