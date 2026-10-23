"""Escucha el bot de Telegram todo el rato y es el ÚNICO que lee sus mensajes.

Antes, el agente leía tus respuestas solo al arrancar (una vez por minuto, y nunca durante una revisión): el botón
se quedaba "cargando" hasta un par de minutos y acababas pulsándolo varias veces. Ahora este proceso:
- responde al instante cuando pulsas un botón ("Recibido…") y quita los botones del mensaje;
- deja tu respuesta en data/respuestas_pendientes.json, que el agente aplica en menos de un minuto;
- guarda tus mensajes de texto en data/mensajes.json (código SMS, datos que la web pide), que el agente consulta.
Un solo lector evita que dos procesos se roben las respuestas (Telegram entrega cada una a quien la pide primero).

Uso: .venv/bin/python -m cita_previa.escucha   (lo arranca systemd o launchd y lo mantiene vivo)
"""
import fcntl
import json
import time
from datetime import datetime
from pathlib import Path

import requests

from cita_previa.config import DATA_DIR, env

OFFSET = DATA_DIR / "tg_offset"
PENDIENTES = DATA_DIR / "respuestas_pendientes.json"
MENSAJES = DATA_DIR / "mensajes.json"
LATIDO = DATA_DIR / "escucha_latido"      # si se tocó hace menos de 2 min, el escuchador está vivo
VIVO_S = 120


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _con_candado(path: Path, cambiar):
    """Lee, cambia y guarda una lista JSON con candado: el escuchador y el agente la tocan a la vez."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        try:
            datos = json.loads(path.read_text())
        except (OSError, ValueError):
            datos = []
        nuevos, resultado = cambiar(datos)
        path.write_text(json.dumps(nuevos, ensure_ascii=False))
        return resultado


def anotar(path: Path, item: dict, maximo: int = 50) -> None:
    _con_candado(path, lambda d: ((d + [item])[-maximo:], None))


def sacar_todo(path: Path) -> list:
    return _con_candado(path, lambda d: ([], d))


def activo() -> bool:
    return LATIDO.exists() and time.time() - LATIDO.stat().st_mtime < VIVO_S


def atender(api: str, chat_id: str, u: dict) -> None:
    """Un update de Telegram: botón de "¿es un bloqueo?" o mensaje de texto. Solo de tu chat."""
    q, m = u.get("callback_query"), u.get("message")
    if q:
        if str((q.get("from") or {}).get("id")) != str(chat_id):
            return
        partes, msg = q.get("data", "").split(":", 2), q.get("message") or {}
        texto = None
        if len(partes) == 3 and partes[0] in ("bloq", "err"):
            si = partes[1] == "si"
            anotar(PENDIENTES, {"tipo": partes[0], "si": si, "t": partes[2], "chat": (msg.get("chat") or {}).get("id"),
                                "mid": msg.get("message_id"), "caption": msg.get("caption") or ""})
            texto = "Recibido: lo cuento como bloqueo" if si else "Recibido: no lo cuento como bloqueo"
            log(f"Botón: {partes[0]} {'sí' if si else 'no'} ({partes[2]})")
        try:
            requests.post(f"{api}/answerCallbackQuery", timeout=20,
                          json={"callback_query_id": q["id"], **({"text": texto} if texto else {})})
            if texto and msg.get("message_id"):      # sin botones: no se puede pulsar dos veces
                requests.post(f"{api}/editMessageReplyMarkup", timeout=20, json={
                    "chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "reply_markup": {"inline_keyboard": []}})
        except requests.RequestException:
            pass
    elif m and str((m.get("chat") or {}).get("id")) == str(chat_id) and m.get("text"):
        anotar(MENSAJES, {"fecha": m.get("date", int(time.time())), "texto": m["text"].strip()})


def main() -> None:
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en .env: no hay nada que escuchar.")
        time.sleep(300)                          # sin bucle rápido de reinicios
        return
    api = f"https://api.telegram.org/bot{token}"
    log("Escuchando el bot de Telegram.")
    while True:
        LATIDO.touch()
        try:
            offset = int(OFFSET.read_text())
        except (OSError, ValueError):
            offset = None
        try:
            r = requests.get(f"{api}/getUpdates", timeout=60, params={
                "timeout": 50, **({"offset": offset} if offset else {}),
                "allowed_updates": json.dumps(["message", "callback_query"])}).json()
        except (requests.RequestException, ValueError):
            time.sleep(5)
            continue
        if not r.get("ok"):          # p. ej. 409: otro proceso (¿otra máquina?) lee el mismo bot
            log(f"Telegram: {r.get('description', '?')}")
            time.sleep(15)
            continue
        for u in r.get("result", []):
            atender(api, chat_id, u)
            OFFSET.write_text(str(u["update_id"] + 1))


if __name__ == "__main__":
    main()
