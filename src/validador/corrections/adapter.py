"""Propuestas de corrección según el documento sospechoso.

Son propuestas, no acciones automáticas. Los textos deben confirmarse con
contabilidad y con el área jurídica antes de usarlos como mensajes definitivos.
``apply()`` está deshabilitado: solo se activará por sistema, con aprobación
humana explícita y registro de auditoría.
"""

from __future__ import annotations

from typing import Protocol

from .. import catalogo
from ..base.schemas import PropuestaCorreccion, ResultadoFactura

# Acción propuesta por fuente: ``correccion`` en el catálogo (catalogo/catalogo.toml).
ACCIONES = {n: f.correccion for n, f in catalogo.get().fuentes.items() if f.correccion}


class CorrectionAdapter(Protocol):
    def propose(self, resultado: ResultadoFactura) -> list[PropuestaCorreccion]: ...

    def apply(self, propuesta: PropuestaCorreccion, aprobado_por: str) -> None: ...


def proponer(resultado: ResultadoFactura) -> list[PropuestaCorreccion]:
    propuestas: list[PropuestaCorreccion] = []
    for campo in resultado.campos:
        if campo.resultado != "grave" or not campo.documento_sospechoso:
            continue
        for doc in campo.documento_sospechoso.split(","):
            if doc not in ACCIONES:
                continue
            propuestas.append(
                PropuestaCorreccion(
                    campo=campo.campo,
                    documento=doc,
                    valor_actual=campo.valores_por_fuente.get(doc),
                    valor_propuesto=campo.valor_final,
                    accion=ACCIONES[doc],
                )
            )
    for linea in resultado.lineas:
        if linea.resultado != "grave" or not linea.documento_sospechoso:
            continue
        for doc in linea.documento_sospechoso.split(","):
            if doc in ACCIONES:
                propuestas.append(
                    PropuestaCorreccion(
                        campo=f"linea:{linea.codigo}",
                        documento=doc,
                        valor_actual=str(linea.valor_por_fuente.get(doc)) if doc in linea.valor_por_fuente else None,
                        valor_propuesto=None,
                        accion=ACCIONES[doc],
                    )
                )
    return propuestas


class ExportAdapter:
    """Primera implementación: solo genera propuestas para exportar a CSV/JSON."""

    def propose(self, resultado: ResultadoFactura) -> list[PropuestaCorreccion]:
        return proponer(resultado)

    def apply(self, propuesta: PropuestaCorreccion, aprobado_por: str) -> None:
        raise PermissionError("apply() deshabilitado: la corrección en sistemas de origen no está habilitada")
