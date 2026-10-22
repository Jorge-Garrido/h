from fabrica import Caso, carpeta
from validador import reglas
from validador.base.config import AjusteRegla, Config, ReglaDocumentos
from validador.base.motivos import MOTIVOS
from validador.pipeline import procesar_carpeta

# Códigos que no salen de una regla del registro: los campos (severity.py) y los de la corrida.
FUERA_DEL_REGISTRO = ("CAMPO.", "DUPLICADO.", "PROCESAMIENTO.", "REGLA.")
EXIGE_HC = [ReglaDocumentos(nombre="exige HC", documentos={"HEV": "grave"})]


def _codigos(r):
    return {h.codigo for h in r.hallazgos} | {c for l in r.lineas for c in l.codigos}


def test_cada_codigo_lo_declara_una_sola_regla():
    declarados = [c for r in reglas.REGISTRO.values() for c in r.severidades]
    assert len(declarados) == len(set(declarados))
    assert set(declarados) == {c for c in MOTIVOS if not c.startswith(FUERA_DEL_REGISTRO)}


def test_desactivar_un_codigo_por_configuracion(tmp_path):
    d = carpeta(tmp_path, Caso())  # sin HC
    activa = procesar_carpeta(d, "c1", Config(documentos_minimos=EXIGE_HC))
    apagada = procesar_carpeta(d, "c1", Config(documentos_minimos=EXIGE_HC, reglas={"DOC.FALTA": AjusteRegla(activa=False)}))
    assert "DOC.FALTA" in _codigos(activa) and activa.estado == "grave"
    assert "DOC.FALTA" not in _codigos(apagada) and apagada.estado != "grave"


def test_cambiar_la_severidad_de_un_codigo(tmp_path):
    d = carpeta(tmp_path, Caso(), hc={"tipo_consulta": "Control"})  # se factura primera vez
    base = Config(documentos_minimos=EXIGE_HC)
    por_defecto = procesar_carpeta(d, "c1", base)
    ajuste = {"HC.TIPO_CONSULTA_DIFIERE": AjusteRegla(severidad="grave")}
    ajustada = procesar_carpeta(d, "c1", base.model_copy(update={"reglas": ajuste}))
    sev = lambda r: next(h.resultado for h in r.hallazgos if h.codigo == "HC.TIPO_CONSULTA_DIFIERE")  # noqa: E731
    assert sev(por_defecto) == "aviso" and sev(ajustada) == "grave"


def test_una_regla_que_falla_no_detiene_la_factura(tmp_path, monkeypatch):
    def rota(ctx):
        raise RuntimeError("dato con la cédula 1000001")  # el detalle del error no debe salir

    def no_declara(ctx):
        yield reglas.Novedad("HC.FECHA_FUERA_PERIODO", "x")

    monkeypatch.setitem(reglas.REGISTRO, "PRUEBA.ROTA", reglas.Regla("PRUEBA.ROTA", "factura", "hc", {}, "", rota))
    monkeypatch.setitem(reglas.REGISTRO, "PRUEBA.MAL", reglas.Regla("PRUEBA.MAL", "factura", "hc", {}, "", no_declara))
    r = procesar_carpeta(carpeta(tmp_path, Caso()), "c1", Config(documentos_minimos=EXIGE_HC))
    errores = [h for h in r.hallazgos if h.codigo == "REGLA.ERROR"]
    assert {h.ref for h in errores} == {"PRUEBA.ROTA", "PRUEBA.MAL"} and all(h.tipo == "regla" for h in errores)
    assert not any("1000001" in h.motivo for h in errores)
    assert "DOC.FALTA" in _codigos(r)  # el resto de las reglas sí corrió
