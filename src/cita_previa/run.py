"""Busca y reserva la cita de toma de huellas (TIE) en la cita previa de extranjería.

Repite el recorrido de la web (provincia → trámite → "Presentación sin Cl@ve" con tu NIE, nombre y país) y, si
hay citas, reserva la primera con los datos de config/cita.yaml (con `con_clave: true` entra con Cl@ve y tu
certificado). Al reservar, la web manda un SMS a tu móvil: el agente te lo pide
por Telegram y confirma con el código. Tras reservar, deja de buscar.

Uso:
    .venv/bin/python -m cita_previa.run                  # launchd/systemd lo lanzan cada minuto; revisa si toca
    .venv/bin/python -m cita_previa.run --ya             # revisa ahora, aunque no le toque
    .venv/bin/python -m cita_previa.run --aprendizaje    # qué aprendió: ritmo, piso, bloqueos, horas con citas
    .venv/bin/python -m cita_previa.run --solo-revisar   # comprueba si hay citas, pero no reserva
    .venv/bin/python -m cita_previa.run --reiniciar      # vuelve a buscar después de una reserva
    .venv/bin/python -m cita_previa.run --ver            # ventana visible, 2 s en cada página (no reserva)
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import requests

# Hora de Madrid siempre (la de la web y la de las citas), también en servidores en UTC y en pruebas a mano: si
# no, una pausa escrita a mano en UTC la leía el timer de systemd (en hora de Madrid) como ya pasada.
if not os.environ.get("TZ"):
    os.environ["TZ"] = "Europe/Madrid"
    time.tzset()

from cita_previa import escucha, notify, tls  # noqa: E402 - después de fijar la zona horaria
from cita_previa.aprendizaje import Memoria
from cita_previa.config import DATA_DIR, ROOT, env, load_config

ICP = f"https://{tls.ICP_HOST}/icpplus/index.html"
SHOTS = DATA_DIR / "ultima"              # capturas de la última corrida (se sobrescriben)
DONE = DATA_DIR / "RESERVADA"            # existe = ya hay cita: no buscar más
PAUSE = DATA_DIR / "pausa_hasta"         # bloqueo del sitio: esperar antes de reintentar
LOCK = DATA_DIR / "corriendo"
LAST_ERROR = DATA_DIR / "ultimo_error"   # para no repetir el mismo aviso de error cada 10 min
LAST_EMPTY = DATA_DIR / "ultimo_sin_citas"
LAST_VISIT = DATA_DIR / "ultima_visita"  # dos visitas seguidas desde la misma IP hacen saltar el cortafuegos
MIN_ENTRE_VISITAS = 5                    # por defecto; `min_entre_visitas_min` en la config (a 4 min ya bloquearon)
MEMORIA = DATA_DIR / "aprendizaje.json"  # ritmo aprendido, bloqueos y revisiones (ver aprendizaje.py)
LATIDO = DATA_DIR / "latido"             # se toca en cada arranque: si pasa mucho sin tocarse, el equipo dormía
NO_ENVIADOS = DATA_DIR / "no_enviados.json"
BLOQUEOS = DATA_DIR / "bloqueos"         # captura y HTML de cada bloqueo o error (las 50 últimas)
PAGINAS = DATA_DIR / "paginas"           # copia de las páginas cuando hubo citas
CAMPOS_APRENDIDOS = DATA_DIR / "campos_aprendidos.json"  # datos que la web pidió y respondiste por Telegram
SI, NO = {"si", "sí", "s", "yes", "y", "1", "true"}, {"no", "n", "0", "false"}
MARCA = "--cita-agente-marca"            # argumento inofensivo para reconocer nuestro Chrome al cerrarlo

NO_CITAS = "no hay citas disponibles"
BLOCKED = ("requested url was rejected", "too many requests", "demasiadas peticiones", "intrusion prevention")
SESION_CADUCADA = "sesión ha caducado"
ERROR_SERVIDOR = ("internal server error", "se ha producido un error", "service unavailable", "bad gateway")
PANTALLA_CITA = ("select#idSede", "#txtTelefonoCitado",          # lo que solo aparece si de verdad hay citas
                 "input[type=radio][id^=cita], input[type=radio][name^=rdbCita]")
SOLICITAR = "#btnEnviar, input[value*='Solicitar Cita' i], button:has-text('Solicitar Cita')"
SIN_CLAVE = "#btnEntrar, input[value*='sin Cl@ve' i], button:has-text('sin Cl@ve')"
DATOS_SIN_CLAVE = ("nie", "nombre", "pais")
CODIGO = re.compile(r"\d{4,8}")
BANNER = re.compile(r"\b(Información|Error|Aviso|Atención)\s*:\s*(\S[^\n]*)", re.I)
RUIDO = {"ir al contenido", "accesibilidad", "inicio", "procedimientos", "mis expedientes", "mis notificaciones",
         "ayuda", "[go back]", "aviso legal", "contacto", "aceptar", "volver", "acepto", "cita previa extranjería"}


class Bloqueado(Exception):
    """El mensaje es el texto que muestra la web (p. ej. "The requested URL was rejected… support ID")."""


class Inesperado(Exception):
    pass


class CampoDesconocido(Inesperado):
    """La web pide un dato obligatorio que el agente no sabe rellenar: se añade en `campos_extra`."""


class SesionCaducada(Exception):
    """El sitio dice "Su sesión ha caducado" (pasa cuando el servidor va mal): se reintenta una vez."""


def banner(texto: str) -> str | None:
    """Texto del recuadro de la web, p. ej. "Información: En este momento no hay citas disponibles"."""
    m = BANNER.search(texto)
    if m:
        return f"{m.group(1).capitalize()}: {m.group(2).strip()}"
    # "Paso 1 de 5": el recuadro lleva "Información" como título, sin dos puntos, y el texto en líneas aparte
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    for i, linea in enumerate(lineas):
        if NO_CITAS in linea.lower():
            siguiente = lineas[i + 1] if i + 1 < len(lineas) else ""
            return f"Información: {linea}" + (f" {siguiente}" if siguiente.lower().startswith("en breve") else "")
    return None


def mensaje_web(texto: str, largo: int = 400) -> str:
    """Lo que dice la página, sin menús ni aviso de cookies: el recuadro si lo hay, si no el texto limpio."""
    if b := banner(texto):
        return b
    lineas = [l.strip() for l in texto.splitlines()
              if l.strip() and l.strip().lower() not in RUIDO and "cookies" not in l.lower()]
    return (" | ".join(lineas) or "(página vacía)")[:largo]


def hay_citas_segun(texto: str, pantalla_cita: bool) -> bool:
    """Decide tras pedir cita. Solo hay citas si se ve la pantalla de reserva: un error 500 u otra página rara
    no contiene "no hay citas" y antes daba una falsa alarma."""
    t = texto.lower()
    if NO_CITAS in t:
        return False
    if pantalla_cita:
        return True
    if any(e in t for e in ERROR_SERVIDOR):
        raise Inesperado("La web dio un error del servidor (no sé si hay citas)")
    raise Inesperado("Página inesperada tras pedir cita: puede que haya citas, mira tú")


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def avisar(text: str, sonido: bool = False) -> None:
    log(text.replace("\n", " | "))
    if not notify.telegram(text):
        no_enviado(text.splitlines()[0][:120])
        notify.macos("Cita extranjería", text.splitlines()[0])
    if sonido:
        notify.sound()


def esperar_codigo_telegram(minutos: int = 8) -> str | None:
    """Espera a que respondas al bot SOLO con el código SMS (4 a 8 dígitos)."""
    return esperar_respuesta_telegram(minutos, CODIGO.fullmatch)


def esperar_respuesta_telegram(minutos: int = 8, valida=bool) -> str | None:
    """Espera tu respuesta en el chat del bot (solo de TELEGRAM_CHAT_ID) que cumpla `valida`. Descarta los
    mensajes anteriores a la pregunta."""
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token:
        return None
    if escucha.activo():          # el escuchador es el único que lee Telegram: sus mensajes, de su cola
        inicio, limite = time.time() - 5, time.time() + minutos * 60
        escucha.sacar_todo(escucha.MENSAJES)                     # descarta lo anterior a la pregunta
        while time.time() < limite:
            for m in escucha.sacar_todo(escucha.MENSAJES):
                if m.get("fecha", 0) >= inicio and valida(m["texto"]):
                    return m["texto"]
            time.sleep(2)
        return None
    api = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        updates = requests.get(api, timeout=20).json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else None
    except (requests.RequestException, ValueError):
        offset = None
    limit = time.time() + minutos * 60
    while time.time() < limit:
        try:
            r = requests.get(api, params={"offset": offset, "timeout": 30}, timeout=40).json()
        except (requests.RequestException, ValueError):
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            message = u.get("message") or {}
            if str((message.get("chat") or {}).get("id")) != str(chat_id):   # solo tu chat
                continue
            text = (message.get("text") or "").strip()
            if valida(text):
                return text
    return None


class Flujo:
    def __init__(self, page, cfg: dict, n: int = 0):
        self.page, self.cfg, self.n = page, cfg, n
        self.paso = "inicio"            # último paso alcanzado: para avisos claros y la memoria
        self.confirmando = False        # tras pulsar Confirmar no se repite nada (podría duplicar la reserva)
        self.pasos: list[str] = []      # páginas recorridas, para saber en qué paso salió "no hay citas"

    def foto(self, nombre: str, full_page: bool = True) -> Path | None:
        self.n += 1
        self.pasos.append(nombre)
        path = SHOTS / f"{self.n:02d}_{nombre}.png"
        self.anotar_botones(nombre)
        if self.cfg.get("pausa_ver_ms"):         # --ver: para que se pueda ver cada pantalla
            try:
                self.page.wait_for_timeout(self.cfg["pausa_ver_ms"])
            except Exception:  # noqa: BLE001 - ventana cerrada a mano
                pass
        try:    # el HTML, para estudiar sin conexión formularios y JavaScript (p. ej. el recuadro 0017)
            path.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001 - diagnóstico: nunca corta el flujo
            pass
        try:
            self.page.screenshot(path=path, full_page=full_page)
            return path
        except Exception:  # noqa: BLE001 - la captura nunca debe cortar el flujo
            return None

    def anotar_botones(self, nombre: str) -> None:
        """Deja en el log los botones visibles con su id y texto, para conocer los ids reales de cada pantalla."""
        try:
            botones = self.page.eval_on_selector_all(
                "button, input[type=submit], input[type=button], input[type=image], a.btn, [id^=btn]",
                "els => [...new Set(els)].filter(e => e.getClientRects().length).map(e => "
                "'#' + (e.id || '(sin id)') + ' «' + (e.value || e.innerText || '').trim().slice(0, 40) + '»')")
            log(f"Botones en {nombre} ({self.page.url.split('?')[0].rsplit('/', 1)[-1]}): {', '.join(botones) or '-'}")
        except Exception:  # noqa: BLE001 - diagnóstico: nunca corta el flujo
            pass

    def elemento(self, selector: str):
        """El primer elemento VISIBLE que cumple el selector (en una lista "a, b, c" puede haber alguno oculto)."""
        return self.page.locator(selector).filter(visible=True).first

    def texto_web(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:  # noqa: BLE001 - página cerrada o navegando
            return ""

    def texto(self) -> str:
        return self.texto_web().lower()

    def revisar_bloqueo(self) -> None:
        texto = self.texto_web()
        if any(b in texto.lower() for b in BLOCKED):
            raise Bloqueado(mensaje_web(texto))

    def esperar(self, selector: str, timeout: int = 120_000, obligatorio: bool = True, clicable: bool = False) -> bool:
        """Espera a que `selector` esté visible, y con `clicable` también habilitado (como element_to_be_clickable de
        Selenium). Mientras, si la web muestra bloqueo, sesión caducada o error del servidor, corta enseguida; si dice
        "no hay citas", devuelve False sin agotar el tiempo."""
        fin = time.time() + timeout / 1000
        while True:
            try:
                el = self.elemento(selector)
                if el.count() and (not clicable or el.is_enabled()):
                    return True
            except Exception:  # noqa: BLE001 - navegando: el documento cambia mientras se mira
                pass
            self.revisar_bloqueo()
            self.revisar_caducada()
            texto = self.texto()
            if NO_CITAS in texto:
                return False
            if any(e in texto for e in ERROR_SERVIDOR):
                raise Inesperado(f"La web dio un error del servidor (esperaba {selector})")
            if time.time() > fin:
                if obligatorio:
                    raise Inesperado(f"No apareció {selector} en {timeout // 1000} s")
                return False
            self.page.wait_for_timeout(500)

    def pulsar(self, selector: str, timeout: int = 180_000) -> bool:
        """Espera al botón, lo pulsa y espera solo al HTML de la página nueva (no a "load": con la web lenta, la
        analítica tardaba más de 60 s). Lo siguiente que se use se vuelve a esperar con `esperar`."""
        if not self.esperar(selector, clicable=True):
            return False                           # la web ya dice "no hay citas": nada que pulsar
        with self.page.expect_navigation(wait_until="domcontentloaded", timeout=timeout):
            # click() de Playwright vuelve a comprobar que esté visible, quieto, habilitado y que nada lo tape
            self.elemento(selector).click(timeout=30_000)
        self.revisar_bloqueo()
        self.revisar_caducada()
        return True

    def revisar_caducada(self) -> None:
        """Solo si el aviso está visible: algunas páginas llevan ese texto oculto en la plantilla."""
        try:
            visible = self.page.get_by_text(re.compile(SESION_CADUCADA, re.I)).first.is_visible()
        except Exception:  # noqa: BLE001 - sin coincidencias o navegando
            visible = False
        if visible:
            raise SesionCaducada(self.page.url)

    def hay(self, selector: str) -> bool:
        return self.page.locator(selector).count() > 0

    # --- 1. Provincia, trámite y entrada (sin Cl@ve, o con Cl@ve y certificado) ----------------
    def entrar(self) -> None:
        cfg = self.cfg
        self.paso = "inicio"
        self.page.goto(ICP, wait_until="domcontentloaded", timeout=180_000)
        self.esperar("select#form")
        self.paso = "provincia"
        self.page.select_option("select#form", label=cfg["provincia"])
        self.pulsar("#btnAceptar")
        self.foto("tramite")

        oficina = str(cfg.get("oficina") or "99")
        # "99" = Cualquier oficina, que ya viene elegida: no se toca el desplegable (ni Gran Vía ni Jardines)
        if oficina != "99" and self.esperar("select#sede") and self.page.input_value("select#sede") != oficina:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=180_000):
                self.page.select_option("select#sede", oficina)
        self.paso = "tramite"
        grupo = "tramiteGrupo[1]" if cfg.get("tramite_policia", True) else "tramiteGrupo[0]"
        sel = f"select[name='{grupo}']"
        self.esperar(sel)
        valor = self.page.eval_on_selector_all(
            f"{sel} option", "(os, t) => (os.find(o => o.text.toUpperCase().includes(t)) || {}).value",
            cfg["tramite"].upper())
        if not valor:
            raise Inesperado(f"No encontré el trámite «{cfg['tramite']}» en {cfg['provincia']}")
        self.page.select_option(sel, valor)
        self.pulsar("#btnAceptar")
        self.foto("info")

        self.paso = "acceso"
        if not cfg.get("con_clave"):
            self.pulsar(SIN_CLAVE)                     # "Presentación sin Cl@ve": formulario con NIE, nombre y país
            self.foto("entrada")
            return
        self.pulsar("#btnAccesoClave")
        self.foto("clave")
        boton = self.page.get_by_role("button", name=re.compile("Certificado electr", re.I))
        boton.first.click()
        fin = time.time() + 120
        while "icpplus/ac" not in self.page.url:      # vuelta de Cl@ve; a veces llega con la sesión caducada
            if "icpplus/infogenerica" in self.page.url and SESION_CADUCADA in self.texto():
                raise SesionCaducada(self.page.url)
            if time.time() > fin:
                raise Inesperado(f"No volvió de Cl@ve en 120 s (quedó en {self.page.url})")
            self.page.wait_for_timeout(1000)
        self.page.wait_for_load_state("domcontentloaded")
        self.revisar_bloqueo()
        self.foto("entrada")

    # --- 2. Datos del solicitante y comprobación de disponibilidad ---------------------------
    def hay_citas(self) -> bool:
        self.paso = "datos"
        if not self.esperar("#txtIdCitado, #btnEnviar"):     # formulario del solicitante (o ya "no hay citas")
            return False
        cfg = self.cfg
        copiar = self.page.get_by_role("button", name=re.compile("Copiar a datos solicitante", re.I))
        if copiar.count():      # con Cl@ve: pasa NIE y nombre del usuario a los datos del solicitante
            copiar.first.click()
        self.rellenar("#txtIdCitado", cfg.get("nie"))
        self.rellenar("#txtDesCitado", cfg.get("nombre"))
        self.rellenar("#txtAnnoCitado", str(cfg.get("anio_nacimiento") or ""))   # solo algunos trámites lo piden
        if self.hay("select#txtPaisNac") and cfg.get("pais"):
            self.page.select_option("select#txtPaisNac", label=cfg["pais"].upper())
            self.page.locator("select#txtPaisNac").press("Tab")
        if self.hay("#btnEnviar"):
            self.preparar_envio()
            self.pulsar("#btnEnviar")
            self.foto("validar")
        if "acentrada" in self.page.url.lower():      # el formulario no avanzó: datos rechazados o captcha visible
            raise Inesperado("La web no aceptó NIE/nombre/país (o pidió un captcha). Mira data/ultima/.")
        # Pantalla de opciones (Solicitar Cita / Consultar… / Salir): esperar al botón y pulsarlo. Si la web ya dice
        # "no hay citas" (recuadro con código 0017), esperar() devuelve False y no se pulsa nada.
        self.paso = "solicitar"
        destino = ", ".join((SOLICITAR, *PANTALLA_CITA))
        if self.esperar(destino, timeout=30_000, obligatorio=False) and self.elemento(SOLICITAR).count():
            self.pulsar(SOLICITAR)
            self.foto("solicitar")
        self.esperar(", ".join(PANTALLA_CITA), timeout=30_000, obligatorio=False)   # o "no hay citas", o nada
        return hay_citas_segun(self.texto_web(), any(self.hay(s) for s in PANTALLA_CITA))

    def rellenar_extra(self) -> None:
        """Datos que la web pueda pedir y el código no conozca (id o name del campo → valor): los que respondiste
        por Telegram (data/campos_aprendidos.json) y, por encima, `campos_extra` de config/cita.yaml."""
        for campo, valor in {**leer_campos_aprendidos(), **(self.cfg.get("campos_extra") or {})}.items():
            if valor is not None:
                self.rellenar_campo(campo, valor)

    def rellenar_campo(self, campo: str, valor) -> None:
        """Texto: se escribe si está vacío; desplegable: por el texto de la opción o su valor; casilla:
        true/false; grupo de opciones (radio): el value de la opción."""
        el = self.elemento(f"[id='{campo}'], [name='{campo}']")
        if not el.count():
            return
        tipo = el.evaluate("e => e.tagName.toLowerCase() + ':' + (e.type || '')")
        if tipo.startswith("select"):
            try:
                el.select_option(label=str(valor), timeout=5_000)
            except Exception:  # noqa: BLE001 - no hay opción con ese texto: probar por su valor
                el.select_option(str(valor), timeout=5_000)
        elif tipo.endswith(":radio") and not isinstance(valor, bool):
            self.page.locator(f"input[type=radio][name='{campo}'][value='{valor}']").check()
        elif tipo.endswith((":checkbox", ":radio")):
            el.set_checked(bool(valor))
        elif not el.input_value():
            el.click()
            el.fill(str(valor))
            el.press("Tab")

    def obligatorios_vacios(self) -> list[dict]:
        """Campos visibles marcados como obligatorios que siguen vacíos: {clave, etiqueta, tipo, opciones}."""
        return self.page.evaluate("""() => {
            const texto = (s) => (s || '').replace(/campo obligatorio/ig, '').trim().split('\\n')[0].trim().slice(0, 60);
            const etiquetaDe = (e) => texto((e.labels && e.labels[0] && e.labels[0].innerText)
                || (e.closest('label') && e.closest('label').innerText) || e.title || e.name);
            const vistos = new Set(), faltan = [];
            for (const e of document.querySelectorAll('input, select, textarea')) {
                if (!e.getClientRects().length || e.disabled || e.type === 'hidden') continue;
                const obligatorio = e.required || e.getAttribute('aria-required') === 'true'
                    || [...(e.labels || [])].some(l => l.classList.contains('required'));
                if (!obligatorio) continue;
                const marcador = (o) => !o.value || ['0', '-1'].includes(o.value) || /^\\s*selecciona/i.test(o.text);
                const vacio = e.type === 'checkbox' ? !e.checked
                    : e.type === 'radio' ? !document.querySelector(`input[type=radio][name="${e.name}"]:checked`)
                    : e.tagName === 'SELECT' ? marcador(e.options[e.selectedIndex] || {})
                    : !e.value;
                const clave = e.type === 'radio' ? e.name : (e.id || e.name);
                if (!vacio || vistos.has(clave)) continue;
                vistos.add(clave);
                let tipo = 'texto', opciones = [];
                if (e.tagName === 'SELECT') {
                    tipo = 'lista';
                    opciones = [...e.options].filter(o => !marcador(o)).map(o => ({texto: o.text.trim(), valor: o.value}));
                } else if (e.type === 'radio') {
                    tipo = 'lista';
                    opciones = [...document.querySelectorAll(`input[type=radio][name="${e.name}"]`)]
                        .map(r => ({texto: etiquetaDe(r) !== r.name ? etiquetaDe(r) : r.value, valor: r.value}));
                } else if (e.type === 'checkbox') {
                    tipo = 'casilla';
                }
                faltan.push({clave: clave || 'sin id', etiqueta: etiquetaDe(e) || 'sin nombre', tipo, opciones});
            }
            return faltan;
        }""")

    def preguntar_campo(self, c: dict):
        """Pregunta por Telegram el valor de un campo obligatorio que el agente no conoce y espera tu respuesta.
        Devuelve el valor listo para `rellenar_campo`, o None si no respondiste a tiempo."""
        minutos = self.cfg.get("minutos_respuesta_campos", 8)
        texto = f"❓ La web pide un dato que no conozco: «{c['etiqueta']}» (#{c['clave']}).\n"
        if c["tipo"] == "lista":
            texto += "Respóndeme con el número o el texto de la opción:\n" + "\n".join(
                f"{n}. {o['texto']}" for n, o in enumerate(c["opciones"], 1))
        elif c["tipo"] == "casilla":
            texto += "¿La marco? Respóndeme sí o no."
        else:
            texto += "Respóndeme aquí solo con el valor."
        avisar(texto + f"\n(Tienes {minutos} min. Lo recordaré para la próxima vez.)", sonido=True)
        captura = self.foto(f"pregunta_{c['clave']}", full_page=False)
        notify.telegram_foto(captura, f"❓ Así está el formulario (falta «{c['etiqueta']}»)")

        def interpretar(r: str):
            r = r.strip()
            if c["tipo"] == "casilla":
                return True if r.lower() in SI else False if r.lower() in NO else None
            if c["tipo"] == "lista":
                if r.isdigit() and 1 <= int(r) <= len(c["opciones"]):
                    o = c["opciones"][int(r) - 1]
                else:
                    o = next((o for o in c["opciones"] if r.lower() in (o["texto"].lower(), o["valor"].lower())), None)
                return o and (o["texto"] if self.page.locator(f"select[id='{c['clave']}'], select[name='{c['clave']}']").count()
                              else o["valor"])
            return r or None

        respuesta = esperar_respuesta_telegram(minutos, lambda r: interpretar(r) is not None)
        if respuesta is None:
            return None
        valor = interpretar(respuesta)
        notify.telegram(f"👍 Pongo «{'sí' if valor is True else 'no' if valor is False else valor}» en «{c['etiqueta']}» y sigo.",
                        silent=True)
        return valor

    def preparar_envio(self) -> None:
        """Antes de enviar un formulario: rellena los datos conocidos y, si queda algún obligatorio vacío, te lo
        pregunta por Telegram, lo rellena y lo recuerda. Si no respondes a tiempo, se para sin enviar."""
        self.rellenar_extra()
        for c in self.obligatorios_vacios():
            valor = self.preguntar_campo(c) if env("TELEGRAM_BOT_TOKEN") else None
            if valor is not None:
                self.rellenar_campo(c["clave"], valor)
                guardar_campo_aprendido(c["clave"], valor)
        if faltan := self.obligatorios_vacios():
            raise CampoDesconocido("La web pide datos que no sé rellenar: "
                                   + ", ".join(f"{c['etiqueta']} (#{c['clave']})" for c in faltan)
                                   + ". Respóndeme cuando te lo pregunte o añádelos en campos_extra de config/cita.yaml")

    def rellenar(self, selector: str, valor: str | None) -> None:
        """Rellena y sale del campo con Tab, como una persona: así la web ejecuta sus comprobaciones al salir
        (p. ej. onchange="comprobarDatos()" en Nombre y apellidos), que con solo fill() podían no ejecutarse."""
        campo = self.page.locator(selector)
        if valor and campo.count() and campo.is_editable() and not campo.input_value():
            campo.click()
            campo.fill(valor)
            campo.press("Tab")

    # --- 3. Reserva -------------------------------------------------------------------------
    def reservar(self) -> str:
        cfg, page = self.cfg, self.page
        page.on("dialog", lambda d: d.accept())

        self.paso = "oficina"
        if self.hay("select#idSede"):                                          # oficina, si la pide
            page.select_option("select#idSede", index=0)
            self.preparar_envio()
            self.pulsar("#btnSiguiente")
            self.foto("oficina")
        self.paso = "contacto"
        if self.hay("#txtTelefonoCitado"):                                     # contacto
            if not cfg.get("telefono") or not cfg.get("email"):
                raise Inesperado("Faltan teléfono o correo en config/cita.yaml")
            self.rellenar("#txtTelefonoCitado", cfg["telefono"])
            self.rellenar("#emailUNO", cfg["email"])
            self.rellenar("#emailDOS", cfg["email"])
            self.preparar_envio()
            self.pulsar("#btnSiguiente")
            self.foto("contacto")
        if NO_CITAS in self.texto():
            raise Inesperado("Las citas se agotaron mientras reservaba")

        self.paso = "horario"
        radios = "input[type=radio][id^=cita], input[type=radio][name^=rdbCita]"
        self.esperar(radios, timeout=30_000, obligatorio=False)
        huecos = page.locator(radios)
        if not huecos.count():
            raise Inesperado("No encontré horarios para elegir")
        detalle = self.detalle_hueco(huecos.first)
        huecos.first.check()
        self.preparar_envio()
        self.pulsar("#btnSiguiente")
        self.foto("verificar")

        for casilla in ("#chkTotal", "#enviarCorreo"):
            if self.hay(casilla):
                page.check(casilla)
        if self.hay("#txtCodigoVerificacion"):
            self.paso = "sms"
            minutos = cfg.get("minutos_codigo_sms", 8)
            avisar(f"📱 Te llegó un SMS de la cita previa. Respóndeme aquí SOLO con el código (tienes {minutos} min)."
                   f"\nHorario elegido: {detalle}", sonido=True)
            notify.telegram_foto(self.foto("sms", full_page=False), f"📱 Esta es la cita que voy a confirmar: {detalle}")
            codigo = esperar_codigo_telegram(minutos)
            if not codigo:
                raise Inesperado("No llegó el código SMS a tiempo")
            page.fill("#txtCodigoVerificacion", codigo)
        self.preparar_envio()
        self.paso, self.confirmando = "confirmar", True
        self.pulsar("#btnConfirmar")
        self.foto("confirmada")

        texto = page.inner_text("body")
        if "confirmada" not in texto.lower():
            raise Inesperado("Pulsé Confirmar pero no veo la cita confirmada")
        justificante = re.search(r"justificante[^\n]*?([A-Z0-9]{6,})", texto, re.I)
        return detalle + (f"\nJustificante: {justificante.group(1)}" if justificante else "")

    def detalle_hueco(self, radio) -> str:
        """Texto del bloque del horario (fecha, hora, oficina) para el aviso."""
        try:
            bloque = radio.locator("xpath=ancestor::*[self::div or self::tr][1]").inner_text()
            return " ".join(bloque.split())[:300]
        except Exception:  # noqa: BLE001
            return "(sin detalle)"


def hay_internet() -> bool:
    """¿Hay Internet en este equipo? Para distinguir "la web ignora esta IP" de "este equipo no tiene red"."""
    return any(tls.conecta(h) for h in ("www.google.com", "1.1.1.1"))


def leer_campos_aprendidos() -> dict:
    try:
        return json.loads(CAMPOS_APRENDIDOS.read_text())
    except (OSError, ValueError):
        return {}


def guardar_campo_aprendido(campo: str, valor) -> None:
    datos = {**leer_campos_aprendidos(), campo: valor}
    CAMPOS_APRENDIDOS.write_text(json.dumps(datos, ensure_ascii=False, indent=1))
    log(f"Aprendido el campo «{campo}»: lo rellenaré solo la próxima vez.")


def resumen_envio(url: str, post_data: str | None) -> str:
    """Qué campos envía un formulario, SIN sus valores (solo su largo): el NIE y el nombre no quedan escritos."""
    campos = parse_qs(post_data or "", keep_blank_values=True)
    detalle = ", ".join(f"{k}({len(v[0]) if v else 0})" for k, v in campos.items())
    return f"POST {url.split('?')[0].rsplit('/', 1)[-1]}: {detalle or '(sin campos)'}"


def guardar_envio(request) -> None:
    if request.method == "POST" and request.resource_type == "document":
        try:
            with open(SHOTS / "envios.txt", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%H:%M:%S} {resumen_envio(request.url, request.post_data)}\n")
        except Exception:  # noqa: BLE001 - diagnóstico: nunca corta el flujo
            pass


def error_nuevo(error: str, horas: int = 3, path: Path | None = None) -> bool:
    """True si el error es distinto al último avisado o pasaron más de `horas` desde ese aviso."""
    path = path or LAST_ERROR       # se lee al llamar (no al definir): las pruebas lo desvían a una carpeta temporal
    clave = error.splitlines()[0][:200] if error else "?"
    if path.exists() and path.read_text() == clave and time.time() - path.stat().st_mtime < horas * 3600:
        return False
    path.write_text(clave)
    return True


def visita_reciente(minutos: float = MIN_ENTRE_VISITAS, path: Path | None = None) -> float | None:
    """Minutos desde la última visita a la web si fue hace menos de `minutos` (si no, None). El 19/10 una prueba
    manual 3 s después de una revisión de launchd provocó el bloqueo de la IP."""
    path = path or LAST_VISIT
    if path.exists():
        hace = (time.time() - path.stat().st_mtime) / 60
        if hace < minutos:
            return hace
    return None


def toca_aviso_sin_citas(horas: float, path: Path | None = None) -> bool:
    """"Sin citas" se avisa como mucho cada `horas` (0 = nunca): así sabes que sigue vivo sin 144 avisos al día."""
    path = path or LAST_EMPTY
    if horas <= 0 or (path.exists() and time.time() - path.stat().st_mtime < horas * 3600):
        return False
    path.touch()
    return True


def nombre_app(ejecutable: str) -> str:
    """Nombre del proceso en macOS: Playwright ya no trae "Chromium" sino "Google Chrome for Testing"."""
    return next((d.stem for d in Path(ejecutable).parents if d.suffix == ".app"), "Chromium")


def faltan_datos(cfg: dict) -> list[str]:
    """Sin Cl@ve la web exige NIE, nombre y país: sin ellos no tiene sentido abrir el navegador."""
    return [] if cfg.get("con_clave") else [k for k in DATOS_SIN_CLAVE if not cfg.get(k)]


def cerrar_chrome(contexto, navegador) -> None:
    """Chrome for Testing a veces falla al apagarse (EXC_BREAKPOINT) y macOS muestra "se cerró inesperadamente".
    Se cierra la pestaña y luego se termina el proceso con SIGKILL: macOS no lo cuenta como fallo."""
    try:
        contexto.close()
    except Exception:  # noqa: BLE001 - el navegador puede estar ya caído
        pass
    subprocess.run(["pkill", "-9", "-f", "--", MARCA], check=False, capture_output=True)
    try:
        navegador.close()     # ya sin proceso: solo libera Playwright y el perfil temporal
    except Exception:  # noqa: BLE001
        pass


def fijar_ip(ip: str | None) -> None:
    """Con client_certificates Playwright conecta desde su proceso Node, que no ve las reglas de Chromium:
    se le precarga fijar_ip.cjs. Debe llamarse antes de sync_playwright()."""
    if ip:
        os.environ["CITA_FIJAR_IP"] = f"{tls.ICP_HOST}={ip}"
        os.environ["NODE_OPTIONS"] = f"--require {Path(__file__).with_name('fijar_ip.cjs')}"


def lanzar(p, cfg: dict, ip: str | None):
    opciones = {"locale": "es-ES", "viewport": {"width": 1280, "height": 900}}
    if cfg.get("con_clave"):
        cert = cfg.get("certificado") or {}
        archivo = ROOT / cert.get("archivo", "data/certificado.p12")
        if not archivo.exists():
            raise Inesperado(f"Falta el certificado {archivo.relative_to(ROOT)}: prepáralo con "
                             "python -m cita_previa.certificado")
        opciones["client_certificates"] = [
            {"origin": o, "pfxPath": str(archivo), "passphrase": env("CITA_CERT_PASSWORD") or ""}
            for o in cert.get("origenes", [])]
    # Cadena ya validada: el navegador acepta solo esa clave aunque el servidor no envíe el intermedio
    args = [MARCA, f"--ignore-certificate-errors-spki-list={tls.clave_verificada(tls.ICP_HOST, ip)}"]
    if ip:
        args.append(f"--host-resolver-rules=MAP {tls.ICP_HOST} {ip}")
    if cfg.get("ventana_oculta", True):    # en headless el cortafuegos del sitio bloquea: ventana real, fuera de vista
        args.append("--window-position=-2400,-2400")
    # "chrome" = tu Google Chrome instalado, tal cual (sin ocultar la automatización); "chromium" = el de Playwright
    canal = "chrome" if cfg.get("navegador") == "chrome" else None
    if canal and platform.system() == "Linux" and not Path("/opt/google/chrome/chrome").exists():
        log("navegador: chrome, pero Google Chrome no está instalado en este servidor; uso Chromium.")
        canal = None
    navegador = p.chromium.launch(headless=cfg.get("headless", False), args=args, slow_mo=cfg.get("slow_mo", 0),
                                  channel=canal)
    return navegador, navegador.new_context(**opciones)


PASOS = {  # qué hace cada paso, para que los avisos se entiendan sin leer el código
    "conexion": "conectar con el sitio de citas", "inicio": "abrir la página de inicio",
    "provincia": "elegir la provincia", "tramite": "elegir el trámite", "acceso": "entrar sin Cl@ve",
    "datos": "poner tu NIE, nombre y país", "solicitar": "pulsar Solicitar Cita", "oficina": "elegir la oficina",
    "contacto": "poner teléfono y correo", "horario": "elegir el horario", "sms": "esperar el código SMS",
    "confirmar": "confirmar la cita",
}


def explicar_error(e: Exception) -> str:
    """La excepción de un recorrido fallido, en una frase clara (los detalles técnicos quedan en el log)."""
    if isinstance(e, SesionCaducada):
        return "la web dijo que la sesión había caducado (pasa cuando el servidor va mal)"
    texto = str(e).splitlines()[0] if str(e) else type(e).__name__
    if "Timeout" in texto:
        return "la página no respondió a tiempo (la web estaba lenta o no cargó)"
    if "Target page, context or browser has been closed" in texto:
        return "el navegador se cerró a mitad de la revisión"
    return texto[:200]


def no_enviado(que: str) -> None:
    """Un aviso que Telegram no entregó: queda en el log y se resume en el siguiente arranque."""
    motivo = notify.ultimo_error or "motivo desconocido"
    log(f"No se pudo enviar por Telegram: {que} ({motivo}).")
    try:
        lista = json.loads(NO_ENVIADOS.read_text()) if NO_ENVIADOS.exists() else []
    except ValueError:
        lista = []
    lista.append({"t": f"{datetime.now():%H:%M}", "que": que, "motivo": motivo})
    NO_ENVIADOS.write_text(json.dumps(lista[-20:], ensure_ascii=False))


def reportar_no_enviados() -> None:
    if not NO_ENVIADOS.exists():
        return
    try:
        lista = json.loads(NO_ENVIADOS.read_text())
    except ValueError:
        lista = []
    if lista:
        lineas = "\n".join(f"• {a['t']} — {a['que']}: {a['motivo']}." for a in lista)
        if not notify.telegram(f"📭 No te llegaron {len(lista)} aviso(s) de Cita:\n{lineas}\n"
                               "El agente siguió revisando con normalidad.", silent=True):
            return                       # sigue sin conexión: se intenta en el próximo arranque
    NO_ENVIADOS.unlink(missing_ok=True)


def apagado_programado(cfg: dict, desde: datetime, hasta: datetime) -> bool:
    """True si el hueco sin revisar coincide con el apagado diario de la config (p. ej. "01:00-07:00" en AWS)."""
    franja = str(cfg.get("apagado") or "")
    if "-" not in franja:
        return False
    ini, fin = (datetime.strptime(x.strip(), "%H:%M").time() for x in franja.split("-"))
    margen = timedelta(minutes=30)
    a, b = datetime.combine(desde.date(), ini), datetime.combine(hasta.date(), fin)
    return a - margen <= desde <= a + margen and b <= hasta <= b + margen


def revisar_latido(cfg: dict) -> None:
    """Avisa si el agente estuvo mucho sin arrancar (Mac dormida o apagada, instancia caída)."""
    if LATIDO.exists():
        desde = datetime.fromtimestamp(LATIDO.stat().st_mtime)
        minutos = (datetime.now() - desde).total_seconds() / 60
        if minutos > cfg.get("aviso_parado_min", 30) and not apagado_programado(cfg, desde, datetime.now()):
            hace = f"{minutos / 60:.1f} h" if minutos >= 90 else f"{minutos:.0f} min"
            avisar(f"⏸️ Cita: estuve {hace} sin revisar (¿equipo dormido o apagado?). Retomo ahora.")
    LATIDO.touch()


def aplicar_respuesta(mem: Memoria, tipo: str, si: bool, t: str) -> str:
    """Tu respuesta a "¿es un bloqueo?" (tipo bloq) o "¿fue un bloqueo encubierto?" (tipo err)."""
    if tipo == "bloq":
        respuesta = mem.responder_bloqueo(t, si)
        if not si and respuesta.startswith("Anotado"):
            PAUSE.unlink(missing_ok=True)
            mem.programar(datetime.now())
        return respuesta
    if si:
        respuesta = mem.error_fue_bloqueo(t)
        if respuesta.startswith("Aprendido"):
            hasta = datetime.now() + timedelta(minutes=mem.pausa_min())
            PAUSE.write_text(hasta.isoformat(timespec="minutes"))
            mem.programar(hasta)
        return respuesta
    return "Ok, queda como error normal."


def leer_respuestas(mem: Memoria) -> None:
    """Aplica los botones que pulsaste en Telegram. Con el escuchador activo (escucha.py) vienen de su cola: él ya
    te respondió al instante. Sin él, se leen aquí de Telegram (método antiguo, una vez por minuto)."""
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token:
        return
    api = f"https://api.telegram.org/bot{token}"
    if escucha.activo():
        pendientes = escucha.sacar_todo(escucha.PENDIENTES)
    else:
        pendientes = []
        try:
            params = {"timeout": 0, **({"offset": mem.offset_telegram} if mem.offset_telegram else {})}
            updates = requests.get(f"{api}/getUpdates", params=params, timeout=20).json().get("result", [])
        except (requests.RequestException, ValueError):
            return
        for u in updates:
            mem.offset_telegram = u["update_id"] + 1
            q = u.get("callback_query") or {}
            partes, msg = q.get("data", "").split(":", 2), q.get("message") or {}
            if len(partes) == 3 and partes[0] in ("bloq", "err") and str((q.get("from") or {}).get("id")) == str(chat_id):
                pendientes.append({"tipo": partes[0], "si": partes[1] == "si", "t": partes[2], "cq": q.get("id"),
                                   "chat": (msg.get("chat") or {}).get("id"), "mid": msg.get("message_id"),
                                   "caption": msg.get("caption") or ""})
    ultimas = {(p["tipo"], p["t"]): p for p in pendientes}       # varias pulsaciones del mismo aviso: vale la última
    for p in ultimas.values():
        respuesta = aplicar_respuesta(mem, p["tipo"], p["si"], p["t"])
        log(f"Respuesta en Telegram sobre el {'bloqueo' if p['tipo'] == 'bloq' else 'error'} de {p['t']}: "
            f"{'sí' if p['si'] else 'no'} es bloqueo. {respuesta}")
        try:
            if p.get("cq"):
                requests.post(f"{api}/answerCallbackQuery", json={"callback_query_id": p["cq"]}, timeout=20)
            if p.get("mid"):       # sin reply_markup, Telegram quita también los botones
                requests.post(f"{api}/editMessageCaption", timeout=20, json={
                    "chat_id": p["chat"], "message_id": p["mid"],
                    "caption": p["caption"].split("\n")[0] + f"\n→ {respuesta}"})
        except requests.RequestException:
            pass


def conservar(carpeta: Path, nombre: str) -> None:
    """Copia la última captura y su HTML a `carpeta` (las de data/ultima/ se sobrescriben); guarda las 50 últimas."""
    try:
        capturas = sorted(SHOTS.glob("*.png"))
        if not capturas:
            return
        carpeta.mkdir(parents=True, exist_ok=True)
        base = carpeta / f"{datetime.now():%Y%m%d-%H%M%S}_{nombre}"
        shutil.copyfile(capturas[-1], base.with_suffix(".png"))
        if capturas[-1].with_suffix(".html").exists():
            shutil.copyfile(capturas[-1].with_suffix(".html"), base.with_suffix(".html"))
        for viejo in sorted(carpeta.glob("*.png"))[:-50]:
            viejo.unlink()
            viejo.with_suffix(".html").unlink(missing_ok=True)
    except OSError as e:
        log(f"No pude conservar la captura en {carpeta.name}: {e}")


def nueva_pagina(contexto, cfg: dict, p):
    page = contexto.new_page()
    # Con tu Chrome no se oculta la app: Cmd+H escondería también tus propias ventanas de Chrome.
    if cfg.get("ventana_oculta", True) and not cfg.get("headless") and cfg.get("navegador") != "chrome":
        notify.ocultar_app(nombre_app(p.chromium.executable_path))
    page.on("requestfailed", lambda r: log(f"Pide certificado: {r.url.split('/')[2]}")
            if "CLIENT_AUTH_CERT" in (r.failure or "") else None)
    page.on("request", guardar_envio)        # campos de cada formulario enviado (sin valores)
    return page


def recorrido(contexto, p, cfg: dict, mem: Memoria, solo_revisar: bool):
    """Entrada → ¿hay citas? → reserva. Si algo falla antes de Confirmar, repite el recorrido completo con sesión
    nueva hasta `reintentos` veces (sin esperar si ya vio citas). Devuelve el último Flujo."""
    intentos = 1 + int(cfg.get("reintentos", 1))
    vio_citas, flujo, page = False, None, None
    try:
        for intento in range(intentos):
            if page:
                page.close()
                contexto.clear_cookies()
            page = nueva_pagina(contexto, cfg, p)
            flujo = Flujo(page, cfg, flujo.n if flujo else 0)
            try:
                flujo.entrar()
                if not flujo.hay_citas():
                    aviso = banner(flujo.texto_web()) or "Sin citas disponibles"
                    paso = "tras Solicitar Cita" if "solicitar" in flujo.pasos else "tras Aceptar, sin llegar a Solicitar Cita"
                    log(f"Sin citas ({paso}). La web dice: {aviso}" + (" (se agotaron)" if vio_citas else ""))
                    if (mem.d["revisiones"] and mem.d["revisiones"][-1]["r"] == "bloqueo"
                            and mem.d["revisiones"][-1].get("paso") == "conexion"):
                        avisar("✅ Cita: la web vuelve a responder desde este equipo. Sigo revisando.")
                    mem.registrar("sin_citas")
                    if toca_aviso_sin_citas(cfg.get("aviso_sin_citas_horas", 2)):
                        if not notify.telegram(f"⏳ {datetime.now():%H:%M} Toma de huellas en {cfg['provincia']}:\n"
                                               f"«{aviso}»\nSigo revisando cada ~{mem.intervalo() / 60:.0f} min.",
                                               silent=True):      # solo texto: la foto, solo si hay citas o algo falla
                            LAST_EMPTY.unlink(missing_ok=True)   # no llegó: que la próxima revisión lo reintente
                            no_enviado("⏳ sin citas")
                    return flujo
                if not vio_citas:
                    vio_citas = True
                    mem.registrar("hay_citas")
                    mem.guardar()
                    avisar(f"🚨 ¡HAY CITAS de toma de huellas en {cfg['provincia']}! "
                           + ("Entra ya a reservar." if solo_revisar else "Intentando reservar…"), sonido=True)
                    notify.telegram_foto(flujo.foto("hay_citas", full_page=False), "🚨 Así se ve la web: ¡hay citas!")
                if solo_revisar:
                    return flujo
                detalle = flujo.reservar()
                mem.registrar("reservada")
                DONE.write_text(f"{datetime.now():%Y-%m-%d %H:%M}\n{detalle}\n")
                avisar(f"✅ CITA RESERVADA\n{detalle}\nRevisa tu correo para el justificante. "
                       "Capturas en data/ultima/.", sonido=True)
                notify.telegram_foto(flujo.foto("reservada", full_page=False), "✅ Cita confirmada en la web")
                return flujo
            except Bloqueado:
                raise
            except Exception as e:  # noqa: BLE001 - se reintenta; el último intento sube a main()
                flujo.foto("error", full_page=False)
                if flujo.confirmando or intento == intentos - 1 or isinstance(e, CampoDesconocido):  # repetir no lo arregla
                    e.flujo = flujo
                    raise
                espera = 0 if vio_citas else cfg.get("espera_reintento_s", 30) * 2 ** intento
                log(f"Falló al {PASOS.get(flujo.paso, flujo.paso)}: {str(e).splitlines()[0][:150]} — reintento "
                    f"{intento + 1}/{intentos - 1}" + (" ya (hay citas)." if vio_citas else f" en {espera} s."))
                page.wait_for_timeout(espera * 1000)
    finally:
        if vio_citas:                     # las únicas páginas reales de la reserva: se guardan para ajustar el código
            destino = PAGINAS / f"{datetime.now():%Y%m%d-%H%M%S}"
            try:
                shutil.copytree(SHOTS, destino, dirs_exist_ok=True)
            except OSError as e:
                log(f"No pude guardar las páginas de la reserva: {e}")
    return flujo


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente de cita previa de extranjería")
    parser.add_argument("--ya", action="store_true", help="Revisa ahora, aunque no le toque según el ritmo")
    parser.add_argument("--solo-revisar", action="store_true", help="Comprueba citas sin reservar")
    parser.add_argument("--reiniciar", action="store_true", help="Vuelve a buscar tras una reserva")
    parser.add_argument("--ver", action="store_true",
                        help="Ventana visible a velocidad normal (implica --ya y --solo-revisar)")
    parser.add_argument("--navegador", choices=("chromium", "chrome"),
                        help="Navegador para esta revisión (por defecto, el de config/cita.yaml)")
    parser.add_argument("--aprendizaje", action="store_true", help="Muestra lo aprendido y sale")
    args = parser.parse_args()
    if args.ver:
        args.ya = args.solo_revisar = True

    SHOTS.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    mem = Memoria(MEMORIA, cfg)
    if args.aprendizaje:
        print(mem.resumen())
        return
    if args.reiniciar:
        DONE.unlink(missing_ok=True)
        log("Reiniciado: vuelvo a buscar citas.")
    if DONE.exists():
        if args.ya:
            log("Ya hay una cita reservada (data/RESERVADA). Usa --reiniciar para buscar otra.")
        return
    corriendo = LOCK.exists() and time.time() - LOCK.stat().st_mtime < 20 * 60
    if not corriendo:                 # launchd/systemd lo lanzan cada minuto: esto se lee en < 1 min
        revisar_latido(cfg)
        leer_respuestas(mem)
        reportar_no_enviados()
        mem.guardar()
    if not args.ya and not mem.toca():          # el ritmo lo decide la memoria
        return
    if PAUSE.exists() and datetime.now() < datetime.fromisoformat(PAUSE.read_text().strip()):
        log(f"En pausa por bloqueo del sitio hasta {PAUSE.read_text().strip()}")
        mem.programar(datetime.fromisoformat(PAUSE.read_text().strip()))
        mem.guardar()
        return
    if corriendo:
        log("Otra revisión sigue en curso; salgo.")
        return
    if args.navegador:
        cfg["navegador"] = args.navegador
    if args.ver:      # visible y a velocidad normal: clic en cuanto el botón está listo, sin pausas
        cfg.update(ventana_oculta=False, headless=False, slow_mo=0)
    if faltan := faltan_datos(cfg):
        error = f"Faltan {', '.join(faltan)} en config/cita.yaml (sin Cl@ve la web los pide)"
        log(error)
        if error_nuevo(error):
            avisar(f"⚠️ Cita: {error}.")
        return
    minimo = float(cfg.get("min_entre_visitas_min", MIN_ENTRE_VISITAS))
    if (hace := visita_reciente(minimo)) is not None:
        log(f"Hubo una visita a la web hace {hace:.1f} min; espero para no provocar un bloqueo.")
        mem.programar(datetime.now() + timedelta(minutes=minimo - hace))
        mem.guardar()
        return

    from playwright.sync_api import sync_playwright   # import tardío: las pruebas no necesitan el navegador

    LOCK.touch()
    mem.programar(datetime.now() + timedelta(minutes=20))    # mientras corre, los arranques de cada minuto no entran
    mem.guardar()
    for f in [*SHOTS.glob("*.png"), *SHOTS.glob("*.html"), *SHOTS.glob("envios.txt")]:
        f.unlink()
    try:
        try:
            ip = tls.ip_alcanzable()
        except OSError as e:    # la web no responde: ¿bloqueo de la IP o problema de red de este equipo?
            log(str(e))
            if not hay_internet():
                mem.registrar("error", "conexion")
                if error_nuevo("sin internet"):
                    avisar("⚠️ Cita: este equipo no tiene conexión a Internet (tampoco responde Google). Sigo intentando.")
                return
            # Internet sí, la web no: la web ignora esta IP (pasó en AWS). Insistir alarga el bloqueo: pausa que
            # se duplica en cada intento fallido (15 min, 30, 1 h, 2 h, hasta 4 h) y un solo aviso.
            primera = not (mem.d["revisiones"] and mem.d["revisiones"][-1]["r"] == "bloqueo")
            mem.registrar("bloqueo", "conexion", frase="la web no responde a esta IP")
            hasta = datetime.now() + timedelta(minutes=mem.pausa_min())
            PAUSE.write_text(hasta.isoformat(timespec="minutes"))
            log(f"La web no responde pero Internet sí: probable bloqueo de la IP. Pauso hasta {hasta:%H:%M}.")
            if primera:
                avisar(f"⛔ Cita: la web de citas no responde desde este equipo, aunque Internet funciona: "
                       f"probablemente bloqueó la IP. Pauso hasta {hasta:%H:%M} y, si sigue igual, cada vez más "
                       "tiempo (hasta 4 h). Te aviso cuando vuelva a funcionar.")
            return
        if cfg.get("con_clave"):     # solo con certificado conecta el proceso Node de Playwright
            fijar_ip(ip)
        with sync_playwright() as p:
            try:
                navegador, contexto = lanzar(p, cfg, ip)
            except Exception as e:  # noqa: BLE001 - falta el certificado, TLS no válido, Chromium sin instalar…
                log(f"No pude abrir el navegador: {e}")
                if error_nuevo(str(e)):
                    avisar(f"⚠️ Cita: no pude abrir el navegador.\n{str(e).splitlines()[0][:300]}")
                return
            LAST_VISIT.touch()       # ahora sí se va a entrar en la web (si el navegador no abre, no cuenta)
            flujo = None
            try:
                flujo = recorrido(contexto, p, cfg, mem, args.solo_revisar)
            except Bloqueado as e:
                t = mem.registrar("bloqueo", frase=str(e)[:80])
                hasta = datetime.now() + timedelta(minutes=mem.pausa_min())
                PAUSE.write_text(hasta.isoformat(timespec="minutes"))
                pagina = contexto.pages[-1] if contexto.pages else None
                captura = SHOTS / "bloqueo.png"
                try:
                    pagina.screenshot(path=captura)       # solo lo visible: se lee bien en el móvil
                except Exception:  # noqa: BLE001
                    captura = None
                conservar(BLOQUEOS, "bloqueo")
                avisar("⛔ La web de citas bloqueó el acceso" + (f" en {pagina.url}" if pagina else "")
                       + f"\nLa web dice: «{e}»\nPauso hasta {hasta:%H:%M}.")
                if not notify.telegram_foto(captura, "⛔ Así se veía la web. ¿Es un bloqueo? Si sí, sigo un poco más "
                                            "lento; si no, retomo ya. Sin tu respuesta no cambio el ritmo.",
                                            botones=[("✅ Sí es bloqueo", f"bloq:si:{t}"),
                                                     ("❌ No es bloqueo", f"bloq:no:{t}")]):
                    no_enviado("captura del bloqueo")
            except Exception as e:  # noqa: BLE001 - cualquier fallo se avisa con su captura
                flujo = getattr(e, "flujo", None)
                paso = flujo.paso if flujo else "?"
                pagina = contexto.pages[-1] if contexto.pages else None
                log(f"Error al {PASOS.get(paso, paso)} ({pagina.url if pagina else '?'}): {e}")
                t = mem.registrar("error", paso)
                conservar(BLOQUEOS, f"error_{paso}")
                if flujo and NO_CITAS not in flujo.texto() and error_nuevo(str(e)):
                    reservando = flujo.confirmando or any(r["r"] == "hay_citas" for r in mem.d["revisiones"][-3:])
                    avisar(f"⚠️ {datetime.now():%H:%M} Cita: esta revisión no se pudo completar "
                           f"(probé {1 + int(cfg.get('reintentos', 1))} veces).\n"
                           f"Dónde: al {PASOS.get(paso, paso)}.\nQué pasó: {explicar_error(e)}.\n"
                           f"La web dice: «{mensaje_web(flujo.texto_web())}»\n"
                           + (f"🚨 ¡Estaba reservando una cita! Entra tú ya: {ICP}" if reservando
                              else "No tienes que hacer nada: sigo revisando."))
                    capturas = sorted(SHOTS.glob("*error*.png"))
                    if not notify.telegram_foto(capturas[-1] if capturas else None,
                                                "⚠️ Así se veía la web. ¿Fue un bloqueo encubierto? Si sí, pauso y "
                                                "sigo un poco más lento.",
                                                botones=[("🚫 Fue bloqueo", f"err:si:{t}"),
                                                         ("⚠️ Error normal", f"err:no:{t}")]):
                        no_enviado("captura del error")
            finally:
                if args.ver:
                    log("Modo --ver: dejo la ventana 10 s abierta antes de cerrar.")
                    try:
                        contexto.pages[-1].wait_for_timeout(10_000)
                    except Exception:  # noqa: BLE001 - cerraste tú la ventana: no es un error
                        pass
                cerrar_chrome(contexto, navegador)
    finally:
        try:
            leer_respuestas(mem)          # lo que respondiste mientras revisaba, sin esperar al siguiente arranque
        except Exception as e:  # noqa: BLE001 - nunca debe impedir guardar la memoria
            log(f"No pude aplicar respuestas de Telegram: {e}")
        if PAUSE.exists() and datetime.now() < datetime.fromisoformat(PAUSE.read_text().strip()):
            mem.programar(datetime.fromisoformat(PAUSE.read_text().strip()))
        else:
            log(f"Próxima revisión: {mem.programar_siguiente():%H:%M} (ritmo ~{mem.intervalo() / 60:.1f} min)")
        mem.guardar()
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
