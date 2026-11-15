"""Pruebas del Bloque 2 – textos del bot de WhatsApp (app/whatsapp.py). Datos 100 % de prueba.

Reloj de las pruebas: sáb 14/11/2026 12:00 UTC = 09:00 en Buenos Aires.
"""
from datetime import timedelta

import pytest

import app.whatsapp as wa
from app.models import Appointment, User, WaNotification, WaOptin

JID_A = "1000000000001@lid"
JID_B = "1000000000002@lid"

PAST = "Ese turno ya pasó. Si querés uno nuevo, reservá en la página."
CANCELLED = "Ese turno fue cancelado. Si querés uno nuevo, reservá en la página."
USED = "Ese código ya se usó desde otro número. Los avisos de ese turno le llegan a ese número."
UNKNOWN = ("No encontré ese código. Revisá que esté bien escrito o volvé a tocar "
           "el botón de WhatsApp en la página de reservas.")


# ---------- Ayudas ----------
def rows(Session, model, **filters):
    db = Session()
    try:
        return db.query(model).filter_by(**filters).order_by(*model.__table__.primary_key.columns).all()
    finally:
        db.close()


def make_appt(Session, code="K2BF8T", jid=None, date="2026-11-20", time="15:00:00", status="confirmed"):
    """Turno de prueba con su código. jid=None: todavía nadie mandó el código."""
    db = Session()
    try:
        barber = db.query(User).filter_by(username="barbero").first()
        if barber is None:
            barber = User(name="Barbero", username="barbero", hashed_password="x")
            db.add(barber)
            db.flush()
        appt = Appointment(client_name="Cliente Prueba", client_phone="000", service="Corte",
                           date=date, time=time, employee_id=barber.id, status=status)
        db.add(appt)
        db.flush()
        db.add(WaOptin(appointment_id=appt.id, code=code, jid=jid))
        db.commit()
        return appt.id
    finally:
        db.close()


class Chat:
    """Manda mensajes al dispatcher y devuelve los textos que el bot le contestó a cada chat."""

    def __init__(self, cartero, dispatcher):
        self.cartero, self.dispatcher, self.n = cartero, dispatcher, 0

    def send(self, jid, text):
        self.n += 1
        self.cartero.add(f"m{self.n}", jid, text)
        self.dispatcher.process_inbox()

    def replies(self, jid):
        for _ in range(20):                       # send_due manda de a 3
            before = len(self.cartero.sent)
            self.dispatcher.send_due()
            if len(self.cartero.sent) == before:
                break
        return [s["text"] for s in self.cartero.sent if s["to"] == jid]


@pytest.fixture
def chat(cartero, dispatcher):
    return Chat(cartero, dispatcher)


def code_msg(code):
    return f"Hola! Quiero el recordatorio de mi turno. Código: {code}"


# ---------- B1 a B4: códigos que no sirven ----------
def test_b1_turno_que_ya_paso(Session, clock, chat):
    make_appt(Session, date="2026-11-13")
    chat.send(JID_A, code_msg("K2BF8T"))
    assert chat.replies(JID_A) == [PAST]


def test_b2_turno_cancelado(Session, clock, chat):
    make_appt(Session, status="cancelled")
    chat.send(JID_A, code_msg("K2BF8T"))
    assert chat.replies(JID_A) == [CANCELLED]


def test_b2_gana_si_esta_cancelado_y_ya_paso(Session, clock, chat):
    make_appt(Session, date="2026-11-13", status="cancelled")
    chat.send(JID_A, code_msg("K2BF8T"))
    assert chat.replies(JID_A) == [CANCELLED]


def test_b3_codigo_usado_desde_otro_chat(Session, clock, chat, events):
    appt_id = make_appt(Session, jid=JID_A)
    chat.send(JID_B, code_msg("K2BF8T"))
    assert chat.replies(JID_B) == [USED]
    assert chat.replies(JID_A) == []
    assert rows(Session, WaOptin, appointment_id=appt_id)[0].jid == JID_A   # no se lo robó
    assert [e["event_type"] for e in events if e["event_type"].startswith("WA_OPTIN")] == ["WA_OPTIN_CODE_REUSE"]


def test_b4_codigo_inexistente(Session, clock, chat):
    chat.send(JID_A, code_msg("ZZ99ZZ"))
    assert chat.replies(JID_A) == [UNKNOWN]


def test_b1_a_b4_comparten_el_limite_de_5_por_dia(Session, clock, chat):
    make_appt(Session, code="PAS234", date="2026-11-13")
    make_appt(Session, code="CAN234", status="cancelled")
    make_appt(Session, code="USA234", jid=JID_A)
    for code in ["PAS234", "CAN234", "USA234", "ZZ99ZZ", "PAS234", "CAN234"]:   # 6 en el mismo día
        chat.send(JID_B, code_msg(code))
    assert chat.replies(JID_B) == [PAST, CANCELLED, USED, UNKNOWN, PAST]        # el 6.º no se contesta

    clock.advance(24 * 3600)                                                     # otro día: vuelve a contestar
    chat.send(JID_B, code_msg("ZZ99ZZ"))
    assert chat.replies(JID_B)[-1] == UNKNOWN
    assert len(rows(Session, WaNotification, to_jid=JID_B, kind="invalid_code")) == 6


