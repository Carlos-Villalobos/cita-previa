"""Memoria del agente: decide cada cuánto revisar según lo que va pasando (sin modelo de lenguaje).

Idea tomada del agente `cita` de ai-personal-agents, implementada aquí de cero:
- Empieza en el mínimo (`minimo_s`, 5 min: una visita a los 4 min de la anterior provocó un bloqueo) y, mientras
  no haya errores, mantiene el ritmo (`baja_s: 0`; con más de 0 acelera ese tanto en cada revisión completa).
- Errores y bloqueos NO cambian el ritmo por sí solos (decides tú): solo si respondes por Telegram "✅ Sí es
  bloqueo" (o "🚫 Fue bloqueo" en un error) pasa a `sube_bloqueo` × el que había, y ese ritmo (+25 %) queda como
  piso durante 7 días. "❌ No es bloqueo" / "⚠️ Error normal" no lo cambian. Un bloqueo detectado sí pausa.
- De cada bloqueo confirmado se aprende también cuántas revisiones llevaba en la hora previa (tope por hora) y a
  qué hora fue (en horas con 2+ bloqueos en la semana revisa 1,5× más lento).
- En las horas en que aparecieron citas (últimos 60 días) y la anterior, revisa al doble de ritmo, sin bajar del
  mínimo ni del piso.
"""
import json
import os
import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

RITMO = {"minimo_s": 300, "maximo_s": 1800, "inicial_s": 300, "baja_s": 0, "sube_bloqueo": 1.25}
DIAS_PISO = 7
DIAS_CITAS = 60
MAX_REVISIONES = 3000
PAUSA_MAX_MIN = 240
COMPLETAS = ("sin_citas", "hay_citas", "reservada")


def _ahora() -> str:
    return f"{datetime.now():%Y-%m-%dT%H:%M:%S}"


