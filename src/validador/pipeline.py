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
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.engine import Engine

from .data_preprocessing import normalize
from .attribution import votar
from .base.config import Config, ReglaDocumentos, ResolutorConfig, Severidad
from .corrections.adapter import proponer
from .ingest import (
    autorizacion_header, cuv, discover, factura_pdf_parser, generico, hc_parser, rips, unificado, xml_parser,
)
from .base.schemas import (
    DocumentoExtraido,
    Hallazgo,
    LineaServicio,
    ResultadoCampo,
    ResultadoFactura,
    ResultadoLinea,
)
from .severity import evaluar_campo, peor
from .store import db
from .tarifario.matcher import Tarifario

log = logging.getLogger(__name__)

FUENTES_LINEAS = ("rips", "xml", "pdf_factura")
NOMBRE_DOCUMENTO = {
    "HEV": "historia clínica",
    "PDE": "autorización",
    "FEV": "factura PDF",
    "PDX": "resultados de apoyo diagnóstico",
    "CUV": "respuesta de validación del Ministerio",
}


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


# Cuál resultado de tarifa representa a un código cuando tiene varias líneas: el más problemático.
GRAVEDAD_TARIFA = {"valor_difiere": 0, "no_determinable": 1, "coincide": 2, "no_en_tarifario": 3, "sin_tarifario": 4}


def _lineas_para_tarifa(docs: dict[str, DocumentoExtraido], codigo: str, principal: str) -> list[LineaServicio]:
    """Líneas individuales del código para buscar su tarifa, de la fuente que trae la descripción
    de cada una (XML, PDF) o, si ninguna la trae, de la fuente principal.

    La tarifa se busca línea por línea y no sobre la suma: un mismo CUPS puede facturarse varias
    veces con tarifas distintas (p. ej. 998702, sedación para resonancia y para tomografía), y el
    promedio de las dos no corresponde a ningún ítem del tarifario."""
    for fuente in ("xml", "pdf_factura", principal):
        lineas = [l for l in docs[fuente].lineas if (l.codigo or "SIN_CODIGO") == codigo] if fuente in docs else []
        if lineas and (fuente == principal or all(l.descripcion for l in lineas)):
            return lineas
    return []


