"""El escuchador de Telegram: responde al instante a los botones, los quita y deja la respuesta en cola."""
import time

from cita_previa import escucha, run
from cita_previa.aprendizaje import Memoria


def boton(update_id, data, quien=42):
    return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "from": {"id": quien}, "data": data,
                                                         "message": {"chat": {"id": 42}, "message_id": 7, "caption": "⛔ x"}}}


def test_responde_al_instante_y_quita_los_botones(monkeypatch):
    llamadas = []
    monkeypatch.setattr(escucha.requests, "post", lambda url, **k: llamadas.append((url.rsplit("/", 1)[1], k["json"])))
    escucha.atender("api", "42", boton(1, "bloq:si:2026-10-22T09:52:28"))
    escucha.atender("api", "42", boton(2, "bloq:no:2026-10-22T09:52:28", quien=999))     # otro chat: nada
    assert [m for m, _ in llamadas] == ["answerCallbackQuery", "editMessageReplyMarkup"]
    assert "Recibido" in llamadas[0][1]["text"] and llamadas[1][1]["reply_markup"] == {"inline_keyboard": []}
    assert escucha.sacar_todo(escucha.PENDIENTES)[0]["si"] is True
    assert escucha.sacar_todo(escucha.PENDIENTES) == []                                # sacar vacía la cola


def test_el_agente_aplica_la_cola_una_vez_por_aviso(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(run, "PAUSE", tmp_path / "pausa_hasta")
    monkeypatch.setattr(escucha.requests, "post", lambda *a, **k: None)
    monkeypatch.setattr(run.requests, "post", lambda *a, **k: None)
    monkeypatch.setattr(run.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe leer Telegram")))
    escucha.LATIDO.touch()                                   # escuchador vivo
    m = Memoria(tmp_path / "aprendizaje.json", {"pausa_bloqueo_min": 15})
    t = m.registrar("bloqueo")
    for i in range(4):                                       # pulsó "Sí" cuatro veces
        escucha.atender("api", "42", boton(i, f"bloq:si:{t}"))
    run.leer_respuestas(m)
    assert m.intervalo() == 375 and m.piso() == 375          # aplicado una sola vez (×1,25, no ×1,25⁴)
    assert escucha.sacar_todo(escucha.PENDIENTES) == []


def test_codigo_sms_desde_la_cola(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    escucha.LATIDO.touch()
    escucha.anotar(escucha.MENSAJES, {"fecha": int(time.time()) - 600, "texto": "111111"})   # viejo: se descarta
    monkeypatch.setattr(run.time, "sleep", lambda s: escucha.anotar(
        escucha.MENSAJES, {"fecha": int(time.time()), "texto": "654321"}))
    assert run.esperar_codigo_telegram(1) == "654321"
