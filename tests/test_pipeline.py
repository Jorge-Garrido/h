"""Casos de regresión de la sección 9 del plan que se pueden cubrir sin OCR."""

import pytest
from fabrica import NIT_EPS, Caso, carpeta

from validador.base.config import Config, ReglaDocumentos
from validador.pipeline import procesar_carpeta
from validador.base.schemas import CampoExtraido
from validador.severity import evaluar_campo
from validador.store import db


@pytest.fixture
def cfg():
    # Las carpetas sintéticas no traen PDFs de factura.
    return Config(documentos_minimos=[ReglaDocumentos(nombre="pruebas", documentos={"RIPS": "info", "CUV": "info"})])


def campo(resultado, nombre):
    return next(c for c in resultado.campos if c.campo == nombre)


def test_caso_limpio_sin_graves(tmp_path, cfg):
    r = procesar_carpeta(carpeta(tmp_path, Caso(), hc={}), "c1", cfg)
    assert r.fuente_principal == "rips"
    assert r.estado in ("ok", "info"), [(c.campo, c.motivo) for c in r.campos if c.resultado != "ok"]
    assert campo(r, "paciente_nombre").resultado == "ok"  # mismo nombre en otro orden
    assert r.lineas[0].valor_por_fuente == {"rips": 214000, "xml": 214000}


def test_sin_rips_usa_xml_como_respaldo(tmp_path, cfg):
    r = procesar_carpeta(carpeta(tmp_path, Caso(), rips=False), "c1", cfg)
    assert r.fuente_principal == "xml"
    assert any("respaldo" in h.motivo for h in r.hallazgos)
    assert campo(r, "cufe").valor_final is not None


def test_cedula_con_un_digito_distinto_entre_xml_y_hc_es_grave(tmp_path, cfg):
    r = procesar_carpeta(carpeta(tmp_path, Caso(), hc={"numero_id": "1000007"}), "c1", cfg)
    c = campo(r, "paciente_id")
    assert c.resultado == "grave"
    assert c.documento_sospechoso == "hc"
    assert c.distancia == 1
    assert r.estado == "grave"
    assert any(p.documento == "hc" for p in r.propuestas_correccion)


def test_autorizacion_distinta_en_rips(tmp_path, cfg):
    r = procesar_carpeta(carpeta(tmp_path, Caso(), cambios_rips={"autorizacion": "17468248"}), "c1", cfg)
    c = campo(r, "numero_autorizacion")
    assert c.resultado == "grave" and c.documento_sospechoso == "rips" and c.distancia == 1


def test_escaneo_mal_leido_y_digitales_coinciden_no_es_grave(cfg):
    # Regresión del plan: un dígito mal leído por el OCR (lectura de un solo motor).
    extraidos = [
        CampoExtraido(campo="numero_autorizacion", valor="17458248", fuente="xml"),
        CampoExtraido(campo="numero_autorizacion", valor="17458248", fuente="pdf_factura"),
        CampoExtraido(campo="numero_autorizacion", valor="17468248", fuente="autorizacion_scan", etapa="paddle", confianza=0.6),
    ]
    r = evaluar_campo("numero_autorizacion", extraidos, cfg)
    assert r.resultado == cfg.si_ocr_incierto_difiere
    assert r.documento_sospechoso == "autorizacion_scan"
    assert r.valor_final == "17458248"


def test_escaneo_confiable_es_la_autoridad_para_la_autorizacion(cfg):
    extraidos = [
        CampoExtraido(campo="numero_autorizacion", valor="3171699530", fuente="xml"),
        CampoExtraido(campo="numero_autorizacion", valor="3171699530", fuente="rips"),
        CampoExtraido(campo="numero_autorizacion", valor="317169530", fuente="autorizacion_scan", etapa="tesseract", confianza=0.9),
    ]
    r = evaluar_campo("numero_autorizacion", extraidos, cfg)
    assert r.resultado == "grave" and r.valor_final == "317169530"
    assert set(r.documento_sospechoso.split(",")) == {"xml", "rips"}


