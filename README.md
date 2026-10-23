# cita-previa

Agente que busca la cita de **toma de huellas (TIE)** en la cita previa de extranjería (entrando por
**"Presentación sin Cl@ve"**, con tu NIE, nombre y país) y, en cuanto hay una, **la reserva** y te pide por
Telegram el código SMS para confirmarla. Basado en el agente `cita` de
[ai-personal-agents](https://github.com/joseigleleal/ai-personal-agents).

Corre **en tu Mac** (launchd) o **en una instancia EC2 con Ubuntu** (systemd + pantalla virtual). Se lanza cada
minuto y el agente decide si le toca revisar: el ritmo lo aprende (de 5 a 30 min; empieza en 10).

## Flujo

```
index.html ──▶ provincia ──▶ trámite ("Cualquier oficina") ──▶ "Presentación sin Cl@ve"
    ──▶ NIE, nombre y país ──▶ [Solicitar Cita] ──▶ ¿"no hay citas disponibles"?
          sí ─▶ "⏳ sin citas" (en silencio, solo texto) y termina
          no ─▶ 🚨 aviso + captura ─▶ oficina ─▶ teléfono y correo ─▶ primer horario
                ─▶ 📱 te pide el código SMS por Telegram ─▶ Confirmar ─▶ ✅ aviso con fecha y justificante
                ─▶ crea data/RESERVADA y deja de buscar
```

## Puesta en marcha (Mac)

```bash
uv venv --python 3.12 .venv.nosync && ln -sfn .venv.nosync .venv   # .nosync: iCloud no toca el entorno
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m playwright install chromium
```

1. **Bot de Telegram propio:** @BotFather → `/newbot`. Pon el token en `.env` (`TELEGRAM_BOT_TOKEN`) y envía
   `/start` a tu bot. Por ese chat te pedirá el código SMS, así que ningún otro programa debe leerlo.
2. **Tus datos:** copia `config/cita.example.yaml` a `config/cita.yaml` y complétalo (provincia, NIE, nombre,
   país, teléfono y correo). Sin Cl@ve, NIE, nombre y país son obligatorios.
3. **Prueba sin reservar:** `.venv/bin/python -m cita_previa.run --ya --solo-revisar` (capturas, HTML y campos
   enviados en `data/ultima/`). Para verlo en pantalla: `--ver` (ventana visible, no reserva).
4. **Programarlo:** `./scripts/instalar_launchd.sh` · estado: `… estado` · quitarlo: `… desinstalar`.
5. **Qué aprendió:** `.venv/bin/python -m cita_previa.run --aprendizaje` (ritmo, piso, bloqueos, horas con citas).

Tras reservar deja de buscar; para buscar otra: `.venv/bin/python -m cita_previa.run --reiniciar`.

## En el Mac sin ver ninguna ventana (máquina Linux de OrbStack)

Para que no se abra ninguna ventana de Chrome en tu pantalla, el agente corre en una máquina Linux ligera de
[OrbStack](https://orbstack.dev) dentro del Mac, con el mismo instalador que en AWS: Chromium con ventana dentro
de una pantalla virtual (Xvfb). Sale a Internet por tu Mac, así que usa **la IP de casa** (la web la acepta) y
solo revisa con el Mac encendido y despierto.

```bash
orb create ubuntu:noble cita                                   # crear la máquina (una vez)
orb -m cita bash -c 'sudo apt-get install -y git'
orb -m cita bash -c 'git clone ~/Documents/Python/cita-previa ~/cita-previa'   # su propia copia, su propio .venv
# copiar .env y config/cita.yaml desde la carpeta del Mac (la máquina la ve en /Users/…) y poner navegador: chromium
orb -m cita bash -c 'cd ~/cita-previa && ./deploy/aws/instalar.sh'
./scripts/instalar_launchd.sh desinstalar                      # en el Mac: un solo lugar a la vez
orb -m cita bash -c 'cd ~/cita-previa && ./deploy/aws/instalar.sh estado'     # ver cómo va
orb -m cita bash -c 'cd ~/cita-previa && git pull ~/Documents/Python/cita-previa main'   # actualizar el código
```

## En AWS (EC2 con Ubuntu)

> Probado el 19/10/2026 en una EC2 de EE. UU.: la web de citas **no responde** desde ahí (Google y Telegram sí).
> Solo tiene sentido en un servidor en España, y conviene comprobarlo antes con una conexión al puerto 443.

En el servidor no hay pantalla y el sitio bloquea el modo *headless*: Chromium corre **con ventana dentro de una
pantalla virtual (Xvfb)**. Un timer de systemd lo lanza cada minuto y tras cada arranque (3 min después), así que
si la instancia se apaga de 1:00 a 7:00, retoma sola al encenderse; con `apagado: "01:00-07:00"` en
`config/cita.yaml` no avisa de ese hueco. Los horarios del log van en hora de Madrid.

```bash
# En la instancia (Ubuntu 22.04/24.04, ≥ 2 GB de RAM recomendados)
git clone <tu-repo-privado> cita-previa && cd cita-previa
# Desde tu Mac, copia tú los dos archivos privados (no están en git):
#   scp .env config/cita.yaml ubuntu@<ip>:cita-previa/   (cita.yaml va en cita-previa/config/)
./deploy/aws/instalar.sh            # paquetes, Xvfb, Chromium y timer
./deploy/aws/instalar.sh estado     # próxima revisión y log
./deploy/aws/instalar.sh desinstalar
```

- En el servidor usa `navegador: chromium` (si pones `chrome` y no está instalado, usa Chromium igualmente).
- **Un solo lugar a la vez:** apaga launchd en el Mac (`./scripts/instalar_launchd.sh desinstalar`). Dos
  revisiones desde sitios distintos se pisan la sesión ("sesión caducada") y aumentan los bloqueos.
- El instalador se niega si `con_clave: true`: el certificado no debe salir del Mac.
- La IP de un centro de datos suele estar más vigilada que la de casa: mira las primeras revisiones del log.

## Decisiones

- **Ventana real:** en modo *headless* (también "new headless") el cortafuegos del sitio bloquea el acceso. En el
  Mac abre la ventana fuera de la pantalla y oculta la app como Cmd+H; en AWS, dentro de Xvfb.
- **No se disfraza el navegador:** no se cambia el user-agent ni se oculta la automatización para pasar los
  filtros anti-bots del sitio (por eso no se usa el "modo fantasma" del agente original).
- **Esperas por elemento:** cada clic espera a que el botón esté visible y habilitado (como
  `element_to_be_clickable` de Selenium); tras el clic solo se espera el HTML nuevo. Si la web muestra bloqueo,
  "sesión caducada", error del servidor o "no hay citas", corta al momento. "Sesión caducada" → un reintento.
- **Campos que el código no conoce:** antes de enviar cada formulario rellena `campos_extra` de
  `config/cita.yaml` y lo que ya respondiste (`data/campos_aprendidos.json`). Si aun así queda un obligatorio
  vacío, **te lo pregunta por Telegram** con la captura («❓ La web pide… Fecha de caducidad (#txtFecha)»;
  en desplegables y opciones, la lista numerada; en casillas, sí/no), lo rellena con tu respuesta, sigue y lo
  recuerda. Si no respondes en `minutos_respuesta_campos` (8), se para sin enviar el formulario a medias.
- **"¡Hay citas!" solo con pruebas:** solo si aparece la pantalla de reserva (oficina, teléfono u horarios).
- **Ritmo aprendido** (`aprendizaje.py`, idea del agente original implementada de cero): empieza cada 5 min (el
  mínimo: una visita a los 4 min de la anterior provocó un bloqueo; el original llega a 1 min) y, **sin errores,
  mantiene el ritmo**. **Los errores y bloqueos no cambian el ritmo solos: decides tú** con los botones de
  Telegram ("✅ Sí es bloqueo / ❌ No es bloqueo"; en errores, "🚫 Fue bloqueo / ⚠️ Error normal"): "Sí" lo
  sube ×1,25 y deja ese ritmo como piso 7 días, con tope por hora y más lento en las horas con bloqueos; "No" no
  lo cambia. Un bloqueo detectado sí pausa 15 min (el doble si se repite al volver; "No" quita la pausa). En las horas en que hubo citas revisa al doble (sin bajar del mínimo ni del piso).
- **Si la web deja de responder a la IP** (pasó en AWS: ni siquiera acepta la conexión) pero Internet funciona
  (Google responde), lo trata como bloqueo: pausa 15 min y la duplica en cada intento fallido (30 min, 1 h, 2 h,
  hasta 4 h), con un solo aviso y otro cuando vuelve a responder. Sin Internet en el equipo, es un error normal.
- **Prudencia:** nunca dos visitas a menos de 5 min, un candado contra revisiones simultáneas, el mismo error se
  avisa como mucho cada 3 h, en lenguaje claro (en qué paso y qué pasó).
- **Reintentos:** si un paso falla, repite el recorrido completo con sesión nueva (`reintentos`, espera creciente;
  sin esperar si ya vio citas). Después de pulsar Confirmar no se repite nada, para no duplicar la reserva.
- **Escuchador de Telegram** (`escucha.py`, servicio siempre encendido): es el único que lee el bot. Al pulsar un
  botón responde al instante («Recibido…») y lo quita (no hay pulsaciones repetidas); la respuesta queda en
  `data/respuestas_pendientes.json` y el agente la aplica en ≤1 min, también al terminar cada revisión. Los
  mensajes de texto (código SMS, datos que pide la web) quedan en `data/mensajes.json`. Sin escuchador, el agente
  lee Telegram él mismo una vez por minuto. **Nunca dos equipos con el mismo bot**: se roban las respuestas.
- **Avisos que no llegan:** Telegram se reintenta a los 3 s; si tampoco, el siguiente arranque te manda un
  resumen. Si el agente estuvo más de 30 min sin arrancar (equipo dormido), avisa al volver.
- **Copias:** captura y HTML de cada bloqueo o error en `data/bloqueos/` (50 últimas) y de las páginas de la
  reserva en `data/paginas/` cuando hay citas.
- **Cierre de Chrome con SIGKILL:** Chrome for Testing a veces falla al apagarse y macOS lo mostraba como cierre
  inesperado; se reconoce por el argumento `--cita-agente-marca`.
- **Sin Cl@ve por defecto.** Con `con_clave: true` vuelve al recorrido original con certificado (`.p12`
  re-cifrado con AES-256 y entregado por Playwright a Cl@ve; ver `cita_previa.certificado`). Solo en el Mac.
- **Cadena TLS validada por el agente** (el servidor no envía el intermedio) e **IP de respaldo** por DNS sobre
  HTTPS si la del DNS local no responde.

## Seguridad

- `.env`, `config/cita.yaml`, `data/` (capturas y HTML con tu NIE, certificado) y cualquier `.p12` están en
  `.gitignore`. `data/ultima/envios.txt` guarda los campos enviados solo con el largo de cada valor.
- El certificado sirve para firmar trámites en tu nombre: no lo copies a servidores ni lo compartas.
- El código SMS solo se acepta desde tu chat (`TELEGRAM_CHAT_ID`).

## Límites

- **Código 0017:** sin Cl@ve, tras Aceptar con NIE, nombre y país, la web le responde al agente "En este momento no
  hay citas disponibles" con el código 0017, mientras que a una persona con los mismos datos le muestra el menú
  "Solicitar Cita" y luego "Paso 1 de 5". Descartados: formulario, datos, ritmo, `onchange`, modo visible/oculto y
  navegador (Chromium y Chrome). Lo más probable es la defensa anti-bots del sitio (F5 TSPD). Mientras sea así,
  el agente puede no ver citas reales. El log dice en cada revisión si llegó a "Solicitar Cita".
- Las pantallas posteriores a "hay citas" se escribieron con los campos conocidos del sitio, pero solo se
  verifican con una cita real: si algo falla, el aviso dice "entra tú ya".
- Si la web muestra un captcha, el agente no lo resuelve: avisa y deja la captura.
- En el Mac solo busca con el equipo encendido, despierto y con tu sesión iniciada; en AWS, con la instancia
  encendida.
