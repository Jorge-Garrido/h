"""Simulación de una capa de configuración antes de publicarla.

Se procesan las mismas facturas con la configuración vigente y con el borrador, y se
reporta qué cambia. Solo se comparan códigos de motivo y severidades (campo, CUPS, código):
nunca valores del paciente.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .base.schemas import ResultadoFactura


@dataclass
class Cambio:
    factura: str
    antes: str
    despues: str
    aparecen: list[str] = field(default_factory=list)
    desaparecen: list[str] = field(default_factory=list)


def _items(r: ResultadoFactura) -> Counter[str]:
    c: Counter[str] = Counter()
    c.update(f"campo {x.campo} {'+'.join(x.codigos)}: {x.resultado}" for x in r.campos if x.resultado != "ok")
    c.update(f"línea {x.codigo} {'+'.join(x.codigos)}: {x.resultado}" for x in r.lineas if x.resultado != "ok")
    c.update(f"{x.codigo}{f' {x.ref}' if x.ref else ''}: {x.resultado}" for x in r.hallazgos if x.resultado != "ok")
    return c


def comparar(actuales: list[ResultadoFactura], nuevos: list[ResultadoFactura]) -> list[Cambio]:
    """Facturas cuyo resultado cambia con el borrador (emparejadas por número de factura)."""
    por_factura = {r.factura: r for r in actuales}
    cambios = []
    for b in sorted(nuevos, key=lambda r: r.factura):
        a = por_factura.get(b.factura)
        if a is None:
            continue
        ia, ib = _items(a), _items(b)
        aparecen, desaparecen = sorted((ib - ia).elements()), sorted((ia - ib).elements())
        if a.estado != b.estado or aparecen or desaparecen:
            cambios.append(Cambio(b.factura, a.estado, b.estado, aparecen, desaparecen))
    return cambios


def resumen(cambios: list[Cambio], total: int) -> dict:
    return {
        "facturas": total,
        "cambian": len(cambios),
        "transiciones": dict(Counter(f"{c.antes} -> {c.despues}" for c in cambios if c.antes != c.despues)),
        "aparecen": dict(Counter(i for c in cambios for i in c.aparecen)),
        "desaparecen": dict(Counter(i for c in cambios for i in c.desaparecen)),
    }
