"""Configuración: umbrales, campos críticos, severidades y códigos recurrentes.

Todo lo que es regla de negocio vive aquí y no en el código. Los valores por
defecto se pueden sobrescribir con un archivo TOML indicado en la variable de
entorno ``VALIDADOR_CONFIG`` (mismas claves que el modelo ``Config``).

Encima del TOML van las capas de la base de datos (``CapaConfig``), del nivel más general
al más específico: global < EPS < hospital < hospital + EPS. Cada capa trae solo las claves
que cambia; ``ResolutorConfig`` arma la configuración efectiva de cada factura según su
EPS y su fecha.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import motivos

Severidad = Literal["ok", "info", "aviso", "grave"]
Comparador = Literal["exacta", "tokens", "numerica", "fecha", "conjunto"]


class Estricto(BaseModel):
    # Una clave mal escrita en el TOML o en una capa es un error, no un valor que se ignora en silencio.
    model_config = ConfigDict(extra="forbid")


class ReglaCampo(Estricto):
    comparador: Comparador = "exacta"
    si_difiere: Severidad = "grave"
    si_falta_en_todas: Severidad = "info"


class ReglasTarifa(Estricto):
    # Puntaje mínimo de similitud de descripción (0-1) para aceptar un ítem del tarifario.
    score_minimo: float = 0.5
    # Ventaja mínima del mejor candidato sobre el segundo cuando hay varios con el mismo CUPS.
    margen_minimo: float = 0.1
    # La base de tarifas actual es parcial (anexos institucionales), por eso no es grave.
    si_valor_difiere: Severidad = "aviso"
    # Diferencias de redondeo (la tarifa se redondea a la centena): no se reportan.
    tolerancia_pesos: int = 100
    si_no_determinable: Severidad = "info"
    si_no_en_tarifario: Severidad = "info"


class ReglaDocumentos(Estricto):
    """Documentos mínimos para un grupo de facturas (EPS x tipo de servicio).

    De todas las reglas que aplican a una factura se usa la más específica: EPS y servicio
    concretos > solo EPS > solo servicio > general ("*"). Ante empate gana la primera de la lista.
    """

    nombre: str
    # NIT de la EPS sin dígito de verificación, o "*" para cualquiera.
    eps: list[str] = ["*"]
    # Tipos de servicio del RIPS (consultas, procedimientos, medicamentos, otrosServicios), o "*".
    # Aplica si la factura trae al menos uno de ellos.
    servicios: list[str] = ["*"]
    # Documento -> severidad si falta. Prefijo del PDF (FEV, HEV, PDE, PDX...), "RIPS" o "CUV".
    # El XML no va aquí: sin XML ni RIPS no hay fuente estructurada y siempre es grave.
    documentos: dict[str, Severidad]

    def aplica(self, eps: str | None, servicios: set[str]) -> bool:
        return ("*" in self.eps or eps in self.eps) and ("*" in self.servicios or bool(servicios & set(self.servicios)))

    @property
    def especificidad(self) -> int:
        return 2 * ("*" not in self.eps) + ("*" not in self.servicios)


class AjusteRegla(Estricto):
    activa: bool = True
    # None = la severidad que decide la regla (p. ej. la de la matriz de documentos).
    severidad: Severidad | None = None


class Config(Estricto):
    # Orden de preferencia para desempatar y para el valor final.
    # El RIPS (JSON) lo genera el sistema del hospital: es la fuente principal.
    # El XML queda como respaldo cuando no hay RIPS.
    prioridad_fuentes: list[str] = [
        "rips",
        "xml",
        "pdf_factura",
        "cuv",
        "hc",
        "autorizacion_digital",
        "autorizacion_scan",
        "carpeta",
    ]

    campos: dict[str, ReglaCampo] = Field(
        default_factory=lambda: {
            "numero_factura": ReglaCampo(),
            "cufe": ReglaCampo(),
            "nit_emisor": ReglaCampo(),
            "nit_eps": ReglaCampo(),
            "paciente_id": ReglaCampo(),
            "paciente_nombre": ReglaCampo(comparador="tokens", si_difiere="aviso"),
            "numero_autorizacion": ReglaCampo(comparador="conjunto"),
            "id_interno_paciente": ReglaCampo(),
            "valor_total": ReglaCampo(comparador="numerica"),
            "fecha_ingreso": ReglaCampo(comparador="fecha", si_difiere="aviso"),
            "fecha_egreso": ReglaCampo(comparador="fecha", si_difiere="aviso"),
            "fecha_emision": ReglaCampo(comparador="fecha", si_difiere="aviso"),
        }
    )

    # Formatos válidos (regex sobre el valor normalizado). Si no cumple: grave.
    formatos: dict[str, str] = {
        "cufe": r"^[0-9a-f]{96}$",
        "numero_autorizacion": r"^\d{6,12}(,\d{6,12})*$",
        "nit_emisor": r"^\d{6,10}$",
        "nit_eps": r"^\d{6,10}$",
        "paciente_id": r"^[A-Z]{2,3} [0-9A-Z]{3,20}$",
    }

    # Documentos mínimos por EPS y tipo de servicio (ver ReglaDocumentos). La regla general por
    # defecto conserva el comportamiento anterior: sin FEV es grave; sin HEV (normal en apoyo
    # diagnóstico, que trae PDX), PDE, RIPS o CUV (COOSALUD no los pide) solo es info.
    documentos_minimos: list[ReglaDocumentos] = [
        ReglaDocumentos(
            nombre="general",
            documentos={"FEV": "grave", "HEV": "info", "PDE": "info", "RIPS": "info", "CUV": "info"},
        )
    ]
    # Códigos DIAN y CUV que aparecen en casi todas las facturas: info con contador.
    codigos_recurrentes: dict[str, Severidad] = {
        "FAJ43b": "info",
        "RUT01": "info",
        "FED137": "info",
    }
    # Códigos que no están en codigos_recurrentes. Un código DIAN nuevo es aviso hasta que se
    # clasifique; los del CUV quedan en info porque las notificaciones RVC0xx son muy frecuentes.
    dian_codigo_desconocido: Severidad = "aviso"
    cuv_codigo_desconocido: Severidad = "info"
    # Clases del CUV que se consideran rechazo.
    cuv_clases_rechazo: list[str] = ["RECHAZADO"]
    # Respuesta DIAN de documento validado.
    dian_codigos_aceptado: list[str] = ["02"]

    tarifa: ReglasTarifa = ReglasTarifa()

    # Ajustes por código de motivo (base/motivos.py) sobre lo que define cada regla (reglas/):
    # desactivarlo o fijar su severidad. Sin entrada, rige el valor por defecto de la regla.
    # ``validador reglas listar`` muestra los códigos y sus severidades por defecto.
    reglas: dict[str, AjusteRegla] = {}

    # Campos de los PDF digitales: "generica" (localizador por posiciones + diccionario de
    # etiquetas, sin plantillas) o "plantilla" (parsers específicos de este hospital).
    extraccion_pdf: Literal["generica", "plantilla"] = "generica"

    # OCR de la autorización escaneada (cascada PP-OCR + Tesseract).
    ocr_habilitado: bool = True
    ocr_cache_dir: str = "salidas/ocr_cache"
    # El documento de la EPS es la autoridad para ciertos campos: si su lectura es confiable,
    # su valor es la referencia aunque las fuentes digitales (del mismo sistema) digan otra cosa.
    fuente_autoridad: dict[str, str] = {"numero_autorizacion": "autorizacion_scan"}
    min_confianza_autoridad: float = 0.85
    # Solo el escaneo difiere, con lectura de un solo motor, y las digitales coinciden: casi
    # siempre es error de OCR. El plan decía "aviso"; se deja "info" para no llenar la revisión
    # de ruido. Si dos motores confirman la lectura distinta, es "aviso".
    si_ocr_incierto_difiere: Severidad = "info"

    db_url: str = "sqlite:///salidas/validador.db"

    @field_validator("reglas")
    @classmethod
    def _codigos_de_reglas(cls, v: dict[str, AjusteRegla]) -> dict[str, AjusteRegla]:
        for codigo in v:
            motivos.validar(codigo)
            if codigo.startswith("CAMPO."):
                raise ValueError(f"{codigo}: los campos se ajustan en 'campos' (si_difiere, si_falta_en_todas)")
        return v

    def regla_documentos(self, eps: str | None, servicios: set[str]) -> ReglaDocumentos | None:
        """La regla de documentos mínimos más específica que aplica a la factura."""
        candidatas = [r for r in self.documentos_minimos if r.aplica(eps, servicios)]
        return max(candidatas, key=lambda r: r.especificidad, default=None)


# Claves de infraestructura: dependen de dónde corre el proceso, no del hospital ni de la EPS.
# No pueden venir en una capa de la base de datos y no cuentan para el hash de la configuración.
CLAVES_INFRA = frozenset({"db_url", "ocr_cache_dir"})


def combinar(base: dict, cambios: dict) -> dict:
    """Mezcla profunda: los diccionarios se combinan clave por clave; listas y valores se reemplazan.

    Así una capa puede cambiar solo ``tarifa.tolerancia_pesos`` o agregar un código a
    ``codigos_recurrentes``, pero si trae ``documentos_minimos`` define la lista completa.
    """
    out = dict(base)
    for k, v in cambios.items():
        out[k] = combinar(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validar_capa(base: Config, datos: dict) -> Config:
    """Configuración resultante de aplicar una capa sobre la base. Lanza ValueError si no es válida."""
    if prohibidas := CLAVES_INFRA & datos.keys():
        raise ValueError(f"claves de infraestructura no permitidas en una capa: {', '.join(sorted(prohibidas))}")
    return Config.model_validate(combinar(base.model_dump(), datos))


def huella(cfg: Config) -> str:
    """Hash corto de la configuración efectiva: dos resultados con la misma huella se validaron con las mismas reglas."""
    return hashlib.sha256(cfg.model_dump_json(exclude=set(CLAVES_INFRA)).encode()).hexdigest()[:16]


class CapaConfig(BaseModel):
    """Una versión publicada de la configuración de un ámbito (global, EPS, hospital u hospital + EPS).

    Cada versión es completa para su ámbito: la nueva reemplaza a la anterior, no se suma.
    Para volver atrás se retira la nueva y vuelve a regir la anterior.
    """

    id: int
    hospital_id: str | None = None
    eps_nit: str | None = None
    vigente_desde: date
    vigente_hasta: date | None = None
    datos: dict

    @property
    def nivel(self) -> int:
        return 2 * (self.hospital_id is not None) + (self.eps_nit is not None)

    def cubre(self, fecha: date) -> bool:
        return self.vigente_desde <= fecha and (self.vigente_hasta is None or fecha <= self.vigente_hasta)


@dataclass(frozen=True)
class ConfigResuelta:
    cfg: Config
    capas: tuple[int, ...]
    huella: str


class ResolutorConfig:
    """Configuración efectiva por factura: base (TOML) + capas vigentes en la fecha de la factura."""

    def __init__(
        self, base: Config, capas: list[CapaConfig] | None = None, hospital_id: str | None = None, forzado: dict | None = None
    ):
        self.base = base
        self.hospital_id = hospital_id
        # Se aplica después de todas las capas (p. ej. --sin-ocr en la línea de comandos).
        self.forzado = forzado or {}
        self.capas = [c for c in capas or [] if c.hospital_id in (None, hospital_id)]
        self._cache: dict[tuple[int, ...], ConfigResuelta] = {}

    def para(self, eps: str | None, fecha: date | None) -> ConfigResuelta:
        fecha = fecha or date.today()
        vigentes: dict[tuple[str | None, str | None], CapaConfig] = {}
        for c in self.capas:
            if c.eps_nit not in (None, eps) or not c.cubre(fecha):
                continue
            ambito = (c.hospital_id, c.eps_nit)
            # Dos versiones del mismo ámbito vigentes a la vez: rige la que empezó después (y, si
            # empezaron el mismo día, la creada después).
            if ambito not in vigentes or (c.vigente_desde, c.id) > (vigentes[ambito].vigente_desde, vigentes[ambito].id):
                vigentes[ambito] = c
        orden = sorted(vigentes.values(), key=lambda c: c.nivel)
        clave = tuple(c.id for c in orden)
        if clave not in self._cache:
            datos = self.base.model_dump()
            for c in orden:
                datos = combinar(datos, c.datos)
            cfg = Config.model_validate(combinar(datos, self.forzado))
            self._cache[clave] = ConfigResuelta(cfg, clave, huella(cfg))
        return self._cache[clave]


def _cargar() -> Config:
    ruta = os.environ.get("VALIDADOR_CONFIG")
    datos: dict = {}
    if ruta:
        datos = tomllib.loads(Path(ruta).read_text(encoding="utf-8"))
    if url := os.environ.get("VALIDADOR_DB_URL"):
        datos["db_url"] = url
    return Config.model_validate(datos)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return _cargar()
