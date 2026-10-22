"""Reglas contra la historia clínica: fecha, tipo de consulta y especialidad.

La identidad del paciente contra la HC se valida en los campos (severity.evaluar_campo).
"""

from __future__ import annotations

import re

from ..data_preprocessing import normalize
from . import Contexto, Novedad, regla


def tipo_consulta_esperado(codigo: str, descripcion: str | None) -> str | None:
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


@regla("HC.FECHA_FUERA_PERIODO", ambito="factura", tipo="hc", severidad="aviso")
def fecha_fuera_periodo(ctx: Contexto):
    """Ninguna nota de la historia clínica está dentro del periodo de la factura."""
    hc = ctx.docs.get("hc")
    inicio, fin = ctx.periodo
    fechas = [a.fecha for a in hc.atenciones if a.fecha] if hc else []
    if inicio and fin and fechas and not any(inicio <= f <= fin for f in fechas):
        yield Novedad("HC.FECHA_FUERA_PERIODO", "ninguna nota de la HC está dentro del periodo de la factura", sospechoso="hc")


@regla(
    "HC.CONSULTAS", ambito="factura", tipo="hc",
    severidades={"HC.TIPO_CONSULTA_DIFIERE": "aviso", "HC.ESPECIALIDAD_NO_VERIFICABLE": "info"},
)
def consultas(ctx: Contexto):
    """Tipo de consulta (primera vez o control) y especialidad de cada consulta facturada, contra la HC."""
    hc = ctx.docs.get("hc")
    if hc is None or not hc.atenciones:
        return
    tipos_hc = {a.tipo_consulta for a in hc.atenciones if a.tipo_consulta}
    especialidades = {a.especialidad for a in hc.atenciones if a.especialidad}
    for linea in ctx.lineas:
        if linea.tipo_servicio != "consultas":
            continue
        esperado = tipo_consulta_esperado(linea.codigo, linea.descripcion)
        if esperado and tipos_hc and esperado not in tipos_hc:
            registrado = ", ".join(sorted(tipos_hc)).replace("_", " ")
            yield Novedad(
                "HC.TIPO_CONSULTA_DIFIERE",
                f"consulta {linea.codigo} facturada como {esperado.replace('_', ' ')} y la HC registra {registrado}",
                ref=linea.codigo, sospechoso="hc",
            )
        desc = normalize.texto(linea.descripcion) or ""
        if especialidades and desc and not any(re.search(rf"\b{re.escape(e)}\b", desc) for e in especialidades):
            motivo = f"especialidad de la consulta {linea.codigo} no verificable contra la HC"
            yield Novedad("HC.ESPECIALIDAD_NO_VERIFICABLE", motivo, ref=linea.codigo)
