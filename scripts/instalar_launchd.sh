#!/bin/zsh
# Programa el agente con launchd: lo lanza cada 1 min y el agente decide si toca revisar (ritmo aprendido, 5-30 min).
#   ./scripts/instalar_launchd.sh               → instala y arranca
#   ./scripts/instalar_launchd.sh desinstalar   → lo quita
#   ./scripts/instalar_launchd.sh estado        → ¿está cargado? y últimas líneas del log
set -e
LABEL="com.citaprevia.agente"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LABEL_ESCUCHA="com.citaprevia.escucha"          # escuchador de Telegram: siempre encendido (cita_previa.escucha)
PLIST_ESCUCHA="$HOME/Library/LaunchAgents/$LABEL_ESCUCHA.plist"
LOG="$PROJECT/data/cita.log"

case "${1:-}" in
  desinstalar)
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)" "$PLIST_ESCUCHA" 2>/dev/null || true
    rm -f "$PLIST" "$PLIST_ESCUCHA"
    echo "Agente desinstalado."
    exit 0 ;;
  estado)
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && echo "Cargado en launchd ($LABEL)" || echo "No está cargado"
    launchctl print "gui/$(id -u)/$LABEL_ESCUCHA" >/dev/null 2>&1 && echo "Escuchador de Telegram activo" || echo "Escuchador de Telegram NO activo"
    [[ -f "$PROJECT/data/RESERVADA" ]] && { echo "Ya hay cita reservada:"; cat "$PROJECT/data/RESERVADA"; }
    [[ -f "$LOG" ]] && tail -n 15 "$LOG"
    exit 0 ;;
esac

[[ -x "$PROJECT/.venv/bin/python" ]] || { echo "Falta .venv: instala el proyecto primero (ver README)"; exit 1; }
[[ -f "$PROJECT/.env" ]] || { echo "Falta .env (cópialo de .env.example)"; exit 1; }
[[ -f "$PROJECT/config/cita.yaml" ]] || { echo "Falta config/cita.yaml (cópialo de config/cita.example.yaml)"; exit 1; }
if grep -qE '^con_clave:[[:space:]]*true' "$PROJECT/config/cita.yaml"; then   # el certificado solo hace falta con Cl@ve
  [[ -f "$PROJECT/data/certificado.p12" ]] || { echo "Falta data/certificado.p12: python -m cita_previa.certificado RUTA.p12"; exit 1; }
fi

mkdir -p "$PROJECT/data"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT/.venv/bin/python</string>
    <string>-m</string>
    <string>cita_previa.run</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

cat > "$PLIST_ESCUCHA" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_ESCUCHA</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT/.venv/bin/python</string>
    <string>-m</string>
    <string>cita_previa.escucha</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$PROJECT/data/escucha.log</string>
  <key>StandardErrorPath</key><string>$PROJECT/data/escucha.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootout "gui/$(id -u)" "$PLIST_ESCUCHA" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_ESCUCHA"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Agente instalado: revisa según el ritmo aprendido (empieza cada ~10 min) mientras el Mac esté encendido."
echo "Log: tail -f $LOG"