def test_lectura_ocr_con_formato_imposible_se_ignora(cfg):
    extraidos = [
        CampoExtraido(campo="paciente_id", valor="CC 1000001", fuente="xml"),
        CampoExtraido(campo="paciente_id", valor="CC 1000001", fuente="rips"),
        CampoExtraido(campo="paciente_id", valor="CC 13", fuente="autorizacion_scan", confianza=0.9),
    ]
    assert evaluar_campo("paciente_id", extraidos, cfg).resultado == "ok"


def test_formato_invalido_en_la_mayoria_es_grave(cfg):
    extraidos = [
        CampoExtraido(campo="numero_autorizacion", valor="2I0100300", fuente="xml"),
        CampoExtraido(campo="numero_autorizacion", valor="2I0100300", fuente="pdf_factura"),
        CampoExtraido(campo="numero_autorizacion", valor="210100300", fuente="rips"),
    ]
    r = evaluar_campo("numero_autorizacion", extraidos, cfg)
    assert r.resultado == "grave" and "formato inválido" in r.motivo


def test_codigos_dian_recurrentes_son_info(tmp_path, cfg):
    r = procesar_carpeta(carpeta(tmp_path, Caso()), "c1", cfg)
    dian = [h for h in r.hallazgos if h.tipo == "dian"]
    assert {h.resultado for h in dian} == {"info"}
    assert r.dian["avisos"] == ["FAJ43b", "RUT01"]


def test_tipo_de_consulta_distinto_en_hc_es_aviso(tmp_path, cfg):
    r = procesar_carpeta(carpeta(tmp_path, Caso(), hc={"tipo_consulta": "Control"}), "c1", cfg)
    assert any(h.tipo == "hc" and h.resultado == "aviso" for h in r.hallazgos)


def test_duplicado_por_cufe(tmp_path, cfg):
    engine = db.conectar(f"sqlite:///{tmp_path / 'v.db'}")
    ruta = carpeta(tmp_path, Caso())
    for corrida in ("c1", "c2"):
        db.iniciar_corrida(engine, corrida, "test")
        r = procesar_carpeta(ruta, corrida, cfg, engine=engine)
        db.guardar_resultado(engine, r)
    assert any(h.tipo == "duplicado" and h.resultado == "info" for h in r.hallazgos)

    otra = Caso(factura="FE0000002")  # mismo CUFE (de la fábrica), otra factura
    db.iniciar_corrida(engine, "c3", "test")
    r = procesar_carpeta(carpeta(tmp_path, otra), "c3", cfg, engine=engine)
    assert any(h.tipo == "duplicado" and h.resultado == "grave" for h in r.hallazgos)


def test_duplicado_por_cufe_en_la_misma_corrida(tmp_path, cfg):
    engine = db.conectar(f"sqlite:///{tmp_path / 'v.db'}")
    db.iniciar_corrida(engine, "c1", "test")
    primera = procesar_carpeta(carpeta(tmp_path / "a", Caso()), "c1", cfg, engine=engine)
    db.guardar_resultado(engine, primera)
    # Mismo CUFE (de la fábrica), otra factura, mismo lote.
    r = procesar_carpeta(carpeta(tmp_path / "b", Caso(factura="FE0000002")), "c1", cfg, engine=engine)
    assert any(h.tipo == "duplicado" and h.resultado == "grave" for h in r.hallazgos)
    assert r.estado == "grave"


def test_factura_que_falla_queda_en_los_reportes(tmp_path):
    from validador.pipeline import resultado_fallido
    from validador.salida_errores import discrepancias

    r = resultado_fallido(tmp_path, "c1", ValueError("detalle con datos del paciente"))
    assert r.estado == "grave" and r.factura == tmp_path.name
    assert "ValueError" in r.hallazgos[0].motivo and "paciente" not in r.hallazgos[0].motivo
    assert discrepancias(r)  # se copia a CON ERROR y sale en el Excel


def test_resultado_serializa_a_json(tmp_path, cfg):
    r = procesar_carpeta(carpeta(tmp_path, Caso()), "c1", cfg)
    datos = r.model_dump(mode="json")
    assert datos["schema_version"] == "1" and datos["estado"] in ("ok", "info", "aviso", "grave")


