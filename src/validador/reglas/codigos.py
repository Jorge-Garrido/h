"""Reglas sobre las respuestas de la DIAN (en el XML) y de la validación del Ministerio (CUV)."""

from __future__ import annotations

from collections import Counter

from . import Contexto, Novedad, regla


@regla("DIAN.RESPUESTA", ambito="factura", tipo="dian", severidades={"DIAN.NO_VALIDADO": "grave", "DIAN.CODIGO": None})
def respuesta_dian(ctx: Contexto):
    """Respuesta de la DIAN y sus códigos (los recurrentes se clasifican en codigos_recurrentes)."""
    x = ctx.docs.get("xml")
    if x is None:
        return
    cfg = ctx.cfg
    codigo = x.extra.get("dian_codigo")
    if codigo and codigo not in cfg.dian_codigos_aceptado:
        yield Novedad("DIAN.NO_VALIDADO", f"respuesta DIAN {codigo}: documento no validado", ref=codigo, sospechoso="xml")
    for c in x.codigos:
        severidad = cfg.codigos_recurrentes.get(c.codigo, cfg.dian_codigo_desconocido)
        yield Novedad("DIAN.CODIGO", f"DIAN {c.codigo}: {c.descripcion or ''}".strip(), severidad, ref=c.codigo)


@regla("CUV.RESPUESTA", ambito="factura", tipo="cuv", severidades={"CUV.RECHAZADO": "grave", "CUV.CODIGO": None})
def respuesta_cuv(ctx: Contexto):
    """Resultado de la validación del Ministerio y sus códigos (agrupados, con el número de veces)."""
    c = ctx.docs.get("cuv")
    if c is None:
        return
    cfg = ctx.cfg
    if c.extra.get("result_state") is False:
        yield Novedad("CUV.RECHAZADO", "validación del Ministerio rechazada (ResultState=false)", sospechoso="rips")
    conteo = Counter((v.codigo, v.clase, v.descripcion) for v in c.codigos)
    for (codigo, clase, descripcion), n in sorted(conteo.items()):
        if clase in cfg.cuv_clases_rechazo:
            severidad = "grave"
        else:
            severidad = cfg.codigos_recurrentes.get(codigo, cfg.cuv_codigo_desconocido)
        veces = f" (x{n})" if n > 1 else ""
        yield Novedad("CUV.CODIGO", f"CUV {clase} {codigo}{veces}: {descripcion or ''}".strip(), severidad, ref=codigo)