class Memoria:
    def __init__(self, ruta: Path, cfg: dict | None = None):
        self.ruta, self.cfg = ruta, cfg or {}
        try:
            self.d = json.loads(ruta.read_text())
        except (OSError, ValueError):
            self.d = {}
        self.d.setdefault("revisiones", [])
        self.d.setdefault("bloqueos", [])

    def guardar(self) -> None:
        self.d["revisiones"] = self.d["revisiones"][-MAX_REVISIONES:]
        tmp = self.ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1))
        os.replace(tmp, self.ruta)      # nunca queda un JSON a medias

    # --- Ritmo ---------------------------------------------------------------------------------
    @property
    def limites(self) -> dict:
        return {**RITMO, **(self.cfg.get("ritmo") or {})}

    def intervalo(self) -> int:
        return int(self.d.get("intervalo_s", self.limites["inicial_s"]))

    def _confirmados(self) -> list[dict]:
        desde = f"{datetime.now() - timedelta(days=DIAS_PISO):%Y-%m-%dT%H:%M:%S}"
        self.d["bloqueos"] = [b for b in self.d["bloqueos"] if b["t"] >= desde][-50:]
        return [b for b in self.d["bloqueos"] if b.get("confirmado")]

    def piso(self) -> int:
        return int(max((b["cada_s"] for b in self._confirmados()), default=0) * 1.25)

    def tope_por_hora(self) -> int | None:
        cuentas = [b["por_hora"] for b in self._confirmados()]
        return max(1, min(cuentas) - 1) if cuentas else None

    def horas_con_bloqueos(self) -> set[int]:
        return {h for h, n in Counter(int(b["t"][11:13]) for b in self._confirmados()).items() if n >= 2}

    def horas_con_citas(self) -> set[int]:
        desde = f"{datetime.now() - timedelta(days=DIAS_CITAS):%Y-%m-%dT%H:%M:%S}"
        horas = {int(r["t"][11:13]) for r in self.d["revisiones"] if r["r"] == "hay_citas" and r["t"] >= desde}
        return {h % 24 for x in horas for h in (x - 1, x)}

    def _por_hora(self) -> int:
        desde = f"{datetime.now() - timedelta(hours=1):%Y-%m-%dT%H:%M:%S}"
        return sum(1 for r in self.d["revisiones"] if r["t"] >= desde)

    def _fijar(self, segundos: float) -> None:
        lim = self.limites
        self.d["intervalo_s"] = int(min(lim["maximo_s"], max(lim["minimo_s"], self.piso(), segundos)))

    # --- Resultados de cada revisión -----------------------------------------------------------
    def registrar(self, resultado: str, paso: str | None = None, frase: str | None = None) -> str:
        """resultado: sin_citas | hay_citas | reservada | error | bloqueo. Devuelve la marca de tiempo (sirve de
        identificador para los botones de Telegram)."""
        t, antes = _ahora(), self.intervalo()        # el ritmo al que pasó (antes de subirlo)
        anterior = self.d["revisiones"][-1]["r"] if self.d["revisiones"] else None
        self.d["revisiones"].append({"t": t, "r": resultado, "cada_s": antes, **({"paso": paso} if paso else {})})
        if resultado in COMPLETAS:
            self._fijar(self.intervalo() - self.limites["baja_s"])      # baja_s 0: sin errores, mantiene el ritmo
            self.d.pop("pausa_min", None)            # la pausa funcionó: la próxima vuelve a la base
        # error o bloqueo: el ritmo no cambia hasta que respondas en Telegram (responder_bloqueo / error_fue_bloqueo)
        if resultado == "bloqueo":
            self.d["bloqueos"].append({"t": t, "cada_s": antes, "frase": frase or "?", "paso": paso or "?",
                                       "por_hora": self._por_hora(), "confirmado": False})
            # bloqueo justo al volver de una pausa: la siguiente pausa dura el doble
            self.d["pausa_min"] = min(PAUSA_MAX_MIN, self.pausa_min() * 2) if anterior == "bloqueo" else self.pausa_min()
        return t

    def pausa_min(self) -> int:
        return int(self.d.get("pausa_min", self.cfg.get("pausa_bloqueo_min", 15)))

    def responder_bloqueo(self, t: str, es_bloqueo: bool) -> str:
        b = next((b for b in self.d["bloqueos"] if b["t"] == t), None)
        if not b:
            return "Ese bloqueo ya no está en la memoria."
        if not es_bloqueo:
            self.d["bloqueos"].remove(b)
            self.d.pop("pausa_min", None)
            for r in self.d["revisiones"]:
                if r["t"] == t:
                    r["r"] = "falsa_alarma"
            return "Anotado como falsa alarma: quito la pausa y sigo al mismo ritmo."
        if not b["confirmado"]:
            b["confirmado"] = True
            self._fijar(max(self.intervalo(), b["cada_s"] * self.limites["sube_bloqueo"]))
        return f"Confirmado: después de la pausa reviso cada {self.intervalo() / 60:.1f} min."

    def error_fue_bloqueo(self, t: str) -> str:
        r = next((r for r in self.d["revisiones"] if r["t"] == t and r["r"] == "error"), None)
        if not r:
            return "Ese error ya no está en la memoria."
        r["r"] = "bloqueo"
        self.d["bloqueos"].append({"t": t, "cada_s": r["cada_s"], "frase": f"error en {r.get('paso', '?')}",
                                   "paso": r.get("paso", "?"), "por_hora": self._por_hora(), "confirmado": True})
        self._fijar(max(self.intervalo(), r["cada_s"] * self.limites["sube_bloqueo"]))
        return f"Aprendido como bloqueo: pauso {self.pausa_min()} min y luego reviso cada {self.intervalo() / 60:.1f} min."

    # --- Cuándo toca -----------------------------------------------------------------------------
    def toca(self) -> bool:
        proxima = self.d.get("proxima")
        return not proxima or datetime.now() >= datetime.fromisoformat(proxima)

    def programar(self, cuando: datetime) -> None:
        self.d["proxima"] = cuando.isoformat(timespec="seconds")

    def programar_siguiente(self) -> datetime:
        segundos, ahora = self.intervalo(), datetime.now()
        if {ahora.hour, (ahora + timedelta(seconds=segundos)).hour} & self.horas_con_citas():
            segundos = max(self.limites["minimo_s"], self.piso(), segundos // 2)
        if ahora.hour in self.horas_con_bloqueos():
            segundos = int(segundos * 1.5)
        if tope := self.tope_por_hora():
            segundos = max(segundos, 3600 // tope)
        cuando = ahora + timedelta(seconds=segundos * random.uniform(0.8, 1.2))   # ±20 %: no ser regular
        self.programar(cuando)
        return cuando

    # --- Telegram ------------------------------------------------------------------------------
    @property
    def offset_telegram(self) -> int | None:
        return self.d.get("tg_offset")

    @offset_telegram.setter
    def offset_telegram(self, valor: int) -> None:
        self.d["tg_offset"] = valor

    # --- Resumen (--aprendizaje) ---------------------------------------------------------------
    def resumen(self) -> str:
        revs = self.d["revisiones"]
        conteo = Counter(r["r"] for r in revs)
        fallos = Counter(r.get("paso", "?") for r in revs if r["r"] == "error")
        vistas = [r["t"][:16] for r in revs if r["r"] == "hay_citas"]
        tope, piso = self.tope_por_hora(), self.piso()
        lineas = [
            f"Revisiones guardadas: {len(revs)}" + (f" ({', '.join(f'{k}: {v}' for k, v in conteo.items())})" if revs else ""),
            "Pasos que más fallan: " + (", ".join(f"{p} ×{n}" for p, n in fallos.most_common(5)) or "ninguno"),
            "Citas vistas: " + (", ".join(vistas[-10:]) or "ninguna todavía"),
            "Horas con citas: " + (", ".join(f"{h:02d}h" for h in sorted(self.horas_con_citas())) or "ninguna"),
            f"Ritmo: cada {self.intervalo() / 60:.1f} min · piso: " + (f"{piso / 60:.1f} min" if piso else "ninguno")
            + f" · pausa tras bloqueo: {self.pausa_min()} min",
            "Tope por hora: " + (f"{tope}" if tope else "ninguno") + " · horas con bloqueos: "
            + (", ".join(f"{h:02d}h" for h in sorted(self.horas_con_bloqueos())) or "ninguna"),
        ]
        for b in self.d["bloqueos"][-5:]:
            lineas.append(f"  bloqueo {b['t'][:16]} {'✓' if b.get('confirmado') else '(sin confirmar: no cuenta)'}: "
                          f"«{b['frase']}» en {b['paso']}, cada {b['cada_s'] / 60:.1f} min, {b['por_hora']} en la hora previa")
        if self.d.get("proxima"):
            lineas.append(f"Próxima revisión: {self.d['proxima']}")
        return "\n".join(lineas)
