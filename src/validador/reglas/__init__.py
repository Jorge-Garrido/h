"""Registro de reglas de validación.

Cada regla es una función pequeña registrada con ``@regla``, que recibe el contexto de la
factura (y la línea, si su ámbito es "linea") y produce ``Novedad``es. El pipeline no conoce
las reglas: ejecuta todas las registradas para cada ámbito.

Cada código de motivo se puede desactivar o cambiar de severidad por hospital o EPS con
``Config.reglas`` (TOML o capas de la base de datos), sin tocar código:

    [reglas."HC.TIPO_CONSULTA_DIFIERE"]
    severidad = "info"

Para agregar una regla: una función con ``@regla`` en el módulo del tema, sus códigos en
``base/motivos.py`` y su prueba. ``validador reglas listar`` muestra el registro.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from ..base import motivos
from ..base.config import Config, ReglaDocumentos, Severidad
from ..base.schemas import DocumentoExtraido, Hallazgo, ResultadoLinea, ResultadoTarifa

if TYPE_CHECKING:
    from ..ingest.discover import CarpetaFactura
    from ..tarifario.matcher import Tarifario

Ambito = Literal["linea", "factura"]


@dataclass(frozen=True)
class Novedad:
    """Lo que encuentra una regla. Sin severidad, toma la que el registro define para el código."""

    codigo: str
    motivo: str = ""
    severidad: Severidad | None = None
    ref: str | None = None
    sospechoso: str | None = None


@dataclass
class Contexto:
    """Todo lo que una regla puede consultar de la factura."""

    cf: CarpetaFactura
    docs: dict[str, DocumentoExtraido]
    cfg: Config
    tarifario: Tarifario | None
    principal: str
    periodo: tuple[date | None, date | None]
    regla_documentos: ReglaDocumentos | None
    # Se llena después de evaluar las líneas: las reglas de factura pueden usarlas (p. ej. HC).
    lineas: list[ResultadoLinea] = field(default_factory=list)


@dataclass
class LineaComparada:
    """Un código de servicio con su cantidad y valor en cada fuente que trae líneas."""

    codigo: str
    fuentes: list[str]
    cantidades: dict[str, Decimal]
    valores: dict[str, Decimal]
    descripcion: str | None = None
    tipo_servicio: str | None = None
    # La regla de tarifa deja aquí el resultado más problemático entre las líneas del código.
    tarifa: ResultadoTarifa | None = None


@dataclass(frozen=True)
class Regla:
    id: str
    ambito: Ambito
    tipo: str  # Hallazgo.tipo de lo que produce (documentos, ocr, dian, cuv, hc...)
    severidades: dict[str, Severidad]  # código de motivo -> severidad por defecto (None = la da la regla)
    descripcion: str
    fn: Callable[..., Iterable[Novedad] | None]


REGISTRO: dict[str, Regla] = {}


def regla(
    id: str,
    *,
    ambito: Ambito,
    tipo: str = "",
    severidad: Severidad | None = None,
    severidades: dict[str, Severidad | None] | None = None,
) -> Callable:
    """Registra una regla. ``severidad`` es el atajo para una regla con un solo código (= ``id``);
    ``severidades`` declara todos los códigos que emite, con su severidad por defecto o None
    cuando la decide la regla según el caso (p. ej. la matriz de documentos)."""
    por_codigo = severidades if severidades is not None else {id: severidad}

    def registrar(fn: Callable) -> Callable:
        if id in REGISTRO:
            raise ValueError(f"regla duplicada: {id}")
        for codigo in por_codigo:
            motivos.validar(codigo)
        descripcion = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        REGISTRO[id] = Regla(id, ambito, tipo or ambito, dict(por_codigo), descripcion, fn)
        return fn

    return registrar


def _severidad(cfg: Config, r: Regla, n: Novedad) -> Severidad | None:
    """Severidad final: la de la configuración si la fija, si no la de la regla. None = desactivado."""
    ajuste = cfg.reglas.get(n.codigo)
    if ajuste is not None and not ajuste.activa:
        return None
    sugerida = n.severidad or r.severidades.get(n.codigo)
    if sugerida is None:
        raise ValueError(f"la regla {r.id} emitió {n.codigo} sin severidad")
    return (ajuste.severidad if ajuste is not None else None) or sugerida


def ejecutar(ambito: Ambito, ctx: Contexto, *args) -> list[tuple[Regla, Novedad]]:
    """Corre las reglas del ámbito. Una regla que falla no detiene la factura: deja REGLA.ERROR."""
    salida: list[tuple[Regla, Novedad]] = []
    for r in REGISTRO.values():
        if r.ambito != ambito:
            continue
        try:
            for n in r.fn(ctx, *args) or ():
                if n.codigo not in r.severidades:
                    raise ValueError(f"la regla {r.id} emitió {n.codigo}, que no declaró")
                if (sev := _severidad(ctx.cfg, r, n)) is not None:
                    salida.append((r, replace(n, severidad=sev)))
        except Exception as e:
            falla = Novedad("REGLA.ERROR", f"la regla {r.id} falló ({type(e).__name__})", "aviso", ref=r.id)
            salida.append((replace(r, tipo="regla"), falla))
    return salida


def hallazgos(ctx: Contexto) -> list[Hallazgo]:
    return [
        Hallazgo(
            tipo=r.tipo, codigo=n.codigo, ref=n.ref, resultado=n.severidad, motivo=n.motivo, documento_sospechoso=n.sospechoso
        )
        for r, n in ejecutar("factura", ctx)
    ]


# Registra las reglas. El orden de los módulos es el orden de los hallazgos en los reportes.
from . import lineas, documentos, codigos, hc  # noqa: E402, F401, I001
