#!/usr/bin/env bash
# Instala el agente en una instancia EC2 con Ubuntu (22.04/24.04) y lo programa con un timer de systemd:
# lo lanza cada 1 min (y 3 min tras cada arranque) y el agente decide si toca revisar (ritmo aprendido, 5-30 min).
# Se ejecuta EN la instancia, desde la carpeta del repo clonado:
#   ./deploy/aws/instalar.sh               → instala dependencias, Chromium y el timer
#   ./deploy/aws/instalar.sh estado        → ¿está activo? próxima revisión y últimas líneas del log
#   ./deploy/aws/instalar.sh desinstalar   → quita el timer (no borra nada más)
#
# Sin pantalla en el servidor: Chromium corre con ventana dentro de una pantalla virtual (Xvfb).
# El modo headless no sirve: el cortafuegos del sitio lo bloquea.
set -euo pipefail
NOMBRE="cita-previa"
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$PROJECT/data/cita.log"
USUARIO="$(id -un)"

case "${1:-}" in
  desinstalar)
    sudo systemctl disable --now "$NOMBRE.timer" "$NOMBRE-escucha.service" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$NOMBRE.service" "/etc/systemd/system/$NOMBRE.timer" \
               "/etc/systemd/system/$NOMBRE-escucha.service"
    sudo systemctl daemon-reload
    echo "Agente desinstalado (el código, .env y data/ siguen ahí)."
    exit 0 ;;
  estado)
    systemctl is-active --quiet "$NOMBRE.timer" && echo "Timer activo ($NOMBRE.timer)" || echo "Timer no activo"
    systemctl is-active --quiet "$NOMBRE-escucha.service" && echo "Escuchador de Telegram activo" || echo "Escuchador de Telegram NO activo"
    systemctl list-timers "$NOMBRE.timer" --no-pager | sed -n '1,2p'
    [[ -f "$PROJECT/data/RESERVADA" ]] && { echo "Ya hay cita reservada:"; cat "$PROJECT/data/RESERVADA"; }
    [[ -f "$LOG" ]] && tail -n 15 "$LOG"
    exit 0 ;;
esac

command -v apt-get >/dev/null || { echo "Este instalador es para Ubuntu (apt). Playwright no instala sus dependencias en Amazon Linux."; exit 1; }
[[ -f "$PROJECT/.env" ]] || { echo "Falta .env: cópialo tú desde tu Mac (scp) o créalo a partir de .env.example"; exit 1; }
[[ -f "$PROJECT/config/cita.yaml" ]] || { echo "Falta config/cita.yaml: cópialo tú desde tu Mac (scp) o desde config/cita.example.yaml"; exit 1; }
if grep -qE '^con_clave:[[:space:]]*true' "$PROJECT/config/cita.yaml"; then
  echo "config/cita.yaml tiene con_clave: true. El certificado no debe copiarse a servidores: pon con_clave: false."; exit 1
fi

memoria_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
(( memoria_mb < 1800 )) && echo "Aviso: la instancia tiene ${memoria_mb} MB de RAM; Chromium necesita ~1 GB libre. Si falla, usa una más grande o añade swap."

echo "== Paquetes del sistema (Python, Xvfb)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv xvfb xauth >/dev/null

echo "== Entorno de Python"
[[ -x "$PROJECT/.venv/bin/python" ]] || python3 -m venv "$PROJECT/.venv"
"$PROJECT/.venv/bin/python" -m pip install -q --upgrade pip
"$PROJECT/.venv/bin/python" -m pip install -q -e "$PROJECT[dev]"

echo "== Chromium de Playwright y sus librerías"
sudo "$PROJECT/.venv/bin/python" -m playwright install-deps chromium >/dev/null
"$PROJECT/.venv/bin/python" -m playwright install chromium

mkdir -p "$PROJECT/data"
chmod 600 "$PROJECT/.env" "$PROJECT/config/cita.yaml"

echo "== Timer de systemd"
sudo tee "/etc/systemd/system/$NOMBRE.service" >/dev/null <<SERVICE
[Unit]
Description=Cita previa: busca la cita de toma de huellas
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$USUARIO
WorkingDirectory=$PROJECT
Environment=TZ=Europe/Madrid
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1280x900x24" $PROJECT/.venv/bin/python -m cita_previa.run
StandardOutput=append:$LOG
StandardError=append:$LOG
TimeoutStartSec=30min
SERVICE

sudo tee "/etc/systemd/system/$NOMBRE.timer" >/dev/null <<TIMER
[Unit]
Description=Cita previa: arranque cada minuto (el agente decide si revisa)

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
TIMER

# Escuchador de Telegram: siempre encendido, es el único que lee el bot (responde al instante a los botones)
sudo tee "/etc/systemd/system/$NOMBRE-escucha.service" >/dev/null <<ESCUCHA
[Unit]
Description=Cita previa: escucha el bot de Telegram (botones y mensajes)
Wants=network-online.target
After=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$PROJECT
Environment=TZ=Europe/Madrid
ExecStart=$PROJECT/.venv/bin/python -m cita_previa.escucha
Restart=always
RestartSec=10
StandardOutput=append:$PROJECT/data/escucha.log
StandardError=append:$PROJECT/data/escucha.log

[Install]
WantedBy=multi-user.target
ESCUCHA

sudo systemctl daemon-reload
sudo systemctl enable --now "$NOMBRE.timer"
sudo systemctl enable "$NOMBRE-escucha.service" && sudo systemctl restart "$NOMBRE-escucha.service"
echo "Instalado. Revisa según el ritmo aprendido (empieza cada ~10 min) mientras la instancia esté encendida."
echo "Si la apagas cada noche, pon en config/cita.yaml: apagado: \"01:00-07:00\" (así no avisa de ese hueco)."
echo "Estado: ./deploy/aws/instalar.sh estado · Log: tail -f $LOG"
