"""Esperas del flujo contra páginas locales que imitan una web lenta (Chromium headless, sin red)."""
import time

import pytest

from cita_previa import run

playwright = pytest.importorskip("playwright.sync_api")

PAGINAS = {
    # el botón aparece a los 1,5 s, como cuando la web va lenta
    "/lento": "<button id='b' style='display:none' onclick=\"location.href='/siguiente'\">Ir</button>"
              "<script>setTimeout(() => document.getElementById('b').style.display='inline', 1500)</script>",
    # el HTML llega enseguida pero una imagen no termina nunca: el evento "load" no llega
    "/siguiente": "<p id='listo'>Página nueva</p><img src='/cuelga.png'>",
    "/caducada": "<p>Su sesión ha caducado por permanecer demasiado tiempo inactiva.</p>",
    "/oculta": "<p style='display:none'>Su sesión ha caducado</p><button id='b'>Ir</button>",
    "/error500": "<h1>Internal Server Error</h1>",
    # botón deshabilitado que se habilita a los 1,5 s: no se pulsa antes (element_to_be_clickable)
    "/deshabilitado": "<button id='b' disabled onclick=\"location.href='/siguiente'\">Ir</button>"
                      "<script>setTimeout(() => document.getElementById('b').disabled = false, 1500)</script>",
    # el primero de la lista está oculto: hay que pulsar el visible
    "/dos": "<button id='btnEntrar' style='display:none'>Oculto</button>"
            "<input type='button' value='Presentación sin Cl@ve' onclick=\"location.href='/siguiente'\">",
    "/form": "<form method='post' action='/siguiente'><input name='txtIdCitado' value='Z1234567W'>"
             "<button>Enviar</button></form>",
    # como la web real: comprobarDatos() solo corre al salir del campo (onchange)
    "/onchange": "<input id='txtDesCitado' onchange=\"document.title='comprobado'\"><select id='txtPaisNac'>"
                 "<option>Seleccionar ...</option><option>PORTUGAL</option></select>",
    # formulario con datos "especiales" que el código no conoce
    "/especial": "<label for='txtFecha' class='required'>Fecha de caducidad</label><input id='txtFecha'>"
                 "<select id='txtTipo' required><option value=''>Seleccionar ...</option><option value='R'>RENOVACION"
                 "</option></select><input type='checkbox' id='chkAcepto' required>"
                 "<input type='radio' name='rdbSexo' value='H' required><input type='radio' name='rdbSexo' value='M'>"
                 "<input id='txtOpcional'>",
    "/sincitas": "<p>Información: En este momento no hay citas disponibles</p>",
    # Recorrido tras "sin Cl@ve", copiado de las capturas reales (19/10)
    "/icpplus/acEntrada": "<input id='txtIdCitado'><input id='txtDesCitado'>"
                          "<select id='txtPaisNac'><option>Seleccionar ...</option><option>PORTUGAL</option></select>"
                          "<button id='btnEnviar' onclick=\"location.href='/icpplus/acValidarEntrada?' + "
                          "document.getElementById('txtIdCitado').value\">Aceptar</button>",
    "/icpplus/acValidarEntrada?X1": "<p>A continuación puedes seleccionar cualquiera de las siguientes opciones</p>"
                                    "<button id='btnEnviar' onclick=\"location.href='/icpplus/acCitar'\">Solicitar Cita"
                                    "</button><button>Consultar Citas Confirmadas</button><button>Salir</button>",
    "/icpplus/acValidarEntrada?X2": "<p>Información: En este momento no hay citas disponibles</p><p>0017</p>"
                                    "<button id='btnAceptar'>Aceptar</button>",
    # mismo menú pero el botón sin id: debe encontrarlo por el texto
    "/icpplus/acValidarEntrada?X3": "<input type='button' value='Solicitar Cita' "
                                    "onclick=\"location.href='/icpplus/acCitarHay'\">",
    "/icpplus/acCitar": "<p>Paso 1 de 5</p><p>En este momento no hay citas disponibles.</p>"
                        "<p>En breve, la Oficina pondrá a su disposición nuevas citas.</p><button>Salir</button>",
    "/icpplus/acCitarHay": "<p>Paso 1 de 5</p><select id='idSede'><option>Oficina 1</option></select>",
}


