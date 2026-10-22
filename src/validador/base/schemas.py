"""Modelos pydantic: lo extraído de cada fuente y el resultado por factura."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .. import SCHEMA_VERSION, __version__
from . import motivos
from .config import Severidad

Fuente = Literal[
    "rips",
    "xml",
    "xml_nota",
    "cuv",
    "pdf_factura",
    "hc",
    "autorizacion_digital",
    "autorizacion_scan",
    "carpeta",
]
Etapa = Literal["digital", "paddle", "tesseract", "digitos", "llm", "humano"]


class CampoExtraido(BaseModel):
    campo: str
    valor: str | None
    valor_crudo: str | None = None
    fuente: Fuente
    pagina: int | None = None
    bbox: list[float] | None = None
    confianza: float = 1.0
    etapa: Etapa = "digital"


class LineaServicio(BaseModel):
    """Un servicio facturado tal como aparece en una fuente."""

    codigo: str | None
    # De dónde salió el código: rips.codProcedimiento, xml.buyers, xml.descripcion...
    origen_codigo: str
    tipo_servicio: str | None = None
    descripcion: str | None = None
    cantidad: Decimal = Decimal(1)
    valor_total: Decimal
    numero_autorizacion: str | None = None
    fecha: date | None = None
    fuente: Fuente


class Atencion(BaseModel):
    """Una nota de la historia clínica (fecha, especialidad, tipo de consulta)."""

    fecha: date | None
    tipo_nota: str | None = None
    especialidad: str | None = None
    tipo_consulta: str | None = None
    pagina: int | None = None


class CodigoValidacion(BaseModel):
    origen: Literal["dian", "cuv"]
    codigo: str
    clase: str | None = None
    descripcion: str | None = None
    ruta: str | None = None


class DocumentoExtraido(BaseModel):
    """Todo lo que se sacó de una fuente."""

    fuente: Fuente
    archivo: str | None = None
    sha256: str | None = None
    campos: dict[str, CampoExtraido] = Field(default_factory=dict)
    lineas: list[LineaServicio] = Field(default_factory=list)
    atenciones: list[Atencion] = Field(default_factory=list)
    codigos: list[CodigoValidacion] = Field(default_factory=list)
    extra: dict[str, str | bool | None] = Field(default_factory=dict)
    paginas_sin_texto: list[int] = Field(default_factory=list)
    requiere_ocr: bool = False
    errores: list[str] = Field(default_factory=list)

    def poner(self, campo: str, valor: str | None, crudo: str | None = None, pagina: int | None = None) -> None:
        if valor is None or valor == "":
            return
        self.campos[campo] = CampoExtraido(
            campo=campo, valor=valor, valor_crudo=crudo if crudo is not None else valor, fuente=self.fuente, pagina=pagina
        )


class ResultadoCampo(BaseModel):
    campo: str
    valor_final: str | None
    valores_por_fuente: dict[str, str | None]
    resultado: Severidad
    motivo: str | None = None
    # Códigos del catálogo (base/motivos.py): lo que se guarda y se cuenta, sin datos del paciente.
    codigos: list[str] = Field(default_factory=list)
    etapa_resolucion: Etapa = "digital"
    documento_sospechoso: str | None = None
    distancia: int | None = None
    confianza: float = 1.0

    @field_validator("codigos")
    @classmethod
    def _codigos_conocidos(cls, v: list[str]) -> list[str]:
        return [motivos.validar(c) for c in v]


class ResultadoTarifa(BaseModel):
    estado: Literal["coincide", "valor_difiere", "no_determinable", "no_en_tarifario", "sin_tarifario"]
    metodo: Literal["unico_por_cups", "por_descripcion", "por_valor", "ninguno"] = "ninguno"
    codigo_institucional: str | None = None
    descripcion_tarifa: str | None = None
    valor_unitario_tarifa: Decimal | None = None
    valor_unitario_facturado: Decimal | None = None
    score: float | None = None
    candidatos: int = 0
    tarifario_version_id: int | None = None


class ResultadoLinea(BaseModel):
    codigo: str
    tipo_servicio: str | None = None
    descripcion: str | None = None
    cantidad_por_fuente: dict[str, Decimal]
    valor_por_fuente: dict[str, Decimal]
    tarifa: ResultadoTarifa | None = None
    resultado: Severidad
    motivos: list[str] = Field(default_factory=list)
    codigos: list[str] = Field(default_factory=list)
    documento_sospechoso: str | None = None

    @field_validator("codigos")
    @classmethod
    def _codigos_conocidos(cls, v: list[str]) -> list[str]:
        return [motivos.validar(c) for c in v]


class Hallazgo(BaseModel):
    """Verificación que no es un campo comparado (HC, documentos, códigos DIAN/CUV...)."""

    tipo: str
    # Código del catálogo de motivos y referencia no sensible (campo, CUPS, prefijo, código DIAN).
    codigo: str
    ref: str | None = None
    resultado: Severidad
    # Texto con el detalle (puede traer valores): solo para el JSON y los reportes de revisión.
    motivo: str
    documento_sospechoso: str | None = None

    @field_validator("codigo")
    @classmethod
    def _codigo_conocido(cls, v: str) -> str:
        return motivos.validar(v)


class PropuestaCorreccion(BaseModel):
    campo: str
    documento: str
    valor_actual: str | None
    valor_propuesto: str | None
    accion: str


class ResultadoFactura(BaseModel):
    schema_version: str = SCHEMA_VERSION
    version_pipeline: str = __version__
    corrida_id: str
    factura: str
    # Carpeta original de la factura (para copiarla a revisión). No es dato del paciente.
    ruta_origen: str | None = None
    cufe: str | None
    estado: Severidad
    fuente_principal: Literal["rips", "xml", "pdf_factura", "ninguna"]
    documentos_presentes: dict[str, list[str]]
    documentos_faltantes: list[str]
    requiere_ocr: list[str]
    campos: list[ResultadoCampo]
    lineas: list[ResultadoLinea]
    hallazgos: list[Hallazgo]
    dian: dict[str, str | list[str] | None]
    cuv: dict[str, str | bool | list[str] | None]
    propuestas_correccion: list[PropuestaCorreccion] = Field(default_factory=list)
    tiempos_ms: dict[str, int]
    tarifario_version_id: int | None = None
    # Con qué reglas se validó: hash de la configuración efectiva y capas de la BD que la formaron.
    hospital_id: str | None = None
    config_huella: str | None = None
    config_capas: list[int] = Field(default_factory=list)
