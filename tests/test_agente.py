"""Lógica del agente que no necesita la web ni el navegador."""
import time

import pytest

from cita_previa import config, run


def test_codigo_sms_valido():
    assert run.CODIGO.fullmatch("123456") and run.CODIGO.fullmatch("1234")
    assert not run.CODIGO.fullmatch("12a456") and not run.CODIGO.fullmatch("123")


def test_error_nuevo_no_repite_en_3_horas(tmp_path):
    path = tmp_path / "ultimo_error"
    assert run.error_nuevo("Falló X\ndetalle", path=path)
    assert not run.error_nuevo("Falló X\notro detalle", path=path)      # mismo error: no se repite
    assert run.error_nuevo("Falló Y", path=path)                        # error distinto: sí
    assert run.error_nuevo("Falló Y", horas=0, path=path)               # pasado el plazo: sí


def test_aviso_sin_citas_como_mucho_cada_n_horas(tmp_path):
    path = tmp_path / "ultimo_sin_citas"
    assert run.toca_aviso_sin_citas(2, path)
    assert not run.toca_aviso_sin_citas(2, path)
    old = time.time() - 3 * 3600
    import os
    os.utime(path, (old, old))
    assert run.toca_aviso_sin_citas(2, path)
    assert not run.toca_aviso_sin_citas(0, tmp_path / "otro")             # 0 = nunca


def test_config_por_defecto_y_obligatorios(tmp_path):
    path = tmp_path / "cita.yaml"
    path.write_text("provincia: Madrid\ntramite: POLICÍA-TOMA DE HUELLAS\n")
    cfg = config.load_config(path)
    assert cfg["oficina"] == "99" and cfg["headless"] is False and cfg["aviso_sin_citas_horas"] == 2
    path.write_text("provincia: Madrid\n")
    with pytest.raises(SystemExit, match="tramite"):
        config.load_config(path)


def test_ejemplo_de_config_es_valido():
    cfg = config.load_config(config.ROOT / "config" / "cita.example.yaml")
    assert cfg["certificado"]["origenes"]


def test_sin_clave_exige_nie_nombre_y_pais():
    assert run.faltan_datos({"nie": "X1", "nombre": "A"}) == ["pais"]
    assert run.faltan_datos({"nie": "X1", "nombre": "A", "pais": "B"}) == []
    assert run.faltan_datos({"con_clave": True}) == []          # con Cl@ve los trae el certificado


def test_por_defecto_entra_sin_clave(tmp_path):
    path = tmp_path / "cita.yaml"
    path.write_text("provincia: Madrid\ntramite: POLICÍA-TOMA DE HUELLAS\n")
    assert config.load_config(path)["con_clave"] is False


def test_fijar_ip_prepara_node(monkeypatch):
    monkeypatch.delenv("NODE_OPTIONS", raising=False)
    run.fijar_ip(None)
    assert "NODE_OPTIONS" not in __import__("os").environ
    run.fijar_ip("1.2.3.4")
    import os
    assert os.environ["CITA_FIJAR_IP"].endswith("=1.2.3.4") and "fijar_ip.cjs" in os.environ["NODE_OPTIONS"]
    monkeypatch.delenv("NODE_OPTIONS")
    monkeypatch.delenv("CITA_FIJAR_IP")


def test_codigo_solo_de_tu_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls = iter([{"result": []}, {"result": [
        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "111111"}},   # otro chat: se ignora
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "hola"}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "654321"}}]}])

    class R:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data
    monkeypatch.setattr(run.requests, "get", lambda *a, **k: R(next(calls)))
    assert run.esperar_codigo_telegram(1) == "654321"


def test_banner_de_informacion():
    texto = ("CITA PREVIA EXTRANJERÍA\nInformación\nInformación: En este momento no hay citas disponibles\n0017\n"
             "Cod. Oper.: 3F30 16/10/2026 09:44:17\nAceptar")
    assert run.banner(texto) == "Información: En este momento no hay citas disponibles"
    assert run.mensaje_web(texto) == run.banner(texto)


