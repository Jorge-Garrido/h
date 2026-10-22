"""Reglas por servicio facturado (código CUPS), comparado entre RIPS, XML y PDF de la factura."""

from __future__ import annotations

from ..attribution import votar
from ..base.schemas import DocumentoExtraido, LineaServicio
from . import Contexto, LineaComparada, Novedad, regla


@regla("LINEA.CODIGO_FALTA", ambito="linea", severidad="grave")
def codigo_falta(ctx: Contexto, linea: LineaComparada):
    """El servicio no aparece en todas las fuentes que traen líneas."""
    faltan = [f for f in linea.fuentes if f not in linea.valores]
    if faltan:
        voto = votar({f: ("si" if f in linea.valores else "no") for f in linea.fuentes}, "exacta")
        motivo = f"el código no aparece en: {', '.join(faltan)}"
        yield Novedad("LINEA.CODIGO_FALTA", motivo, sospechoso=",".join(voto.sospechosos) or None)


@regla("LINEA.VALOR_DIFIERE", ambito="linea", severidad="grave")
def valor_difiere(ctx: Contexto, linea: LineaComparada):
    """El valor total del servicio difiere entre fuentes."""
    if len(set(linea.valores.values())) > 1:
        voto = votar({f: str(v) for f, v in linea.valores.items()}, "numerica")
        motivo = "el valor difiere entre fuentes" + ("" if voto.sospechosos else " sin mayoría")
        yield Novedad("LINEA.VALOR_DIFIERE", motivo, sospechoso=",".join(voto.sospechosos) or None)


@regla("LINEA.CANTIDAD_DIFIERE", ambito="linea", severidad="aviso")
def cantidad_difiere(ctx: Contexto, linea: LineaComparada):
    """La cantidad del servicio difiere entre fuentes."""
    if len(set(linea.cantidades.values())) > 1:
        yield Novedad("LINEA.CANTIDAD_DIFIERE", "la cantidad difiere entre fuentes")


@regla("LINEA.FECHA_FUERA_PERIODO", ambito="linea", severidad="aviso")
def fecha_fuera_periodo(ctx: Contexto, linea: LineaComparada):
    """Fecha de atención del RIPS fuera del periodo de la factura."""
    inicio, fin = ctx.periodo
    if not (inicio and fin and "rips" in ctx.docs):
        return
    if any(
        (l.codigo or "SIN_CODIGO") == linea.codigo and l.fecha and not (inicio <= l.fecha <= fin)
        for l in ctx.docs["rips"].lineas
    ):
        yield Novedad("LINEA.FECHA_FUERA_PERIODO", "fecha de atención del RIPS fuera del periodo de la factura")


@regla("LINEA.CUPS_NO_EN_AUTORIZACION", ambito="linea", severidad="aviso")
def cups_no_en_autorizacion(ctx: Contexto, linea: LineaComparada):
    """El CUPS facturado no aparece en la autorización escaneada de la EPS."""
    escaneo = ctx.docs.get("autorizacion_scan")
    if escaneo is None or escaneo.extra.get(f"cups:{linea.codigo}") not in ("no_legible", "diferencia"):
        return
    listados = [c for c in (escaneo.extra.get("cups_escaneo") or "").split(",") if c]
    detalle = f" (la autorización lista: {', '.join(listados[:5])})" if listados else ""
    yield Novedad("LINEA.CUPS_NO_EN_AUTORIZACION", f"el CUPS facturado no está en la autorización escaneada{detalle}")


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


# Las severidades de la tarifa están en Config.tarifa (si_valor_difiere, si_no_determinable...).
CODIGOS_TARIFA = ("TARIFA.VALOR_DIFIERE", "TARIFA.NO_DETERMINABLE", "TARIFA.NO_EN_TARIFARIO")


@regla("LINEA.TARIFA", ambito="linea", severidades=dict.fromkeys(CODIGOS_TARIFA))
def tarifa(ctx: Contexto, linea: LineaComparada):
    """Valor unitario de cada línea contra el tarifario contratado (severidades en Config.tarifa)."""
    if ctx.tarifario is None:
        return
    reglas_tarifa = ctx.cfg.tarifa
    for l in _lineas_para_tarifa(ctx.docs, linea.codigo, ctx.principal):
        if not l.cantidad:
            continue
        unitario = l.valor_total / l.cantidad
        t = ctx.tarifario.buscar(linea.codigo, l.descripcion or linea.descripcion, unitario, reglas_tarifa)
        if linea.tarifa is None or GRAVEDAD_TARIFA[t.estado] < GRAVEDAD_TARIFA[linea.tarifa.estado]:
            linea.tarifa = t
        if t.estado == "valor_difiere":
            yield Novedad(
                "TARIFA.VALOR_DIFIERE",
                f"valor unitario {unitario} distinto a la tarifa {t.valor_unitario_tarifa} ({t.codigo_institucional})",
                reglas_tarifa.si_valor_difiere,
            )
        elif t.estado == "no_determinable":
            yield Novedad(
                "TARIFA.NO_DETERMINABLE",
                "tarifa no determinable: la descripción no corresponde a ningún ítem del tarifario",
                reglas_tarifa.si_no_determinable,
            )
        elif t.estado == "no_en_tarifario":
            yield Novedad("TARIFA.NO_EN_TARIFARIO", severidad=reglas_tarifa.si_no_en_tarifario)
