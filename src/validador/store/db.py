"""Persistencia con SQLAlchemy Core.

El mismo código corre en SQLite (desarrollo) y Postgres (producción): basta con
cambiar ``VALIDADOR_DB_URL`` (p. ej. ``postgresql+psycopg://usuario@host/validador``).
En Postgres los resultados quedan en JSONB.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    func,
    insert,
    not_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from ..base.config import CapaConfig, Config, validar_capa
from ..base.schemas import ResultadoFactura
from ..tarifario.importer import ItemTarifa, ResultadoImportacion
from ..tarifario.matcher import Tarifario

JsonType = JSON().with_variant(JSONB(), "postgresql")
metadata = MetaData()

corridas = Table(
    "corridas",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("inicio", DateTime(timezone=True), nullable=False),
    Column("fin", DateTime(timezone=True)),
    Column("version_pipeline", String(20), nullable=False),
    Column("resumen", JsonType),
)

resultados = Table(
    "resultados",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("corrida_id", String(64), ForeignKey("corridas.id"), nullable=False, index=True),
    Column("numero_factura", String(40), nullable=False, index=True),
    Column("cufe", String(96), index=True),
    Column("estado", String(10), nullable=False, index=True),
    Column("creado", DateTime(timezone=True), nullable=False),
    Column("resultado", JsonType, nullable=False),
    UniqueConstraint("corrida_id", "numero_factura"),
)

tarifario_version = Table(
    "tarifario_version",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nombre", String(200), nullable=False),
    Column("archivo", String(300), nullable=False),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("uvb_anio", Integer),
    Column("uvb_valor", Numeric(14, 2)),
    Column("cargado_en", DateTime(timezone=True), nullable=False),
    Column("avisos", JsonType),
)

tarifario_item = Table(
    "tarifario_item",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("version_id", Integer, ForeignKey("tarifario_version.id"), nullable=False),
    Column("anexo", String(100), nullable=False),
    Column("codigo_institucional", String(20), nullable=False),
    Column("cups", String(20), nullable=False),
    Column("descripcion", Text, nullable=False),
    Column("tarifa_uvb", Numeric(14, 4), nullable=False),
    Column("valor", Numeric(16, 2), nullable=False),
    Column("fila", Integer),
    UniqueConstraint("version_id", "anexo", "codigo_institucional"),
)
# Capas de configuración (ver base/config.py). hospital_id/eps_nit en NULL = aplica a todos.
# Una capa publicada no se edita: se crea una nueva versión y, si hace falta, se retira la anterior.
config_capa = Table(
    "config_capa",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("hospital_id", String(40), index=True),
    Column("eps_nit", String(20)),
    Column("vigente_desde", Date, nullable=False),
    Column("vigente_hasta", Date),
    Column("estado", String(12), nullable=False),  # borrador | publicada | retirada
    Column("datos", JsonType, nullable=False),
    Column("comentario", Text),
    Column("creado_por", String(100), nullable=False),
    Column("creado", DateTime(timezone=True), nullable=False),
    Column("publicado", DateTime(timezone=True)),
)

Index("ix_tarifario_item_version_cups", tarifario_item.c.version_id, tarifario_item.c.cups)


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def conectar(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    metadata.create_all(engine)
    return engine


# --- tarifario ---------------------------------------------------------------


def guardar_tarifario(engine: Engine, res: ResultadoImportacion, nombre: str, uvb_anio: int | None) -> int:
    with engine.begin() as con:
        existente = con.execute(
            select(tarifario_version.c.id).where(tarifario_version.c.sha256 == res.sha256)
        ).scalar_one_or_none()
        if existente is not None:
            return existente
        version_id = con.execute(
            insert(tarifario_version).values(
                nombre=nombre,
                archivo=res.archivo,
                sha256=res.sha256,
                uvb_anio=uvb_anio,
                uvb_valor=res.uvb_valor,
                cargado_en=ahora(),
                avisos=res.avisos,
            )
        ).inserted_primary_key[0]
        con.execute(
            insert(tarifario_item),
            [
                {
                    "version_id": version_id,
                    "anexo": it.anexo,
                    "codigo_institucional": it.codigo_institucional,
                    "cups": it.cups,
                    "descripcion": it.descripcion,
                    "tarifa_uvb": it.tarifa_uvb,
                    "valor": it.valor,
                    "fila": it.fila,
                }
                for it in res.items
            ],
        )
    return version_id


def cargar_tarifario(engine: Engine, version_id: int | None = None) -> Tarifario | None:
    with engine.connect() as con:
        if version_id is None:
            version_id = con.execute(select(func.max(tarifario_version.c.id))).scalar_one_or_none()
        if version_id is None:
            return None
        filas = con.execute(select(tarifario_item).where(tarifario_item.c.version_id == version_id)).mappings()
        items = [
            ItemTarifa(
                anexo=f["anexo"],
                codigo_institucional=f["codigo_institucional"],
                cups=f["cups"],
                descripcion=f["descripcion"],
                tarifa_uvb=Decimal(f["tarifa_uvb"]),
                valor=Decimal(f["valor"]),
                fila=f["fila"],
            )
            for f in filas
        ]
    return Tarifario(items, version_id)


# --- capas de configuración ---------------------------------------------------

# Transiciones permitidas. Un borrador descartado también queda como "retirada" (no se borra: auditoría).
TRANSICIONES = {("borrador", "publicada"), ("borrador", "retirada"), ("publicada", "retirada")}


def guardar_borrador(
    engine: Engine,
    base: Config,
    datos: dict,
    *,
    hospital_id: str | None,
    eps_nit: str | None,
    vigente_desde: date,
    vigente_hasta: date | None = None,
    comentario: str | None = None,
    creado_por: str,
) -> int:
    """Valida la capa contra la configuración base y la guarda como borrador. Lanza ValueError si no es válida."""
    validar_capa(base, datos)
    if vigente_hasta is not None and vigente_hasta < vigente_desde:
        raise ValueError("vigente_hasta es anterior a vigente_desde")
    with engine.begin() as con:
        return con.execute(
            insert(config_capa).values(
                hospital_id=hospital_id,
                eps_nit=eps_nit,
                vigente_desde=vigente_desde,
                vigente_hasta=vigente_hasta,
                estado="borrador",
                datos=datos,
                comentario=comentario,
                creado_por=creado_por,
                creado=ahora(),
            )
        ).inserted_primary_key[0]


def cambiar_estado_capa(engine: Engine, capa_id: int, nuevo: str) -> None:
    with engine.begin() as con:
        actual = con.execute(select(config_capa.c.estado).where(config_capa.c.id == capa_id)).scalar_one_or_none()
        if actual is None:
            raise ValueError(f"no existe la capa {capa_id}")
        if (actual, nuevo) not in TRANSICIONES:
            raise ValueError(f"la capa {capa_id} está {actual}: no se puede pasar a {nuevo}")
        valores = {"estado": nuevo} | ({"publicado": ahora()} if nuevo == "publicada" else {})
        con.execute(config_capa.update().where(config_capa.c.id == capa_id).values(**valores))


def capa(engine: Engine, capa_id: int) -> dict | None:
    with engine.connect() as con:
        fila = con.execute(select(config_capa).where(config_capa.c.id == capa_id)).mappings().first()
    return dict(fila) if fila else None


def listar_capas(engine: Engine, hospital_id: str | None = None) -> list[dict]:
    consulta = select(config_capa).order_by(config_capa.c.id)
    if hospital_id is not None:
        consulta = consulta.where(config_capa.c.hospital_id.is_(None) | (config_capa.c.hospital_id == hospital_id))
    with engine.connect() as con:
        return [dict(f) for f in con.execute(consulta).mappings()]


def a_capa(fila: dict) -> CapaConfig:
    return CapaConfig(
        id=fila["id"], hospital_id=fila["hospital_id"], eps_nit=fila["eps_nit"],
        vigente_desde=fila["vigente_desde"], vigente_hasta=fila["vigente_hasta"], datos=fila["datos"],
    )


def capas_publicadas(engine: Engine, hospital_id: str | None) -> list[CapaConfig]:
    """Las capas publicadas que pueden aplicar a ese hospital (las globales y las suyas)."""
    filas = listar_capas(engine, hospital_id)
    return [a_capa(f) for f in filas if f["estado"] == "publicada" and f["hospital_id"] in (None, hospital_id)]


# --- corridas y resultados ---------------------------------------------------


def iniciar_corrida(engine: Engine, corrida_id: str, version: str) -> None:
    with engine.begin() as con:
        con.execute(insert(corridas).values(id=corrida_id, inicio=ahora(), version_pipeline=version))


def cerrar_corrida(engine: Engine, corrida_id: str, resumen: dict) -> None:
    with engine.begin() as con:
        con.execute(corridas.update().where(corridas.c.id == corrida_id).values(fin=ahora(), resumen=resumen))


def previos_por_cufe(engine: Engine, cufe: str, corrida_id: str, factura: str) -> list[tuple[str, str]]:
    """(corrida_id, numero_factura) ya guardados con el mismo CUFE, incluida la corrida actual
    (otra factura del mismo lote). Excluye solo el registro de esta misma factura en esta corrida."""
    with engine.connect() as con:
        filas = con.execute(
            select(resultados.c.corrida_id, resultados.c.numero_factura).where(
                resultados.c.cufe == cufe,
                not_(and_(resultados.c.corrida_id == corrida_id, resultados.c.numero_factura == factura)),
            )
        ).all()
    return [(f[0], f[1]) for f in filas]


def guardar_resultado(engine: Engine, r: ResultadoFactura) -> None:
    with engine.begin() as con:
        con.execute(
            insert(resultados).values(
                corrida_id=r.corrida_id,
                numero_factura=r.factura,
                cufe=r.cufe,
                estado=r.estado,
                creado=ahora(),
                resultado=r.model_dump(mode="json"),
            )
        )
