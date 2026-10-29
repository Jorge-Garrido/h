"""Catálogo de documentos, fuentes y campos (``catalogo.toml``).

Reemplaza las tablas que antes estaban repetidas en el código (prefijos en discover, campos por
documento en generico, palabras de clasificación en unificado, etiquetas en localizar, nombres en
salida_errores y explicar, acciones en corrections). Un documento nuevo se agrega aquí, sin tocar
el pipeline, si el localizador genérico lo puede leer.

El catálogo se valida completo al cargarlo: alias repetidos, fuentes o campos que no existen,
palabras de clasificación ambiguas... son errores de arranque, no de una factura.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..data_preprocessing import normalize

RUTA = Path(__file__).with_name("catalogo.toml")

TipoCampo = Literal["codigo", "documento", "nombre", "dinero", "fecha"]
Lector = Literal["rips", "xml", "cuv", "factura", "hc", "autorizacion", "generico", "ninguno"]
# Fuente de un PDF cuyo prefijo no está en el catálogo: "doc_EPX". Solo verifica identidad, no vota.
PREFIJO_DESCONOCIDA = "doc_"


class Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Campo(Estricto):
    nombre: str
    tipo: TipoCampo
    etiquetas: list[str] = []


class Fuente(Estricto):
    nombre: str  # encabezado de columna en el Excel
    corto: str  # "historia clínica"
    articulo: str  # "la historia clínica"
    documento: str | None = None  # clave del documento en la carpeta (para mostrar el archivo)
    archivo: str = ""  # dónde mirar, en general
    ocr: bool = False
    accion: str = ""  # qué hacer si este documento está mal (explicaciones)
    correccion: str = ""  # propuesta de corrección (corrections/adapter.py)
    donde: dict[str, str] = {}  # campo -> "dónde mirar" con {FEV}, {HEV}... como archivo


def _normalizar_textos(v: list[str]) -> list[str]:
    return [normalize.texto(t) or "" for t in v]


class Formato(Estricto):
    """Variante de un documento según el sistema que lo genera (p. ej. la epicrisis de cada HIS)."""

    nombre: str
    detectar: list[str] = Field(min_length=1)  # todos deben estar en la primera página
    etiquetas: dict[str, list[str]] = {}

    @field_validator("detectar")
    @classmethod
    def _normalizar(cls, v: list[str]) -> list[str]:
        return _normalizar_textos(v)

    def coincide(self, texto: str) -> bool:
        return all(t in texto for t in self.detectar)


class TipoDocumento(Estricto):
    nombre: str
    lector: Lector
    fuente: str | None = None
    alias: list[str] = []
    campos: list[str] = []
    vota: bool = True
    max_paginas: int | None = None
    clasificar: list[str] = []
    formatos: list[Formato] = []
    prefijo: str = ""  # lo llena el catálogo con la clave

    @field_validator("clasificar")
    @classmethod
    def _normalizar(cls, v: list[str]) -> list[str]:
        return _normalizar_textos(v)

    @property
    def es_pdf(self) -> bool:
        return self.lector not in ("rips", "xml", "cuv")

    def formato(self, texto: str, extra: list[Formato] = ()) -> Formato | None:
        """Primer formato (del hospital, luego del catálogo) cuyos textos están en la primera página."""
        return next((f for f in [*extra, *self.formatos] if f.coincide(texto)), None)


class Catalogo(Estricto):
    campos: dict[str, Campo]
    fuentes: dict[str, Fuente]
    documentos: dict[str, TipoDocumento]

    @model_validator(mode="after")
    def _consistente(self) -> Catalogo:
        errores: list[str] = []
        vistos: dict[str, str] = {}
        palabras: dict[str, str] = {}
        for prefijo, t in self.documentos.items():
            t.prefijo = prefijo
            if t.fuente is not None and t.fuente not in self.fuentes:
                errores.append(f"documento {prefijo}: fuente '{t.fuente}' no existe")
            if t.lector != "ninguno" and t.fuente is None:
                errores.append(f"documento {prefijo}: el lector '{t.lector}' necesita una fuente")
            for c in t.campos + [c for f in t.formatos for c in f.etiquetas]:
                if c not in self.campos:
                    errores.append(f"documento {prefijo}: campo '{c}' no existe")
            for clave in [prefijo, *t.alias]:
                if clave in vistos:
                    errores.append(f"prefijo o alias '{clave}' repetido en {vistos[clave]} y {prefijo}")
                vistos[clave] = prefijo
            for p in t.clasificar:
                if p in palabras:
                    errores.append(f"texto de clasificación '{p}' repetido en {palabras[p]} y {prefijo}")
                palabras[p] = prefijo
        fuentes_documento = [t.fuente for t in self.documentos.values() if t.fuente]
        for f in {f for f in fuentes_documento if fuentes_documento.count(f) > 1}:
            errores.append(f"la fuente '{f}' la alimentan dos documentos")
        for nombre, f in self.fuentes.items():
            if f.documento is not None and f.documento not in self.documentos:
                errores.append(f"fuente {nombre}: documento '{f.documento}' no existe")
            for c in f.donde:
                if c not in self.campos:
                    errores.append(f"fuente {nombre}: campo '{c}' no existe en 'donde'")
        if errores:
            raise ValueError("catálogo inválido:\n  " + "\n  ".join(errores))
        return self

    # --- consultas -------------------------------------------------------------------------

    def documento(self, prefijo: str) -> TipoDocumento | None:
        """Por prefijo o alias del catálogo (los alias del hospital se resuelven en discover)."""
        prefijo = prefijo.upper()
        if prefijo in self.documentos:
            return self.documentos[prefijo]
        return next((t for t in self.documentos.values() if prefijo in t.alias), None)

    def alias(self) -> dict[str, str]:
        return {a: t.prefijo for t in self.documentos.values() for a in t.alias}

    def pdfs(self) -> list[TipoDocumento]:
        return [t for t in self.documentos.values() if t.es_pdf]

    def por_fuente(self, fuente: str) -> TipoDocumento | None:
        return next((t for t in self.documentos.values() if t.fuente == fuente), None)

    def fuentes_ocr(self) -> set[str]:
        return {n for n, f in self.fuentes.items() if f.ocr}

    def clasificar(self, texto: str) -> TipoDocumento | None:
        """Documento al que corresponde una página por su contenido: el que tiene más textos
        de clasificación en ella; ante empate, el que va antes en el catálogo."""
        t = normalize.texto(texto) or ""
        puntajes = [(sum(p in t for p in d.clasificar), -i, d) for i, d in enumerate(self.pdfs()) if d.clasificar]
        mejor = max(puntajes, key=lambda x: x[:2], default=None)
        return mejor[2] if mejor and mejor[0] > 0 else None

    def fuente(self, nombre: str) -> Fuente:
        """Datos para mostrar una fuente. Una fuente sin catálogo ("doc_EPX") se muestra con su prefijo."""
        if nombre in self.fuentes:
            return self.fuentes[nombre]
        prefijo = nombre.removeprefix(PREFIJO_DESCONOCIDA)
        return Fuente(nombre=f"Documento {prefijo}", corto=f"documento {prefijo}", articulo=f"el documento {prefijo}",
                      documento=prefijo, archivo=prefijo)


def fuente_desconocida(prefijo: str) -> str:
    return f"{PREFIJO_DESCONOCIDA}{prefijo}"


def cargar(ruta: Path = RUTA) -> Catalogo:
    return Catalogo.model_validate(tomllib.loads(ruta.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def get() -> Catalogo:
    return cargar()


def fuente_valida(nombre: str) -> bool:
    return nombre in get().fuentes or (nombre.startswith(PREFIJO_DESCONOCIDA) and len(nombre) > len(PREFIJO_DESCONOCIDA))