def test_mensaje_web_de_bloqueo():
    texto = ("The requested URL was rejected. Please consult with your administrador.\n\n"
             "Your support ID is: <14917498816836265810>\n\n[Go Back]")
    assert run.banner(texto) is None
    assert run.mensaje_web(texto) == ("The requested URL was rejected. Please consult with your administrador. | "
                                      "Your support ID is: <14917498816836265810>")


def test_mensaje_web_quita_menus_y_cookies():
    texto = "Ir al contenido\nINICIO\nAYUDA\nAlgo raro pasó\nUtilizamos cookies propias...\nAcepto"
    assert run.mensaje_web(texto) == "Algo raro pasó"


def test_nombre_app_de_playwright():
    ruta = "/x/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    assert run.nombre_app(ruta) == "Google Chrome for Testing"
    assert run.nombre_app("/usr/bin/chromium") == "Chromium"


def test_hay_citas_solo_con_pantalla_de_reserva():
    assert run.hay_citas_segun("Información: En este momento no hay citas disponibles", False) is False
    assert run.hay_citas_segun("Seleccione la oficina", True) is True
    with pytest.raises(run.Inesperado, match="error del servidor"):     # la falsa alarma del 17/10 12:16
        run.hay_citas_segun("Internal Server Error\nThe server encountered an internal error", False)
    with pytest.raises(run.Inesperado, match="mira tú"):
        run.hay_citas_segun("Algo nuevo que no conozco", False)


def test_foto_por_telegram_y_respaldo_como_archivo(monkeypatch, tmp_path):
    from cita_previa import notify
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    img = tmp_path / "error.png"
    img.write_bytes(b"png")
    usados = []

    class R:
        def __init__(self, ok):
            self.ok, self.status_code = ok, 200 if ok else 400

    def post(url, **k):
        usados.append(url.rsplit("/", 1)[1])
        return R(url.endswith("sendDocument"))       # la foto falla (muy alta, 400): va como archivo, sin reintentar
    monkeypatch.setattr(notify.requests, "post", post)
    assert notify.telegram_foto(img, "pie") is True
    assert usados == ["sendPhoto", "sendDocument"]
    assert notify.telegram_foto(tmp_path / "no_existe.png") is False


def test_no_visitar_dos_veces_seguidas(tmp_path):
    path = tmp_path / "ultima_visita"
    assert run.visita_reciente(5, path) is None              # nunca hubo visita
    path.touch()
    assert run.visita_reciente(5, path) < 1                  # recién visitada: no entrar
    old = time.time() - 6 * 60
    import os
    os.utime(path, (old, old))
    assert run.visita_reciente(5, path) is None              # hace 6 min: ya se puede


def test_banner_de_paso_1_sin_dos_puntos():
    texto = ("CITA PREVIA EXTRANJERÍA\nPaso 1 de 5\nIdentidad del usuario de cita\nNOMBRE PRUEBA - X1\nInformación\n"
             "En este momento no hay citas disponibles.\nEn breve, la Oficina pondrá a su disposición nuevas citas.\n"
             "Salir")
    assert run.banner(texto) == ("Información: En este momento no hay citas disponibles. "
                                 "En breve, la Oficina pondrá a su disposición nuevas citas.")
    assert run.hay_citas_segun(texto, False) is False


def test_resumen_envio_sin_valores():
    r = run.resumen_envio("https://x/icpplus/acValidarEntrada?a=1",
                          "txtIdCitado=Z1234567W&txtDesCitado=NOMBRE+PRUEBA&txtPaisNac=PORTUGAL&token=")
    assert r == "POST acValidarEntrada: txtIdCitado(9), txtDesCitado(13), txtPaisNac(8), token(0)"
    assert "Z1234567W" not in r and "NOMBRE" not in r


def test_minimo_entre_visitas_configurable(tmp_path):
    path = tmp_path / "ultima_visita"
    path.touch()
    old = time.time() - 4.5 * 60
    import os
    os.utime(path, (old, old))
    assert run.visita_reciente(5, path) is not None      # con 5 min, a los 4,5 todavía no
    assert run.visita_reciente(4, path) is None          # con 4 min, ya se puede
