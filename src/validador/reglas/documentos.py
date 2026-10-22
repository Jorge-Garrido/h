"""Reglas sobre los documentos de la carpeta: los que faltan, los que no se pudieron leer y el OCR."""

from __future__ import annotations

from . import Contexto, Novedad, regla

NOMBRE_DOCUMENTO = {
    "HEV": "historia clínica",
    "PDE": "autorización",
    "FEV": "factura PDF",
    "PDX": "resultados de apoyo diagnóstico",
    "CUV": "respuesta de validación del Ministerio",
}


def presente(cf, documento: str) -> bool:
    """¿La carpeta trae el documento? Prefijo del PDF (FEV, HEV...), "RIPS" o "CUV"."""
    if documento == "RIPS":
        return cf.rips is not None
    if documento == "CUV":
        return cf.cuv is not None
    return cf.pdf(documento) is not None


@regla(
    "DOC.DOCUMENTOS", ambito="factura", tipo="documentos",
    # DOC.SIN_RIPS y DOC.FALTA toman la severidad de la matriz de documentos mínimos.
    severidades={
        "DOC.SOLO_PDF": "aviso", "DOC.SIN_RIPS_NI_XML": "grave", "DOC.SIN_RIPS": None, "DOC.SIN_XML": "grave", "DOC.FALTA": None,
    },
)
def documentos(ctx: Contexto):
    """Fuentes estructuradas (XML, RIPS) y documentos exigidos por la matriz de documentos mínimos."""
    cf, matriz = ctx.cf, ctx.regla_documentos
    exigidos = matriz.documentos if matriz else {}
    if not cf.rips and not cf.xml and cf.pdf("FEV"):
        origen = f" (tomada de {cf.etiquetas['FEV']})" if "FEV" in cf.etiquetas else ""
        motivo = f"sin XML ni RIPS: se validó con el PDF de la factura{origen}; no se pudo verificar el CUFE ante la DIAN"
        yield Novedad("DOC.SOLO_PDF", motivo)
    elif not cf.rips and not cf.xml:
        yield Novedad("DOC.SIN_RIPS_NI_XML", "no hay RIPS ni XML de la factura")
    elif not cf.rips and "RIPS" in exigidos:
        yield Novedad("DOC.SIN_RIPS", "sin RIPS: se usó el XML como respaldo", exigidos["RIPS"], ref="RIPS")
    elif not cf.xml:
        yield Novedad("DOC.SIN_XML", "falta el XML de la factura electrónica", ref="XML")
    for documento, severidad in exigidos.items():
        if documento == "RIPS" or presente(cf, documento):
            continue  # el RIPS ya se reportó arriba junto con el XML
        motivo = f"falta {documento} ({NOMBRE_DOCUMENTO.get(documento, documento)}); regla '{matriz.nombre}'"
        yield Novedad("DOC.FALTA", motivo, severidad, ref=documento)


@regla("EXTRACCION.ERROR", ambito="factura", tipo="extraccion", severidad="aviso")
def errores_de_extraccion(ctx: Contexto):
    """Un documento que no se pudo leer (PDF dañado, XML mal formado...)."""
    for fuente, doc in ctx.docs.items():
        for e in doc.errores:
            yield Novedad("EXTRACCION.ERROR", f"{fuente}: {e}", ref=fuente, sospechoso=fuente)


@regla(
    "OCR.ESTADO", ambito="factura", tipo="ocr",
    severidades={
        "OCR.HC_ESCANEADA": "info", "OCR.NO_CONCLUYENTE": "aviso",
        "OCR.NO_LEGIBLE": "aviso", "OCR.AUTORIZACION_PENDIENTE": "info",
    },
)
def estado_ocr(ctx: Contexto):
    """Escaneos que no se leyeron o cuya lectura no es concluyente: van a revisión humana."""
    docs = ctx.docs
    if "hc" in docs and docs["hc"].requiere_ocr:
        yield Novedad("OCR.HC_ESCANEADA", "historia clínica escaneada: identidad pendiente de OCR", ref="HEV")
    if "autorizacion_scan" in docs:
        esc = docs["autorizacion_scan"]
        for campo in ("numero_autorizacion", "paciente_id"):
            if esc.extra.get(f"no_concluyente:{campo}") and campo == "numero_autorizacion":
                yield Novedad(
                    "OCR.NO_CONCLUYENTE",
                    "autorización escaneada: lectura no concluyente del número (un solo motor); revisión humana",
                    ref=campo, sospechoso="autorizacion_scan",
                )
            if esc.extra.get(f"no_legible:{campo}"):
                yield Novedad(
                    "OCR.NO_LEGIBLE",
                    f"autorización escaneada: {campo.replace('_', ' ')} no legible (revisión humana)",
                    None if campo == "numero_autorizacion" else "info",  # el documento del paciente también está en el RIPS
                    ref=campo, sospechoso="autorizacion_scan",
                )
    elif "autorizacion_digital" in docs:
        detalle = "" if docs["autorizacion_digital"].extra.get("encabezado_digital") else " (sin encabezado digital)"
        yield Novedad("OCR.AUTORIZACION_PENDIENTE", f"autorización: número y CUPS pendientes de OCR{detalle}", ref="PDE")