@pytest.mark.parametrize("token", ["K0B28T", "KOB28T", "K1B28T", "KIB28T", "KLB28T", "K2B8T", "K2BF88T"])
def test_codigos_no_reconocidos_siguen_con_autoreply(Session, clock, chat, token):
    chat.send(JID_A, code_msg(token))
    assert [n.kind for n in rows(Session, WaNotification, to_jid=JID_A)] == ["autoreply"]


def test_fila_vieja_de_invalid_code_sin_body_usa_el_texto_nuevo(Session, clock, chat):
    # Avisos que hayan quedado en la cola antes de actualizar (sin body)
    db = Session()
    db.add(WaNotification(kind="invalid_code", to_jid=JID_A, status="pending", scheduled_at=clock.now))
    db.commit()
    db.close()
    assert chat.replies(JID_A) == [UNKNOWN]


# ---------- B5: mismo código desde el mismo chat ----------
ALREADY = "Tu turno ya está confirmado ✅ vie 20/11 a las 15:00 con Barbero."


def test_b5_mismo_codigo_mismo_chat_maximo_1_cada_10_min(Session, clock, chat):
    make_appt(Session, jid=JID_A)
    chat.send(JID_A, code_msg("K2BF8T"))
    assert chat.replies(JID_A) == [ALREADY]

    clock.advance(9 * 60 + 59)                    # dentro de los 10 min: silencio
    chat.send(JID_A, code_msg("K2BF8T"))
    assert chat.replies(JID_A) == [ALREADY]

    clock.advance(1)                              # a los 10 min: contesta otra vez
    chat.send(JID_A, code_msg("K2BF8T"))
    assert chat.replies(JID_A) == [ALREADY, ALREADY]


def test_b5_despues_de_anotarse(Session, clock, chat):
    make_appt(Session)
    chat.send(JID_A, code_msg("K2BF8T"))          # se anota: confirmación
    clock.advance(60)
    chat.send(JID_A, code_msg("K2BF8T"))          # lo manda de nuevo
    textos = chat.replies(JID_A)
    assert len(textos) == 2
    assert "quedó confirmado" in textos[0]
    assert textos[1] == ALREADY


@pytest.mark.parametrize("anterior_min", [None, 20])   # sin entrada previa / entrada vieja (hace 20 min)
def test_b5_memoria_se_restaura_si_el_mensaje_falla(Session, clock, cartero, dispatcher, monkeypatch, anterior_min):
    make_appt(Session, jid=JID_A)
    anterior = None if anterior_min is None else clock.now - timedelta(minutes=anterior_min)
    if anterior:
        dispatcher._last_confirmed[JID_A] = anterior

    original_queue = dispatcher._queue
    state = {"fallar": True}

    def queue_que_falla(db, *args, **kwargs):
        original_queue(db, *args, **kwargs)
        db.flush()
        if state.pop("fallar", False):
            raise RuntimeError("falla de prueba")

    monkeypatch.setattr(dispatcher, "_queue", queue_que_falla)

    cartero.add("m1", JID_A, code_msg("K2BF8T"))
    dispatcher.process_inbox()
    assert rows(Session, WaNotification) == []                       # rollback
    if anterior:
        assert dispatcher._last_confirmed[JID_A] == anterior          # memoria restaurada
    else:
        assert JID_A not in dispatcher._last_confirmed                 # no quedó marcada

    clock.advance(60)                                                  # reintento: contesta una sola vez
    dispatcher.process_inbox()
    assert len(rows(Session, WaNotification, kind="already_confirmed")) == 1
    assert dispatcher._last_confirmed[JID_A] == clock.now
    assert cartero.inbox == []


# ---------- B6: CANCELAR repetido ----------
def test_b6_cancelar_repetido_vuelve_a_preguntar_y_el_plazo_arranca_de_nuevo(Session, clock, chat, dispatcher):
    appt_id = make_appt(Session, jid=JID_A)
    chat.send(JID_A, "CANCELAR")
    clock.advance(5 * 60)
    chat.send(JID_A, "cancelar")
    assert len(rows(Session, WaNotification, kind="cancel_ask")) == 2
    assert dispatcher._pending_cancel[JID_A] == (appt_id, clock.now + wa.CANCEL_CONFIRM_WINDOW)

    clock.advance(14 * 60)                        # 19 min después del primero, 14 del segundo
    chat.send(JID_A, "SI")
    assert rows(Session, Appointment, id=appt_id)[0].status == "cancelled"
    assert rows(Session, WaNotification, kind="cancel_expired") == []
    assert len(rows(Session, WaNotification, kind="cancel_done")) == 1


