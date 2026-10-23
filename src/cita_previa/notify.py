"""Avisos por el bot de Telegram del agente, con notificación de macOS y sonido como respaldo."""
import json
import platform
import subprocess
import time
from pathlib import Path

import requests

from cita_previa.config import env

TELEGRAM_LIMIT = 4000   # Telegram corta en 4096 caracteres
ultimo_error = ""       # motivo del último envío fallido, en claro (sin la URL: lleva el token)


def _motivo(e: Exception | None = None, r=None) -> str:
    if r is not None:
        if r.status_code == 429:
            return "Telegram frenó los envíos por exceso de mensajes"
        return f"{'falla en los servidores de' if r.status_code >= 500 else 'rechazo de'} Telegram (HTTP {r.status_code})"
    if isinstance(e, requests.Timeout):
        return "Telegram tardó demasiado en responder"
    if isinstance(e, requests.ConnectionError):
        return "no había conexión con Telegram"
    return type(e).__name__ if e else "motivo desconocido"


def _enviar(metodo: str, **kwargs) -> bool:
    """Una llamada a la API con un reintento a los 3 s (red o Telegram caídos un momento)."""
    global ultimo_error
    token = env("TELEGRAM_BOT_TOKEN")
    for intento in range(2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/{metodo}", timeout=60, **kwargs)
            if r.ok:
                return True
            ultimo_error = _motivo(r=r)
            if r.status_code < 500 and r.status_code != 429:
                return False                      # rechazo definitivo: reintentar no cambia nada
        except requests.RequestException as e:
            ultimo_error = _motivo(e)
        if not intento:
            time.sleep(3)
    return False


def _teclado(botones) -> str | None:
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in botones]]}) if botones else None


def telegram(text: str, silent: bool = False) -> bool:
    """Envía por el bot del agente. silent=True: llega sin sonido (avisos rutinarios)."""
    if not env("TELEGRAM_BOT_TOKEN") or not env("TELEGRAM_CHAT_ID"):
        return False
    return all(_enviar("sendMessage", json={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text[i:i + TELEGRAM_LIMIT],
                                             "disable_web_page_preview": True, "disable_notification": silent})
               for i in range(0, len(text), TELEGRAM_LIMIT))


def telegram_foto(path, caption: str = "", silent: bool = False, botones=None) -> bool:
    """Manda una captura por el bot, con botones opcionales [(texto, dato)]. Si Telegram no la acepta como foto
    (muy alta o pesada), va como archivo."""
    if not env("TELEGRAM_BOT_TOKEN") or not env("TELEGRAM_CHAT_ID") or not path or not Path(path).exists():
        return False
    datos = {"chat_id": env("TELEGRAM_CHAT_ID"), "caption": caption[:1000], "disable_notification": silent}
    if teclado := _teclado(botones):
        datos["reply_markup"] = teclado
    for metodo, campo in (("sendPhoto", "photo"), ("sendDocument", "document")):
        with open(path, "rb") as f:
            if _enviar(metodo, data=datos, files={campo: (Path(path).name, f, "image/png")}):
                return True
    return False


def macos(title: str, message: str) -> None:
    if platform.system() != "Darwin":
        return
    message = message.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                   check=False, capture_output=True, timeout=10)


def sound() -> None:
    if platform.system() == "Darwin":
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)


def ocultar_app(nombre: str) -> None:
    """Oculta la app (como Cmd+H). La primera vez macOS pide permiso para controlar "System Events"."""
    if platform.system() == "Darwin":
        subprocess.run(["osascript", "-e", f'tell application "System Events" to set visible of '
                        f'(every process whose name is "{nombre}") to false'],
                       check=False, capture_output=True, timeout=15)