def comparar_lineas(
    docs: dict[str, DocumentoExtraido],
    principal: str,
    tarifario: Tarifario | None,
    cfg: Config,
    periodo: tuple[date | None, date | None],
) -> list[ResultadoLinea]:
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
    codigos = sorted({c for g in agrupado.values() for c in g})
    for codigo in codigos:
        cantidades = {f: agrupado[f][codigo][0] for f in fuentes if codigo in agrupado[f]}
        valores = {f: agrupado[f][codigo][1] for f in fuentes if codigo in agrupado[f]}
        motivos: list[str] = []
        resultado: Severidad = "ok"
        sospechoso: str | None = None

        faltan = [f for f in fuentes if f not in valores]
        if faltan:
            voto = votar({f: ("si" if f in valores else "no") for f in fuentes}, "exacta")
            sospechoso = ",".join(voto.sospechosos) or None
            resultado = peor(resultado, cfg.lineas_si_codigo_falta)
            motivos.append(f"el código no aparece en: {', '.join(faltan)}")
        if len(set(valores.values())) > 1:
            voto = votar({f: str(v) for f, v in valores.items()}, "numerica")
            sospechoso = ",".join(voto.sospechosos) or sospechoso
            resultado = peor(resultado, cfg.lineas_si_valor_difiere)
            motivos.append("el valor difiere entre fuentes" + ("" if voto.sospechosos else " sin mayoría"))
        if len(set(cantidades.values())) > 1:
            resultado = peor(resultado, cfg.lineas_si_cantidad_difiere)
            motivos.append("la cantidad difiere entre fuentes")

        inicio, fin = periodo
        if inicio and fin and "rips" in docs:
            fuera = [
                l.fecha for l in docs["rips"].lineas
                if (l.codigo or "SIN_CODIGO") == codigo and l.fecha and not (inicio <= l.fecha <= fin)
            ]
            if fuera:
                resultado = peor(resultado, cfg.lineas_si_fecha_fuera_periodo)
                motivos.append("fecha de atención del RIPS fuera del periodo de la factura")

        if "autorizacion_scan" in docs:
            estado_cups = docs["autorizacion_scan"].extra.get(f"cups:{codigo}")
            if estado_cups in ("no_legible", "diferencia"):
                resultado = peor(resultado, cfg.cups_no_en_autorizacion)
                listados = [c for c in (docs["autorizacion_scan"].extra.get("cups_escaneo") or "").split(",") if c]
                detalle = f" (la autorización lista: {', '.join(listados[:5])})" if listados else ""
                motivos.append(f"el CUPS facturado no está en la autorización escaneada{detalle}")

        tarifa = None
        for linea in _lineas_para_tarifa(docs, codigo, principal) if tarifario is not None else []:
            if not linea.cantidad:
                continue
            unitario = linea.valor_total / linea.cantidad
            t = tarifario.buscar(codigo, linea.descripcion or descripciones.get(codigo), unitario, cfg.tarifa)
            motivo = None
            if t.estado == "valor_difiere":
                resultado = peor(resultado, cfg.tarifa.si_valor_difiere)
                motivo = f"valor unitario {unitario} distinto a la tarifa {t.valor_unitario_tarifa} ({t.codigo_institucional})"
            elif t.estado == "no_determinable":
                resultado = peor(resultado, cfg.tarifa.si_no_determinable)
                motivo = "tarifa no determinable: la descripción no corresponde a ningún ítem del tarifario"
            elif t.estado == "no_en_tarifario":
                resultado = peor(resultado, cfg.tarifa.si_no_en_tarifario)
            if motivo and motivo not in motivos:
                motivos.append(motivo)
            if tarifa is None or GRAVEDAD_TARIFA[t.estado] < GRAVEDAD_TARIFA[tarifa.estado]:
                tarifa = t

        resultados.append(
            ResultadoLinea(
                codigo=codigo,
                tipo_servicio=tipos.get(codigo),
                descripcion=descripciones.get(codigo),
                cantidad_por_fuente=cantidades,
                valor_por_fuente=valores,
                tarifa=tarifa,
                resultado=resultado,
                motivos=motivos,
                documento_sospechoso=sospechoso,
            )
        )
    return resultados


def _presente(cf: discover.CarpetaFactura, documento: str) -> bool:
    if documento == "RIPS":
        return cf.rips is not None
    if documento == "CUV":
        return cf.cuv is not None
    return cf.pdf(documento) is not None


def _servicios(docs: dict[str, DocumentoExtraido]) -> set[str]:
    return {l.tipo_servicio for l in docs["rips"].lineas if l.tipo_servicio} if "rips" in docs else set()


