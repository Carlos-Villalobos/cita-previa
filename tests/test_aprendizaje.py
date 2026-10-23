"""Memoria del agente: ritmo aprendido, bloqueos confirmados por Telegram y apagado programado."""
from datetime import datetime, timedelta

from cita_previa import run
from cita_previa.aprendizaje import Memoria


def memoria(tmp_path, **cfg):
    return Memoria(tmp_path / "aprendizaje.json", {"pausa_bloqueo_min": 15, **cfg})


def test_empieza_en_5_min_y_sin_errores_mantiene(tmp_path):
    m = memoria(tmp_path)
    assert m.intervalo() == 300
    for _ in range(20):
        m.registrar("sin_citas")
    assert m.intervalo() == 300                      # sin errores, no cambia


def test_con_baja_s_acelera_hasta_el_minimo(tmp_path):
    m = memoria(tmp_path, ritmo={"inicial_s": 600, "baja_s": 30})
    m.registrar("sin_citas")
    assert m.intervalo() == 570
    for _ in range(50):
        m.registrar("sin_citas")
    assert m.intervalo() == 300                      # nunca por debajo de 5 min


def test_errores_y_bloqueos_no_cambian_el_ritmo_solos(tmp_path):
    m = memoria(tmp_path)
    m.registrar("error", "datos")
    m.registrar("bloqueo", frase="requested url was rejected")
    m.registrar("sin_citas")
    assert m.intervalo() == 300 and m.piso() == 0     # sin tu respuesta en Telegram, nada cambia


def test_solo_tu_respuesta_sube_el_ritmo(tmp_path):
    m = memoria(tmp_path)
    t = m.registrar("bloqueo", frase="requested url was rejected")
    assert m.responder_bloqueo(t, False).startswith("Anotado")
    assert m.intervalo() == 300                      # "No es bloqueo": igual que antes
    t = m.registrar("bloqueo")
    assert m.responder_bloqueo(t, True).startswith("Confirmado")
    assert m.intervalo() == 375 and m.piso() == 375   # "Sí es bloqueo": ×1,25 y piso 7 días
    for _ in range(10):
        m.registrar("sin_citas")
    assert m.intervalo() == 375
    t = m.registrar("error", "datos")
    assert m.error_fue_bloqueo(t).startswith("Aprendido")
    assert m.intervalo() == 468                      # "Fue bloqueo" en un error: también sube


def test_falsa_alarma_quita_el_bloqueo(tmp_path):
    m = memoria(tmp_path)
    t = m.registrar("bloqueo")
    assert m.responder_bloqueo(t, False).startswith("Anotado")
    assert m.d["bloqueos"] == [] and m.d["revisiones"][-1]["r"] == "falsa_alarma"


def test_pausa_doble_si_bloquea_al_volver(tmp_path):
    m = memoria(tmp_path)
    m.registrar("bloqueo")
    assert m.pausa_min() == 15
    m.registrar("bloqueo")
    assert m.pausa_min() == 30
    m.registrar("sin_citas")
    assert m.pausa_min() == 15                       # la pausa funcionó: vuelve a la base


def test_error_que_fue_bloqueo_y_tope_por_hora(tmp_path):
    m = memoria(tmp_path)
    for _ in range(4):
        m.registrar("sin_citas")
    t = m.registrar("error", "datos")
    assert m.error_fue_bloqueo(t).startswith("Aprendido")
    assert m.tope_por_hora() == 4                    # llevaba 5 en la hora: no pasar de 4
    assert m.programar_siguiente() >= datetime.now() + timedelta(seconds=0.8 * 900)


def test_horas_con_citas_revisan_al_doble(tmp_path):
    m = memoria(tmp_path)
    m.d["intervalo_s"] = 1200
    m.registrar("hay_citas")
    assert datetime.now().hour in m.horas_con_citas()
    assert m.programar_siguiente() <= datetime.now() + timedelta(seconds=1.2 * 600)


def test_toca_y_guardar(tmp_path):
    m = memoria(tmp_path)
    assert m.toca()
    m.programar(datetime.now() + timedelta(minutes=5))
    m.guardar()
    assert not Memoria(tmp_path / "aprendizaje.json").toca()


def test_apagado_programado_no_avisa():
    cfg = {"apagado": "01:00-07:00"}
    d = datetime(2026, 10, 20)
    assert run.apagado_programado(cfg, d.replace(hour=0, minute=58), d.replace(hour=7, minute=4))
    assert not run.apagado_programado(cfg, d.replace(hour=3), d.replace(hour=7, minute=4))   # se cayó a las 3
    assert not run.apagado_programado({}, d.replace(hour=0, minute=58), d.replace(hour=7, minute=4))


def test_botones_de_telegram(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(run, "PAUSE", tmp_path / "pausa_hasta")
    m = memoria(tmp_path)
    t = m.registrar("bloqueo")
    run.PAUSE.write_text("2099-01-01T00:00")
    respuesta = {"result": [
        {"update_id": 7, "callback_query": {"id": "a", "from": {"id": 999}, "data": f"bloq:si:{t}"}},   # otro chat
        {"update_id": 8, "callback_query": {"id": "b", "from": {"id": 42}, "data": f"bloq:no:{t}",
                                            "message": {"chat": {"id": 42}, "message_id": 1, "caption": "⛔ x"}}}]}

    class R:
        def json(self):
            return respuesta
    monkeypatch.setattr(run.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(run.requests, "post", lambda *a, **k: R())
    run.leer_respuestas(m)
    assert m.offset_telegram == 9 and m.d["bloqueos"] == [] and not run.PAUSE.exists()
