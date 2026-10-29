"""Catálogo de documentos: documentos nuevos, formatos por sistema, prefijos raros y documentos
desconocidos. Todos los datos son inventados."""

import tomllib
import typing

import openpyxl
import pymupdf
import pytest

from fabrica import NIT_EPS, NIT_IPS, Caso, carpeta
from validador import catalogo
from validador.base.config import AjusteDocumento, Config, Plantilla, ReglaDocumentos
from validador.pipeline import LECTORES, procesar_carpeta
from validador.reglas.plantillas import PLANTILLAS
from validador.salida_errores import discrepancias, escribir_excel, texto_revision

BASE = Config(documentos_minimos=[ReglaDocumentos(nombre="pruebas", documentos={"FEV": "info"})], ocr_habilitado=False)


def pdf(ruta, lineas: list[str]) -> None:
    """PDF digital con una línea de texto por renglón (etiqueta y valor en la misma línea)."""
    doc = pymupdf.open()
    p = doc.new_page()
    for i, texto in enumerate(lineas):
        p.insert_text((40, 50 + 14 * i), texto, fontsize=9)
    doc.save(ruta)


def epicrisis(c: Caso, numero_id: str | None = None, sistema: str = "A") -> list[str]:
    """Dos sistemas de historia clínica con la misma epicrisis y etiquetas distintas."""
    if sistema == "A":
        return ["EPICRISIS", f"Identificación: {c.tipo_id} {numero_id or c.numero_id}", f"Nombre del paciente: {c.nombre}",
                "Resumen de la atención con texto suficiente para tener contenido digital."]
    return ["SISTEMA B - RESUMEN DE EGRESO", f"Ident. usuario: {c.tipo_id} {numero_id or c.numero_id}",
            "Resumen de la atención con texto suficiente para tener contenido digital."]


def codigos(r) -> set[str]:
    return {h.codigo for h in r.hallazgos}


def campo(r, nombre):
    return next(c for c in r.campos if c.campo == nombre)


# --- el catálogo -------------------------------------------------------------------------------


def test_catalogo_consistente():
    cat = catalogo.get()
    assert set(Config().prioridad_fuentes) <= set(cat.fuentes)
    assert {t.lector for t in cat.pdfs()} <= set(LECTORES) | {"generico", "ninguno"}
    assert cat.documento("FVS").prefijo == "FEV"  # alias del catálogo


@pytest.mark.parametrize(
    "cambio, error",
    [
        (lambda d: d["documentos"]["EPI"].update(alias=["FVS"]), "repetido"),
        (lambda d: d["documentos"]["EPI"].update(campos=["diagnostico"]), "no existe"),
        (lambda d: d["documentos"]["EPI"].update(fuente="epicrisis_x"), "no existe"),
        (lambda d: d["documentos"]["EPI"].update(clasificar=["CUFE"]), "clasificación"),
        (lambda d: d["documentos"]["EPI"].update(fuente="hc"), "dos documentos"),
    ],
)
def test_catalogo_invalido_se_rechaza(cambio, error):
    datos = tomllib.loads(catalogo.RUTA.read_text(encoding="utf-8"))
    cambio(datos)
    with pytest.raises(ValueError, match=error):
        catalogo.Catalogo.model_validate(datos)


@pytest.mark.parametrize(
    "datos, error",
    [
        ({"documentos": {"EPI": {"alias": ["FVS"]}}}, "ya es FEV"),
        ({"documentos": {"EPI": {"alias": ["EPC"]}, "HEV": {"alias": ["EPC"]}}}, "en EPI y en HEV"),
        ({"documentos": {"EPICRISIS": {}}}, "catálogo"),
        ({"documentos": {"EPI": {"etiquetas": {"diagnostico": ["DX"]}}}}, "catálogo"),
        ({"documentos_minimos": [{"nombre": "x", "documentos": {"HCE": "grave"}}]}, "HCE"),
        ({"plantillas": [{"usar": "campo_requerido", "nombre": "abc", "campo": "nope", "documentos": ["HEV"]}]}, "nope"),
        ({"plantillas": [{"usar": "valor_permitido", "nombre": "abc", "campo": "nit_eps"}]}, "valores"),
        ({"plantillas": [{"usar": "no_existe", "nombre": "abc"}]}, "usar"),
    ],
)
def test_ajuste_invalido_se_rechaza(datos, error):
    with pytest.raises(ValueError, match=error):
        Config.model_validate(datos)


# --- documentos nuevos y formatos ---------------------------------------------------------------


def test_epicrisis_del_catalogo_vota_sin_codigo_nuevo(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c)
    pdf(d / f"EPI_{NIT_IPS}_{c.factura}.pdf", epicrisis(c, numero_id="2000002"))
    r = procesar_carpeta(d, "c1", BASE)
    pid = campo(r, "paciente_id")
    assert pid.valores_por_fuente["epicrisis"] == "CC 2000002"
    assert pid.resultado == "grave" and "epicrisis" in pid.documento_sospechoso


def test_formato_del_hospital_agrega_sus_etiquetas(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c)
    pdf(d / f"EPI_{NIT_IPS}_{c.factura}.pdf", epicrisis(c, numero_id="2000002", sistema="B"))
    # Sin el formato, "Ident. usuario" no es una etiqueta conocida: el error pasa sin verse.
    assert "epicrisis" not in campo(procesar_carpeta(d, "c1", BASE), "paciente_id").valores_por_fuente
    formato = {"nombre": "sistema-b", "detectar": ["SISTEMA B"], "etiquetas": {"paciente_id": ["IDENT USUARIO"]}}
    cfg = BASE.model_copy(update={"documentos": {"EPI": AjusteDocumento.model_validate({"formatos": [formato]})}})
    pid = campo(procesar_carpeta(d, "c1", cfg), "paciente_id")
    assert pid.valores_por_fuente["epicrisis"] == "CC 2000002" and pid.resultado == "grave"