def _hallazgos_documentos(
    cf: discover.CarpetaFactura, docs: dict[str, DocumentoExtraido], cfg: Config, regla: ReglaDocumentos | None
) -> list[Hallazgo]:
    h: list[Hallazgo] = []
    exigidos = regla.documentos if regla else {}
    if not cf.rips and not cf.xml and cf.pdf("FEV"):
        origen = f" (tomada de {cf.etiquetas['FEV']})" if "FEV" in cf.etiquetas else ""
        h.append(Hallazgo(
            tipo="documentos", resultado=cfg.si_solo_pdf,
            motivo=f"sin XML ni RIPS: se validó con el PDF de la factura{origen}; no se pudo verificar el CUFE ante la DIAN",
        ))
    elif not cf.rips and not cf.xml:
        h.append(Hallazgo(tipo="documentos", resultado="grave", motivo="no hay RIPS ni XML de la factura"))
    elif not cf.rips and "RIPS" in exigidos:
        h.append(Hallazgo(tipo="documentos", resultado=exigidos["RIPS"], motivo="sin RIPS: se usó el XML como respaldo"))
    elif not cf.xml:
        h.append(Hallazgo(tipo="documentos", resultado="grave", motivo="falta el XML de la factura electrónica"))
    for documento, severidad in exigidos.items():
        if documento == "RIPS" or _presente(cf, documento):
            continue  # el RIPS ya se reportó arriba junto con el XML
        h.append(Hallazgo(
            tipo="documentos", resultado=severidad,
            motivo=f"falta {documento} ({NOMBRE_DOCUMENTO.get(documento, documento)}); regla '{regla.nombre}'",
        ))
    for fuente, doc in docs.items():
        for e in doc.errores:
            h.append(Hallazgo(tipo="extraccion", resultado="aviso", motivo=f"{fuente}: {e}", documento_sospechoso=fuente))
    if "hc" in docs and docs["hc"].requiere_ocr:
        h.append(Hallazgo(tipo="ocr", resultado="info", motivo="historia clínica escaneada: identidad pendiente de OCR"))
    if "autorizacion_scan" in docs:
        esc = docs["autorizacion_scan"]
        for campo in ("numero_autorizacion", "paciente_id"):
            if esc.extra.get(f"no_concluyente:{campo}") and campo == "numero_autorizacion":
                h.append(Hallazgo(
                    tipo="ocr", resultado=cfg.autorizacion_no_legible,
                    motivo="autorización escaneada: lectura no concluyente del número (un solo motor); revisión humana",
                    documento_sospechoso="autorizacion_scan",
                ))
            if esc.extra.get(f"no_legible:{campo}"):
                h.append(Hallazgo(
                    tipo="ocr", resultado=cfg.autorizacion_no_legible if campo == "numero_autorizacion" else "info",
                    motivo=f"autorización escaneada: {campo.replace('_', ' ')} no legible (revisión humana)",
                    documento_sospechoso="autorizacion_scan",
                ))
    elif "autorizacion_digital" in docs:
        aut = docs["autorizacion_digital"]
        detalle = "" if aut.extra.get("encabezado_digital") else " (sin encabezado digital)"
        h.append(Hallazgo(tipo="ocr", resultado="info", motivo=f"autorización: número y CUPS pendientes de OCR{detalle}"))
    return h


def _hallazgos_codigos(docs: dict[str, DocumentoExtraido], cfg: Config) -> list[Hallazgo]:
    h: list[Hallazgo] = []
    if "xml" in docs:
        x = docs["xml"]
        codigo = x.extra.get("dian_codigo")
        if codigo and codigo not in cfg.dian_codigos_aceptado:
            h.append(Hallazgo(tipo="dian", resultado="grave", motivo=f"respuesta DIAN {codigo}: documento no validado", documento_sospechoso="xml"))
        for c in x.codigos:
            h.append(Hallazgo(tipo="dian", resultado=cfg.codigos_recurrentes.get(c.codigo, cfg.dian_codigo_desconocido), motivo=f"DIAN {c.codigo}: {c.descripcion or ''}".strip()))
    if "cuv" in docs:
        c = docs["cuv"]
        if c.extra.get("result_state") is False:
            h.append(Hallazgo(tipo="cuv", resultado="grave", motivo="validación del Ministerio rechazada (ResultState=false)", documento_sospechoso="rips"))
        conteo = Counter((v.codigo, v.clase, v.descripcion) for v in c.codigos)
        for (codigo, clase, descripcion), n in sorted(conteo.items()):
            if clase in cfg.cuv_clases_rechazo:
                sev: Severidad = "grave"
            else:
                sev = cfg.codigos_recurrentes.get(codigo, cfg.cuv_codigo_desconocido)
            veces = f" (x{n})" if n > 1 else ""
            h.append(Hallazgo(tipo="cuv", resultado=sev, motivo=f"CUV {clase} {codigo}{veces}: {descripcion or ''}".strip()))
    return h