@pytest.mark.parametrize("con_turno_cercano, kind", [(False, "cancel_none"), (True, "cancel_late")])
def test_b6_cancelar_repetido_sin_nada_esperando_contesta_normal(Session, clock, chat, con_turno_cercano, kind):
    if con_turno_cercano:
        make_appt(Session, jid=JID_A, date="2026-11-14", time="09:30:00")   # en 30 min
    chat.send(JID_A, "CANCELAR")
    clock.advance(60)
    chat.send(JID_A, "CANCELAR")
    assert [n.kind for n in rows(Session, WaNotification, to_jid=JID_A)] == [kind, kind]


# ---------- Extra: palabras para cancelar ----------
@pytest.mark.parametrize("text, expected", [
    ("CANCELAME", True),
    ("cancelame el turno", True),
    ("ANULAR", True),
    ("anular turno", True),
    ("Anulo", True),
    ("quiero anular mi turno", True),
    ("anulá el turno", True),
    ("anulame el turno", True),
    ("Anulámelo ya", False),                  # ANULAMELO no está en la lista
    ("hola quiero anular", True),             # 3.ª posición
    ("hola buenas tardes anular", False),     # 4.ª posición
    ("no quiero anular", False),
    ("anular no", False),
    ("anulo el turno del viernes por favor", False),   # 7 palabras
])
def test_extra_palabras_para_cancelar(text, expected):
    assert wa.is_cancel_command(wa.words_of(text)) is expected


def test_extra_anulo_pide_confirmacion(Session, clock, chat):
    make_appt(Session, jid=JID_A)
    chat.send(JID_A, "Anulo")
    assert [n.kind for n in rows(Session, WaNotification, to_jid=JID_A)] == ["cancel_ask"]


# ---------- C: final de la confirmación ----------
HEAD = ["¡Hola Cliente! Tu turno en Barbería de prueba quedó confirmado ✂️"]


@pytest.mark.parametrize("date, time, tail", [
    ("2026-11-20", "15:00:00", ["Te vamos a recordar 2 horas antes por acá.",
                                "Si no podés venir, respondé CANCELAR."]),
    ("2026-11-20", "09:00:00", ["Te vamos a recordar la noche anterior por acá.",
                                "Si no podés venir, respondé CANCELAR."]),
    ("2026-11-14", "10:30:00", ["Si no podés venir, respondé CANCELAR."]),       # sin recordatorio
    ("2026-11-14", "09:45:00", ["Si no podés venir, avisale a la barbería."]),   # faltan menos de 1 h
], ids=["2h", "noche_anterior", "sin_recordatorio", "menos_de_1h"])
def test_c_final_de_la_confirmacion(Session, clock, chat, date, time, tail):
    make_appt(Session, date=date, time=time)
    chat.send(JID_A, code_msg("K2BF8T"))
    lines = chat.replies(JID_A)[0].split("\n")
    assert lines[:1] == HEAD
    assert lines[3:] == tail


def test_c_si_el_recordatorio_ya_no_esta_en_la_cola_no_se_promete(Session, clock, cartero, dispatcher):
    appt_id = make_appt(Session)
    cartero.add("m1", JID_A, code_msg("K2BF8T"))
    dispatcher.process_inbox()
    db = Session()
    db.query(WaNotification).filter_by(appointment_id=appt_id, kind="reminder").update({"status": "cancelled"})
    db.commit()
    db.close()
    dispatcher.send_due()
    assert cartero.sent[0]["text"].split("\n")[3:] == ["Si no podés venir, respondé CANCELAR."]


def test_c_con_cancelacion_apagada(Session, clock, chat, monkeypatch):
    monkeypatch.setattr(wa, "CANCEL_ENABLED", False)
    make_appt(Session)
    chat.send(JID_A, code_msg("K2BF8T"))
    assert chat.replies(JID_A)[0].split("\n")[3:] == ["Te vamos a recordar 2 horas antes por acá.",
                                                      "Si no podés venir, avisale a la barbería."]


# ---------- Opcional: límite de CANCELAR y nombre con mayúscula ----------
def test_opcional_cancelar_tiene_limite_diario(Session, clock, chat, events):
    for _ in range(12):
        chat.send(JID_A, "CANCELAR")
        clock.advance(60)
    assert len(rows(Session, WaNotification, to_jid=JID_A, kind="cancel_none")) == 10
    assert len([e for e in events if e["event_type"] == "WA_CANCEL_RATE_LIMITED"]) == 1
    clock.advance(24 * 3600)
    chat.send(JID_A, "CANCELAR")
    assert len(rows(Session, WaNotification, to_jid=JID_A, kind="cancel_none")) == 11


@pytest.mark.parametrize("nombre, saludo", [("prueba", "Prueba"), ("JUAN", "JUAN"), ("maría josé", "María"),
                                            ("McDonald", "McDonald")])
def test_opcional_nombre_con_mayuscula(nombre, saludo):
    assert wa.first_name(Appointment(client_name=nombre)) == saludo