# --- prefijos raros y documentos desconocidos ---------------------------------------------------


def test_prefijo_raro_se_reconoce_por_contenido(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c)
    pdf(d / f"EPC_{NIT_IPS}_{c.factura}.pdf", epicrisis(c))
    r = procesar_carpeta(d, "c1", BASE)
    assert "DOC.PREFIJO_INESPERADO" in codigos(r) and "epicrisis" in campo(r, "paciente_id").valores_por_fuente
    # El hospital declara el alias: deja de ser inesperado.
    cfg = BASE.model_copy(update={"documentos": {"EPI": AjusteDocumento(alias=["EPC"])}})
    r = procesar_carpeta(d, "c2", cfg)
    assert "DOC.PREFIJO_INESPERADO" not in codigos(r) and "epicrisis" in campo(r, "paciente_id").valores_por_fuente


def test_pdf_sin_prefijo_se_reconoce_por_contenido(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c, hc={})
    pdf(d / "epicrisis paciente.pdf", epicrisis(c))
    # Sin factura PDF, se trata como PDF unificado y se separa por páginas.
    r = procesar_carpeta(d, "c1", BASE)
    assert "epicrisis" in campo(r, "paciente_id").valores_por_fuente and "EPI" in r.documentos_presentes
    # Con factura PDF, es un documento suelto: se reconoce por su primera página.
    pdf(d / f"FEV_{NIT_IPS}_{c.factura}.pdf", ["FACTURA ELECTRONICA DE VENTA No. FE0000001", "Texto de relleno de la factura."])
    r = procesar_carpeta(d, "c2", BASE)
    assert "epicrisis" in campo(r, "paciente_id").valores_por_fuente
    assert next(h for h in r.hallazgos if h.codigo == "DOC.PREFIJO_INESPERADO").ref == "UNI>EPI"


def test_documento_desconocido_verifica_identidad_sin_votar(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c)
    pdf(d / f"XYZ_{NIT_IPS}_{c.factura}.pdf", ["CONSTANCIA", "Identificación: CC 2000002", "Texto de relleno para el PDF."])
    r = procesar_carpeta(d, "c1", BASE)
    pid = campo(r, "paciente_id")
    assert "doc_XYZ" not in pid.valores_por_fuente and pid.resultado == "ok"  # no vota
    otro = next(h for h in r.hallazgos if h.codigo == "DOC.OTRO_PACIENTE")
    assert otro.ref == "XYZ" and otro.documento_sospechoso == "doc_XYZ" and "1000001" not in (otro.ref or "")
    assert "DOC.DESCONOCIDO" in codigos(r)


def test_prefijo_del_catalogo_con_contenido_de_otro_documento(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c)
    pdf(d / f"HEV_{NIT_IPS}_{c.factura}.pdf", ["FACTURA ELECTRONICA DE VENTA No. FE0000001", "CUFE: abc", "Texto de relleno."])
    r = procesar_carpeta(d, "c1", BASE)
    h = next(h for h in r.hallazgos if h.codigo == "DOC.CONTENIDO_NO_COINCIDE")
    assert h.ref == "HEV>FEV" and h.resultado == "aviso"
    assert "DOC.CONTENIDO_NO_COINCIDE" not in codigos(procesar_carpeta(carpeta(tmp_path / "ok", c, hc={}), "c2", BASE))


# --- reportes ---------------------------------------------------------------------------------


def test_excel_y_revision_muestran_la_fuente_nueva(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c)
    pdf(d / f"EPI_{NIT_IPS}_{c.factura}.pdf", epicrisis(c, numero_id="2000002"))
    r = procesar_carpeta(d, "c1", BASE)
    revision = texto_revision(r, discrepancias(r))
    assert "Epicrisis" in revision and f"EPI_{NIT_IPS}_{c.factura}.pdf" in revision
    escribir_excel([r], tmp_path / "e.xlsx", "EPS", "c1", set())
    encabezados = [x.value for x in openpyxl.load_workbook(tmp_path / "e.xlsx")["Discrepancias"][1]]
    assert "Epicrisis" in encabezados


# --- plantillas -------------------------------------------------------------------------------


def test_cada_plantilla_tiene_implementacion():
    variantes = typing.get_args(typing.get_args(Plantilla)[0])
    assert {typing.get_args(v.model_fields["usar"].annotation)[0] for v in variantes} == set(PLANTILLAS)


def test_plantillas(tmp_path):
    c = Caso()
    d = carpeta(tmp_path, c)
    pdf(d / f"EPI_{NIT_IPS}_{c.factura}.pdf", epicrisis(c))
    plantillas = [
        {"usar": "valor_permitido", "nombre": "eps-con-contrato", "campo": "nit_eps", "valores": ["111111111"],
         "severidad": "grave"},
        {"usar": "campo_requerido", "nombre": "egreso-en-epicrisis", "campo": "fecha_egreso", "documentos": ["EPI"]},
        {"usar": "campo_requerido", "nombre": "id-en-epicrisis", "campo": "paciente_id", "documentos": ["EPI"]},
    ]
    r = procesar_carpeta(d, "c1", Config.model_validate(BASE.model_dump() | {"plantillas": plantillas}))
    por_ref = {h.ref: h for h in r.hallazgos if h.codigo.startswith("PLANTILLA.")}
    assert set(por_ref) == {"eps-con-contrato", "egreso-en-epicrisis"}
    assert por_ref["eps-con-contrato"].resultado == "grave" and NIT_EPS in por_ref["eps-con-contrato"].motivo
    assert r.estado == "grave"
