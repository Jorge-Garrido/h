"""Orquesta la validación de una factura: extracción digital (A) y cruce (B).

La etapa C (OCR en cascada) llega en la Fase 2; por ahora las fuentes escaneadas se
marcan en ``requiere_ocr``.

Fuente principal: el RIPS (JSON) que genera el sistema del hospital. Si no está,
el XML de la factura electrónica hace de respaldo.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.engine import Engine

from . import reglas
from .base.config import Config, ResolutorConfig
from .base.schemas import (
    DocumentoExtraido,
    Hallazgo,
    LineaServicio,
    ResultadoCampo,
    ResultadoFactura,
    ResultadoLinea,
)
from .corrections.adapter import proponer
from .data_preprocessing import normalize
from .ingest import (
    autorizacion_header,
    cuv,
    discover,
    factura_pdf_parser,
    generico,
    hc_parser,
    rips,
    unificado,
    xml_parser,
)
from .reglas.documentos import presente
from .severity import evaluar_campo, peor
from .store import db
from .tarifario.matcher import Tarifario

log = logging.getLogger(__name__)

FUENTES_LINEAS = ("rips", "xml", "pdf_factura")


def esperados(docs: dict[str, DocumentoExtraido]) -> dict[str, set[str]]:
    """Valores de las fuentes estructuradas (RIPS, XML) que se buscarán en los PDF.
    Sin RIPS ni XML, la referencia es lo que se leyó en el PDF de la factura."""
    valores: dict[str, set[str]] = defaultdict(set)
    fuentes = ("rips", "xml", "xml_nota") if ("rips" in docs or "xml" in docs) else ("pdf_factura",)
    for fuente in fuentes:
        for nombre, c in docs.get(fuente, DocumentoExtraido(fuente="carpeta")).campos.items():
            if c.valor:
                valores[nombre].add(c.valor)
    return valores


def extraer(cf: discover.CarpetaFactura, cfg: Config | None = None) -> dict[str, DocumentoExtraido]:
    docs: dict[str, DocumentoExtraido] = {}
    if cf.pdfs.get("UNI") and not cf.pdf("FEV"):
        destino = Path(cfg.ocr_cache_dir).parent / "unificados" if cfg else Path("salidas/unificados")
        for prefijo, (ruta, etiqueta) in unificado.separar(cf.pdfs["UNI"][0], destino).items():
            if not cf.pdf(prefijo):
                cf.pdfs[prefijo] = [ruta]
                cf.etiquetas[prefijo] = etiqueta
    carpeta = DocumentoExtraido(fuente="carpeta", archivo=cf.ruta.name)
    carpeta.poner("numero_factura", normalize.codigo(cf.factura))
    docs["carpeta"] = carpeta
    if cf.rips:
        docs["rips"] = rips.extraer(cf.rips)
    if cf.cuv:
        docs["cuv"] = cuv.extraer(cf.cuv)
    if cf.xml:
        x, nota = xml_parser.extraer(cf.xml)
        docs["xml"] = x
        docs["xml_nota"] = nota
    rutas = {}
    if ruta := cf.pdf("FEV"):
        docs["pdf_factura"] = factura_pdf_parser.extraer(ruta)
        rutas["pdf_factura"] = ruta
    if ruta := cf.pdf("HEV"):
        docs["hc"] = hc_parser.extraer(ruta)
        rutas["hc"] = ruta
    if ruta := cf.pdf("PDE"):
        docs["autorizacion_digital"] = autorizacion_header.extraer(ruta)
        rutas["autorizacion_digital"] = ruta
    if cfg is None or cfg.extraccion_pdf == "generica":
        # Los parsers de plantilla solo aportan las líneas de la factura y las notas de la HC.
        valores = esperados(docs)
        if "pdf_factura" in rutas:
            generico.completar(docs["pdf_factura"], rutas["pdf_factura"], valores)
            valores = esperados(docs)  # sin RIPS/XML, ahora la referencia es el PDF de la factura
        for fuente, ruta in rutas.items():
            if fuente != "pdf_factura":
                generico.completar(docs[fuente], ruta, valores)
    if cfg is not None and cfg.ocr_habilitado and (ruta := cf.pdf("PDE")):
        # Import diferido: el OCR (OpenCV, RapidOCR) es un extra opcional; sin él, --sin-ocr funciona.
        from .ingest import autorizacion_ocr

        valores = esperados(docs)
        cups: dict[str, str | None] = {}
        for f in ("xml", "pdf_factura", "rips"):
            for l in docs[f].lineas if f in docs else []:
                if l.codigo and re.fullmatch(r"\d{6}", l.codigo):
                    cups[l.codigo] = cups.get(l.codigo) or l.descripcion
        docs["autorizacion_scan"] = autorizacion_ocr.extraer(ruta, valores, cups, Path(cfg.ocr_cache_dir) if cfg.ocr_cache_dir else None)
        docs["autorizacion_digital"].requiere_ocr = False
    return docs


def _fuente_principal(docs: dict[str, DocumentoExtraido]) -> str:
    if "rips" in docs and docs["rips"].lineas:
        return "rips"
    if "xml" in docs and docs["xml"].campos:
        return "xml"
    if "pdf_factura" in docs and docs["pdf_factura"].campos:
        return "pdf_factura"
    return "ninguna"


def _periodo(campos: list[ResultadoCampo]) -> tuple[date | None, date | None]:
    por_nombre = {c.campo: c.valor_final for c in campos}
    return normalize.fecha(por_nombre.get("fecha_ingreso")), normalize.fecha(por_nombre.get("fecha_egreso"))


def _agrupar(lineas: list[LineaServicio]) -> dict[str, tuple[Decimal, Decimal]]:
    total: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for l in lineas:
        clave = l.codigo or "SIN_CODIGO"
        total[clave][0] += l.cantidad
        total[clave][1] += l.valor_total
    return {k: (v[0], v[1]) for k, v in total.items()}


def comparar_lineas(ctx: reglas.Contexto) -> list[ResultadoLinea]:
    """Agrupa los servicios por código en cada fuente y corre sobre cada uno las reglas de línea."""
    docs = ctx.docs
    fuentes = [f for f in FUENTES_LINEAS if f in docs and docs[f].lineas]
    agrupado = {f: _agrupar(docs[f].lineas) for f in fuentes}
    descripciones: dict[str, str] = {}
    tipos: dict[str, str] = {}
    for f in ("xml", "pdf_factura", "rips"):
        for l in docs[f].lineas if f in docs else []:
            if l.codigo and l.descripcion:
                descripciones.setdefault(l.codigo, l.descripcion)
            if l.codigo and l.tipo_servicio:
                tipos.setdefault(l.codigo, l.tipo_servicio)

    resultados: list[ResultadoLinea] = []
    for codigo in sorted({c for g in agrupado.values() for c in g}):
        linea = reglas.LineaComparada(
            codigo=codigo,
            fuentes=fuentes,
            cantidades={f: agrupado[f][codigo][0] for f in fuentes if codigo in agrupado[f]},
            valores={f: agrupado[f][codigo][1] for f in fuentes if codigo in agrupado[f]},
            descripcion=descripciones.get(codigo),
            tipo_servicio=tipos.get(codigo),
        )
        novedades = [n for _, n in reglas.ejecutar("linea", ctx, linea)]
        resultados.append(
            ResultadoLinea(
                codigo=codigo,
                tipo_servicio=linea.tipo_servicio,
                descripcion=linea.descripcion,
                cantidad_por_fuente=linea.cantidades,
                valor_por_fuente=linea.valores,
                tarifa=linea.tarifa,
                resultado=peor(*(n.severidad for n in novedades)),
                motivos=list(dict.fromkeys(n.motivo for n in novedades if n.motivo)),
                codigos=list(dict.fromkeys(n.codigo for n in novedades)),
                # La última regla que señala un documento manda (el valor pesa más que la ausencia).
                documento_sospechoso=next((n.sospechoso for n in reversed(novedades) if n.sospechoso), None),
            )
        )
    return resultados


def _servicios(docs: dict[str, DocumentoExtraido]) -> set[str]:
    return {l.tipo_servicio for l in docs["rips"].lineas if l.tipo_servicio} if "rips" in docs else set()


def marcar_duplicados(r: ResultadoFactura, engine: Engine) -> None:
    """Consulta la base por CUFE. Va aparte porque en paralelo solo el proceso principal usa la base."""
    if not r.cufe:
        return
    previos = db.previos_por_cufe(engine, r.cufe, r.corrida_id, r.factura)
    # Otra factura con el mismo CUFE, en una corrida anterior o en esta misma.
    for factura_previa in sorted({f for _, f in previos if f != r.factura}):
        r.hallazgos.append(Hallazgo(tipo="duplicado", codigo="DUPLICADO.CUFE", ref=factura_previa, resultado="grave",
                                  motivo=f"CUFE ya registrado con la factura {factura_previa}"))
    if corrida_previa := next((c for c, f in previos if f == r.factura), None):
        r.hallazgos.append(Hallazgo(tipo="duplicado", codigo="DUPLICADO.YA_PROCESADA", ref=corrida_previa, resultado="info",
                                  motivo=f"factura ya procesada en la corrida {corrida_previa}"))
    r.estado = peor(r.estado, *(h.resultado for h in r.hallazgos))


def resultado_fallido(ruta: Path, corrida_id: str, error: Exception) -> ResultadoFactura:
    """Factura que no se pudo procesar: queda en los reportes como grave para revisión manual,
    en lugar de desaparecer de la corrida. Solo se guarda el tipo de error (sin datos del paciente)."""
    return ResultadoFactura(
        corrida_id=corrida_id,
        factura=ruta.name,
        ruta_origen=str(ruta.resolve()),
        cufe=None,
        estado="grave",
        fuente_principal="ninguna",
        documentos_presentes={},
        documentos_faltantes=[],
        requiere_ocr=[],
        campos=[],
        lineas=[],
        hallazgos=[Hallazgo(
            tipo="procesamiento", codigo="PROCESAMIENTO.ERROR", ref=type(error).__name__, resultado="grave",
            motivo=f"la factura no se pudo procesar ({type(error).__name__}); revisar manualmente",
        )],
        dian={},
        cuv={},
        tiempos_ms={},
    )


def _clave_config(docs: dict[str, DocumentoExtraido], cfg: Config) -> tuple[str | None, date | None]:
    """EPS y fecha de la factura: deciden qué capas de configuración aplican."""

    def valor(campo: str) -> str | None:
        extraidos = [d.campos[campo] for d in docs.values() if campo in d.campos]
        return evaluar_campo(campo, extraidos, cfg).valor_final if extraidos else None

    return valor("nit_eps"), normalize.fecha(valor("fecha_emision"))


def procesar_carpeta(
    ruta: Path,
    corrida_id: str,
    cfg: Config | ResolutorConfig,
    tarifario: Tarifario | None = None,
    engine: Engine | None = None,
) -> ResultadoFactura:
    t0 = time.perf_counter()
    resolutor = cfg if isinstance(cfg, ResolutorConfig) else ResolutorConfig(cfg)
    cf = discover.clasificar(ruta)
    # La extracción (OCR, modo de los PDF) depende del hospital, no de la EPS: aún no se sabe cuál es.
    docs = extraer(cf, resolutor.para(None, None).cfg)
    resuelta = resolutor.para(*_clave_config(docs, resolutor.base))
    cfg = resuelta.cfg
    t_digital = time.perf_counter()

    principal = _fuente_principal(docs)
    campos: list[ResultadoCampo] = []
    for campo in cfg.campos:
        extraidos = [d.campos[campo] for d in docs.values() if campo in d.campos]
        campos.append(evaluar_campo(campo, extraidos, cfg))
    nit_eps = next((c.valor_final for c in campos if c.campo == "nit_eps"), None)
    regla = cfg.regla_documentos(nit_eps, _servicios(docs))
    ctx = reglas.Contexto(cf, docs, cfg, tarifario, principal, _periodo(campos), regla)
    lineas = ctx.lineas = comparar_lineas(ctx)
    hallazgos = reglas.hallazgos(ctx)

    cufe = next((c.valor_final for c in campos if c.campo == "cufe"), None)
    estado = peor(*(c.resultado for c in campos), *(l.resultado for l in lineas), *(h.resultado for h in hallazgos))
    x = docs.get("xml")
    c = docs.get("cuv")
    resultado = ResultadoFactura(
        corrida_id=corrida_id,
        factura=cf.factura,
        ruta_origen=str(ruta.resolve()),
        cufe=cufe,
        estado=estado,
        fuente_principal=principal,
        documentos_presentes=cf.presentes(),
        documentos_faltantes=[d for d in (regla.documentos if regla else {}) if not presente(cf, d)],
        requiere_ocr=[f for f, d in docs.items() if d.requiere_ocr],
        campos=campos,
        lineas=lineas,
        hallazgos=hallazgos,
        dian={
            "codigo": x.extra.get("dian_codigo") if x else None,
            "avisos": [k.codigo for k in x.codigos] if x else [],
        },
        cuv={
            "cuv": c.extra.get("cuv") if c else None,
            "result_state": c.extra.get("result_state") if c else None,
            "fecha_radicacion": c.extra.get("fecha_radicacion") if c else None,
            "codigos": sorted({k.codigo for k in c.codigos}) if c else [],
        },
        tiempos_ms={
            "digital": round((t_digital - t0) * 1000),
            "total": round((time.perf_counter() - t0) * 1000),
        },
        tarifario_version_id=tarifario.version_id if tarifario else None,
        hospital_id=resolutor.hospital_id,
        config_huella=resuelta.huella,
        config_capas=list(resuelta.capas),
    )
    resultado.propuestas_correccion = proponer(resultado)
    if engine is not None:
        marcar_duplicados(resultado, engine)
    log.info("factura %s: %s", cf.factura, resultado.estado)
    return resultado
