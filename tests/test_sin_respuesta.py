"""La web deja de responder a la IP (pasó en AWS): pausa creciente y un solo aviso, sin insistir cada pocos minutos."""
import sys
from datetime import datetime

from cita_previa import run


def preparar(monkeypatch, tmp_path, internet=True):
    for nombre in ("SHOTS", "DONE", "PAUSE", "LOCK", "LAST_ERROR", "LAST_EMPTY", "LAST_VISIT", "MEMORIA", "LATIDO",
                   "NO_ENVIADOS"):
        monkeypatch.setattr(run, nombre, tmp_path / nombre.lower())
    monkeypatch.setattr(run, "load_config", lambda: {"provincia": "Madrid", "nie": "X", "nombre": "N",
                                                     "pais": "P", "pausa_bloqueo_min": 15})
    monkeypatch.setattr(run.tls, "ip_alcanzable", lambda: (_ for _ in ()).throw(ConnectionError("no responde")))
    monkeypatch.setattr(run, "hay_internet", lambda: internet)
    avisos = []
    monkeypatch.setattr(run, "avisar", lambda texto, sonido=False: avisos.append(texto))
    monkeypatch.setattr(sys, "argv", ["run", "--ya"])
    return avisos


def revisar_tras_la_pausa():
    run.PAUSE.unlink(missing_ok=True)          # como si ya hubiera pasado
    run.main()
    return (datetime.fromisoformat(run.PAUSE.read_text()) - datetime.now()).total_seconds() / 60


def test_pausa_creciente_y_un_solo_aviso(monkeypatch, tmp_path):
    avisos = preparar(monkeypatch, tmp_path)
    pausas = [revisar_tras_la_pausa() for _ in range(4)]
    assert all(abs(p - esperada) < 1 for p, esperada in zip(pausas, (15, 30, 60, 120)))   # se guarda al minuto
    assert len(avisos) == 1 and "bloqueó la IP" in avisos[0]


def test_sin_internet_no_es_bloqueo(monkeypatch, tmp_path):
    avisos = preparar(monkeypatch, tmp_path, internet=False)
    run.main()
    assert not run.PAUSE.exists() and "no tiene conexión" in avisos[0]