@pytest.fixture(scope="module")
def page():
    with playwright.sync_playwright() as p:
        nav = p.chromium.launch()
        ctx = nav.new_context()

        def servir(route):
            ruta = "/" + route.request.url.split("/", 3)[3]
            if ruta == "/cuelga.png":
                return                       # nunca responde
            route.fulfill(content_type="text/html; charset=utf-8", body=PAGINAS.get(ruta, "<p>404</p>"))
        ctx.route("http://prueba.local/**", servir)
        yield ctx.new_page()
        nav.close()


@pytest.fixture(autouse=True)
def capturas_aparte(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "SHOTS", tmp_path)      # no tocar data/ultima/ de verdad


def flujo(page, ruta):
    page.goto(f"http://prueba.local{ruta}", wait_until="domcontentloaded")
    return run.Flujo(page, {})


def test_espera_al_boton_y_no_al_evento_load(page):
    f = flujo(page, "/lento")
    inicio = time.time()
    assert f.pulsar("#b") is True                         # esperó al botón tardío
    assert f.esperar("#listo", timeout=5_000)              # y no se quedó colgado por la imagen
    assert time.time() - inicio < 10


def test_sesion_caducada_corta_enseguida(page):
    f = flujo(page, "/caducada")
    inicio = time.time()
    with pytest.raises(run.SesionCaducada):
        f.pulsar("#btnEntrar")
    assert time.time() - inicio < 5                       # antes agotaba 30 s


def test_texto_oculto_de_caducada_no_cuenta(page):
    assert flujo(page, "/oculta").esperar("#b", timeout=3_000)


def test_error_del_servidor_corta_enseguida(page):
    with pytest.raises(run.Inesperado, match="error del servidor"):
        flujo(page, "/error500").esperar("#txtIdCitado", timeout=20_000)


def test_sin_citas_no_espera_ni_pulsa(page):
    f = flujo(page, "/sincitas")
    assert f.pulsar("#btnEnviar") is False
    assert f.esperar("#txtIdCitado") is False


def test_si_no_aparece_avisa_con_el_selector(page):
    with pytest.raises(run.Inesperado, match="No apareció #nada"):
        flujo(page, "/siguiente").esperar("#nada", timeout=1_500)
    assert flujo(page, "/siguiente").esperar("#nada", timeout=1_000, obligatorio=False) is False


def recorrido(page, nie):
    f = flujo(page, "/icpplus/acEntrada")
    f.cfg = {"nie": nie, "nombre": "NOMBRE PRUEBA", "pais": "Portugal"}
    return f, f.hay_citas()


def test_tu_camino_solicitar_cita_y_paso_1_sin_citas(page):
    f, hay = recorrido(page, "X1")
    assert hay is False and "solicitar" in f.pasos and page.url.endswith("/acCitar")


def test_recuadro_0017_no_pulsa_nada(page):
    f, hay = recorrido(page, "X2")
    assert hay is False and "solicitar" not in f.pasos


def test_solicitar_cita_por_texto_y_hay_citas(page):
    f, hay = recorrido(page, "X3")
    assert hay is True and "solicitar" in f.pasos


def test_espera_a_que_el_boton_este_habilitado(page):
    f = flujo(page, "/deshabilitado")
    inicio = time.time()
    assert f.pulsar("#b") is True and page.url.endswith("/siguiente")
    assert time.time() - inicio >= 1.4                    # no pulsó mientras estaba deshabilitado


def test_pulsa_el_visible_aunque_el_primero_este_oculto(page):
    f = flujo(page, "/dos")
    assert f.pulsar(run.SIN_CLAVE) is True and page.url.endswith("/siguiente")


def test_anota_ids_de_botones(page, capsys):
    flujo(page, "/icpplus/acValidarEntrada?X1").anotar_botones("opciones")
    out = capsys.readouterr().out
    assert "#btnEnviar «Solicitar Cita»" in out and "#(sin id) «Salir»" in out