def test_pdf_unificado_se_separa_por_paginas(tmp_path):
    import pymupdf

    from validador.ingest import unificado

    doc = pymupdf.open()
    for texto in (
        "FACTURA ELECTRÓNICA DE VENTA No.FE0000009\nCUFE: abc",
        "Consultar Ambulatorio\n#1234567 - Por asignación de cita\nPortal Salud",
        "HISTORIA CLÍNICA\nTipo y número de identificación: CC 1000001",
        "continuación de la historia con suficiente texto para no ser escaneo, notas del médico.",
    ):
        doc.new_page().insert_text((40, 60), texto, fontsize=10)
    ruta = tmp_path / "FE0000009.pdf"
    doc.save(ruta)
    assert unificado.clasificar_paginas(ruta) == ["FEV", "PDE", "HEV", "HEV"]
    partes = unificado.separar(ruta, tmp_path / "sep")
    assert set(partes) == {"FEV", "PDE", "HEV"}
    assert partes["HEV"][1] == "FE0000009.pdf, págs. 3-4"


def test_regla_de_documentos_mas_especifica():
    general = ReglaDocumentos(nombre="general", documentos={"FEV": "grave"})
    proc = ReglaDocumentos(nombre="procedimientos", servicios=["procedimientos"], documentos={"PDX": "grave"})
    eps = ReglaDocumentos(nombre="eps", eps=[NIT_EPS], documentos={"HEV": "grave"})
    eps_proc = ReglaDocumentos(nombre="eps+proc", eps=[NIT_EPS], servicios=["procedimientos"], documentos={})
    cfg = Config(documentos_minimos=[general, proc, eps, eps_proc])
    assert cfg.regla_documentos("111", {"consultas"}).nombre == "general"
    assert cfg.regla_documentos("111", {"consultas", "procedimientos"}).nombre == "procedimientos"
    assert cfg.regla_documentos(NIT_EPS, {"consultas"}).nombre == "eps"
    assert cfg.regla_documentos(NIT_EPS, {"procedimientos"}).nombre == "eps+proc"
    assert Config(documentos_minimos=[eps]).regla_documentos("111", set()) is None


def test_documento_minimo_faltante_segun_la_eps(tmp_path):
    cfg = Config(documentos_minimos=[ReglaDocumentos(nombre="exigente", eps=[NIT_EPS], documentos={"HEV": "grave"})])
    r = procesar_carpeta(carpeta(tmp_path, Caso()), "c1", cfg)  # la carpeta no trae HEV
    assert any(h.tipo == "documentos" and h.resultado == "grave" and "exigente" in h.motivo for h in r.hallazgos)
    assert r.documentos_faltantes == ["HEV"]


def test_tarifa_por_linea_con_el_mismo_cups_y_tarifas_distintas(tmp_path, cfg):
    from decimal import Decimal

    from validador.tarifario.importer import ItemTarifa
    from validador.tarifario.matcher import Tarifario

    items = [
        ItemTarifa("anexo", "ACD015", "998702", "SEDACION PARA PROCEDIMIENTO DE RESONANCIA MAGNETICA", Decimal("49.14"), Decimal(595100), 1),
        ItemTarifa("anexo", "ACD279", "998702", "SEDACION PARA PROCEDIMIENTO DE TOMOGRAFIA AXIAL COMPUTARIZADA", Decimal("39.22"), Decimal(475000), 2),
    ]
    caso = Caso(lineas=[
        ("998702", "SEDACION PARA PROCEDIMIENTO DE RESONANCIA MAGNETICA", 1, 595100, "procedimientos", "998702"),
        ("998702", "SEDACION PARA PROCEDIMIENTO DE TOMOGRAFIA AXIAL COMPUTARIZADA", 1, 475000, "procedimientos", "998702"),
    ])
    r = procesar_carpeta(carpeta(tmp_path, caso), "c1", cfg, Tarifario(items))
    linea = next(l for l in r.lineas if l.codigo == "998702")
    # Sumadas darían un unitario de 535.050, que no es ninguna de las dos tarifas.
    assert linea.tarifa.estado == "coincide", linea.motivos
