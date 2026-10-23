"""Rutas, variables de entorno y configuración. Los secretos viven en .env y los datos personales en
config/cita.yaml; ninguno de los dos se sube a git."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config" / "cita.yaml"
DEFAULTS = {"navegador": "chromium", "con_clave": False, "oficina": "99", "tramite_policia": True, "headless": False, "ventana_oculta": True,
            "pausa_bloqueo_min": 15, "aviso_sin_citas_horas": 2, "minutos_codigo_sms": 8}
REQUIRED = ("provincia", "tramite")


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def load_config(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        raise SystemExit(f"Falta {path.relative_to(ROOT)}: cópialo de config/cita.example.yaml y complétalo")
    cfg = {**DEFAULTS, **(yaml.safe_load(path.read_text(encoding="utf-8")) or {})}
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise SystemExit(f"config/cita.yaml: faltan {', '.join(missing)}")
    return cfg