def test_guarda_html_y_campos_enviados(page, tmp_path):
    page.on("request", run.guardar_envio)
    page.goto("http://prueba.local/form", wait_until="domcontentloaded")
    f = run.Flujo(page, {})
    f.foto("form")
    assert "<form" in (tmp_path / "01_form.html").read_text()
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click("button")
    assert "POST siguiente: txtIdCitado(9)" in (tmp_path / "envios.txt").read_text()


def test_rellenar_dispara_onchange_como_una_persona(page):
    f = flujo(page, "/onchange")
    f.rellenar("#txtDesCitado", "NOMBRE PRUEBA")
    assert page.title() == "comprobado"                  # comprobarDatos() se ejecutó


def test_campos_extra_rellenan_lo_que_el_codigo_no_conoce(page):
    f = flujo(page, "/especial")
    f.cfg = {"campos_extra": {"txtFecha": "31/12/2026", "txtTipo": "RENOVACION", "chkAcepto": True, "rdbSexo": "H",
                              "noExiste": "x"}}
    f.preparar_envio()                                           # no queda ningún obligatorio vacío
    assert page.input_value("#txtFecha") == "31/12/2026" and page.input_value("#txtTipo") == "R"
    assert page.is_checked("#chkAcepto") and page.is_checked("input[value=H]")
    assert page.input_value("#txtOpcional") == ""               # lo que no está en la config no se toca


def test_obligatorio_desconocido_para_y_dice_cual(page):
    f = flujo(page, "/especial")
    f.cfg = {"campos_extra": {"txtTipo": "R", "chkAcepto": True, "rdbSexo": "M"}}
    with pytest.raises(run.CampoDesconocido) as e:
        f.preparar_envio()
    assert "Fecha de caducidad (#txtFecha)" in str(e.value) and "txtTipo" not in str(e.value)


def test_pregunta_por_telegram_los_campos_que_no_conoce_y_los_recuerda(page, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "falso")
    enviados, respuestas = [], iter(["31/12/2026", "1", "sí", "2"])   # fecha, desplegable, casilla, opción
    monkeypatch.setattr(run.notify, "telegram", lambda texto, **k: enviados.append(texto) or True)
    monkeypatch.setattr(run.notify, "telegram_foto", lambda *a, **k: True)
    monkeypatch.setattr(run.notify, "sound", lambda: None)

    def responder(minutos, valida):
        r = next(respuestas)
        assert valida(r)
        return r
    monkeypatch.setattr(run, "esperar_respuesta_telegram", responder)
    f = flujo(page, "/especial")
    f.preparar_envio()                                           # pregunta los 4, los rellena y sigue
    assert page.input_value("#txtFecha") == "31/12/2026" and page.input_value("#txtTipo") == "R"
    assert page.is_checked("#chkAcepto") and page.is_checked("input[value=M]")
    preguntas = [t for t in enviados if t.startswith("❓")]
    assert len(preguntas) == 4 and "Fecha de caducidad" in preguntas[0] and "1. RENOVACION" in preguntas[1]
    assert run.leer_campos_aprendidos() == {"txtFecha": "31/12/2026", "txtTipo": "RENOVACION",
                                            "chkAcepto": True, "rdbSexo": "M"}
    enviados.clear()
    flujo(page, "/especial").preparar_envio()                    # la próxima vez ya no pregunta
    assert not [t for t in enviados if t.startswith("❓")]


def test_sin_respuesta_se_para_sin_enviar(page, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "falso")
    monkeypatch.setattr(run.notify, "telegram", lambda *a, **k: True)
    monkeypatch.setattr(run.notify, "telegram_foto", lambda *a, **k: True)
    monkeypatch.setattr(run.notify, "sound", lambda: None)
    monkeypatch.setattr(run, "esperar_respuesta_telegram", lambda minutos, valida: None)
    f = flujo(page, "/especial")
    f.cfg = {"campos_extra": {"txtTipo": "R", "chkAcepto": True, "rdbSexo": "M"}}
    with pytest.raises(run.CampoDesconocido, match=r"Fecha de caducidad \(#txtFecha\)"):
        f.preparar_envio()
