"""Plantillas de regla: validaciones parametrizadas desde la configuración.

La lógica de cada plantilla está aquí, en código y con pruebas; cada uso es un dato en
``Config.plantillas`` (TOML o capa de la base de datos), validado contra sus parámetros en
base/config.py. Así un hospital (o un LLM que propone un borrador) puede pedir "el número de
autorización debe leerse en la factura PDF para esta EPS" sin escribir código:

    [[plantillas]]
    usar = "campo_requerido"
    nombre = "autorizacion-en-factura"
    campo = "numero_autorizacion"
    documentos = ["FEV"]
    severidad = "grave"

El ``nombre`` va como ``ref`` del hallazgo (se cuenta en la base): nunca datos del paciente.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

from .. import catalogo
from ..base.config import PlantillaCampoRequerido, PlantillaValorPermitido
from . import Contexto, Novedad, regla

PLANTILLAS: dict[str, Callable[[Contexto, object], Iterator[Novedad]]] = {}


def plantilla(usar: str) -> Callable:
    def registrar(fn: Callable) -> Callable:
        PLANTILLAS[usar] = fn
        return fn

    return registrar


@plantilla("campo_requerido")
def campo_requerido(ctx: Contexto, p: PlantillaCampoRequerido) -> Iterator[Novedad]:
    for prefijo in p.documentos:
        tipo = catalogo.get().documentos[prefijo]
        doc = ctx.docs.get(tipo.fuente) if tipo.fuente else None
        if doc is None or doc.requiere_ocr or doc.errores:
            continue  # sin el documento (DOC.FALTA) o sin leer (OCR, EXTRACCION.ERROR): ya se reporta aparte
        if not (c := doc.campos.get(p.campo)) or not c.valor:
            nombre = catalogo.get().campos[p.campo].nombre.lower()
            yield Novedad("PLANTILLA.CAMPO_FALTA", f"{nombre} no se encontró en {tipo.nombre} (plantilla '{p.nombre}')",
                          p.severidad, ref=p.nombre, sospechoso=tipo.fuente)


@plantilla("valor_permitido")
def valor_permitido(ctx: Contexto, p: PlantillaValorPermitido) -> Iterator[Novedad]:
    valor = ctx.valor_final(p.campo)
    if valor is None:
        return
    if valor in p.valores or (p.patron and re.fullmatch(p.patron, valor)):
        return
    nombre = catalogo.get().campos[p.campo].nombre.lower()
    yield Novedad("PLANTILLA.VALOR_NO_PERMITIDO", f"{nombre} {valor} no está permitido (plantilla '{p.nombre}')",
                  p.severidad, ref=p.nombre)


@regla(
    "PLANTILLAS", ambito="factura", tipo="plantilla",
    severidades={"PLANTILLA.CAMPO_FALTA": None, "PLANTILLA.VALOR_NO_PERMITIDO": None},
)
def plantillas(ctx: Contexto):
    """Validaciones parametrizadas de la configuración (Config.plantillas)."""
    for p in ctx.cfg.plantillas:
        yield from PLANTILLAS[p.usar](ctx, p)