def _tipo_consulta_esperado(codigo: str, descripcion: str | None) -> str | None:
    d = normalize.texto(descripcion) or ""
    if "PRIMERA VEZ" in d:
        return "primera_vez"
    if "CONTROL" in d or "SEGUIMIENTO" in d:
        return "control"
    # Estructura CUPS: 8902xx primera vez, 8903xx control o seguimiento.
    if codigo.startswith("8902"):
        return "primera_vez"
    if codigo.startswith("8903"):
        return "control"
    return None


def _hallazgos_hc(docs: dict[str, DocumentoExtraido], lineas: list[ResultadoLinea], periodo, cfg: Config) -> list[Hallazgo]:
    """Contra la HC solo se valida identidad (en campos), fecha, especialidad y tipo de consulta."""
    hc = docs.get("hc")
    if hc is None or not hc.atenciones:
        return []
    h: list[Hallazgo] = []
    inicio, fin = periodo
    fechas = [a.fecha for a in hc.atenciones if a.fecha]
    if inicio and fin and fechas and not any(inicio <= f <= fin for f in fechas):
        h.append(Hallazgo(tipo="hc", resultado=cfg.hc_si_fecha_fuera_periodo, motivo="ninguna nota de la HC está dentro del periodo de la factura", documento_sospechoso="hc"))

    tipos_hc = {a.tipo_consulta for a in hc.atenciones if a.tipo_consulta}
    especialidades = {a.especialidad for a in hc.atenciones if a.especialidad}
    for linea in lineas:
        if linea.tipo_servicio != "consultas":
            continue
        esperado = _tipo_consulta_esperado(linea.codigo, linea.descripcion)
        if esperado and tipos_hc and esperado not in tipos_hc:
            h.append(Hallazgo(
                tipo="hc",
                resultado=cfg.hc_si_tipo_consulta_difiere,
                motivo=f"consulta {linea.codigo} facturada como {esperado.replace('_', ' ')} y la HC registra {', '.join(sorted(tipos_hc)).replace('_', ' ')}",
                documento_sospechoso="hc",
            ))
        desc = normalize.texto(linea.descripcion) or ""
        if especialidades and desc and not any(re.search(rf"\b{re.escape(e)}\b", desc) for e in especialidades):
            h.append(Hallazgo(tipo="hc", resultado="info", motivo=f"especialidad de la consulta {linea.codigo} no verificable contra la HC"))
    return h


def marcar_duplicados(r: ResultadoFactura, engine: Engine) -> None:
    """Consulta la base por CUFE. Va aparte porque en paralelo solo el proceso principal usa la base."""
    if not r.cufe:
        return
    previos = db.previos_por_cufe(engine, r.cufe, r.corrida_id, r.factura)
    # Otra factura con el mismo CUFE, en una corrida anterior o en esta misma.
    for factura_previa in sorted({f for _, f in previos if f != r.factura}):
        r.hallazgos.append(Hallazgo(tipo="duplicado", resultado="grave", motivo=f"CUFE ya registrado con la factura {factura_previa}"))
    if corrida_previa := next((c for c, f in previos if f == r.factura), None):
        r.hallazgos.append(Hallazgo(tipo="duplicado", resultado="info", motivo=f"factura ya procesada en la corrida {corrida_previa}"))
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
            tipo="procesamiento", resultado="grave",
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
    periodo = _periodo(campos)
    lineas = comparar_lineas(docs, principal, tarifario, cfg, periodo)

    nit_eps = next((c.valor_final for c in campos if c.campo == "nit_eps"), None)
    regla = cfg.regla_documentos(nit_eps, _servicios(docs))
    hallazgos = _hallazgos_documentos(cf, docs, cfg, regla) + _hallazgos_codigos(docs, cfg) + _hallazgos_hc(docs, lineas, periodo, cfg)

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
        documentos_faltantes=[d for d in (regla.documentos if regla else {}) if not _presente(cf, d)],
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
