"""Ninguna prueba debe escribir a tu Telegram ni tocar data/ de verdad: sin token ni chat (las que los necesitan
los ponen falsos) y los campos aprendidos en una carpeta temporal."""
import pytest

from cita_previa import escucha, run


@pytest.fixture(autouse=True)
def sin_telegram_real(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(run, "CAMPOS_APRENDIDOS", tmp_path / "campos_aprendidos.json")
    for nombre in ("OFFSET", "PENDIENTES", "MENSAJES", "LATIDO"):        # escuchador: apagado y en tmp
        monkeypatch.setattr(escucha, nombre, tmp_path / f"escucha_{nombre.lower()}")
