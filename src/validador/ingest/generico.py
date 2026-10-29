"""Campos de un PDF digital con el localizador genérico (sin reglas de plantilla).

Principio: los valores ya vienen de las fuentes estructuradas (RIPS/XML). En cada
PDF se **verifica** si esos valores están junto a la etiqueta del campo; si no están,
se **extrae** lo que haya junto a la etiqueta para poder mostrar la diferencia.

Qué campos buscar y cuántas páginas leer lo dice el catálogo (``campos``, ``max_paginas``).
Las etiquetas son las del catálogo más, delante, las del formato detectado (el sistema que
generó el documento) y las del hospital (``Config.documentos``).
"""

from __future__ import annotations

from pathlib import Path

from .. import catalogo, localizar
from ..base.config import AjusteDocumento
from ..base.schemas import CampoExtraido, DocumentoExtraido
from ..catalogo import TipoDocumento
from ..data_preprocessing import layout, normalize
from . import pdf_text
from .util import sha256

# Lo que se busca en un PDF que no está en el catálogo: solo para verificar que es del paciente.
CAMPOS_IDENTIDAD = ["paciente_id", "paciente_nombre"]


def normalizar(campo: str, texto: str | None) -> str | None:
    if not texto:
        return None
    tipo = localizar.TIPOS[campo]
    if campo.startswith("nit"):
        return normalize.nit(texto)
    if campo == "cufe":
        return localizar._compacto(texto).lower()
    if tipo == "codigo":
        return localizar._compacto(texto)
    if tipo == "documento":
        return normalize.documento_id_texto(texto)
    if tipo == "nombre":
        return normalize.nombre(texto)
    if tipo == "dinero":
        return normalize.dinero_texto(normalize.dinero(texto))
    if tipo == "fecha":
        f = normalize.fecha(texto.replace("/", "-")) if texto[:4].isdigit() else normalize.fecha(texto)
        return f.isoformat() if f else None
    return texto


def campo(
    doc: layout.DocLayout, nombre: str, fuente: str, esperados: set[str], etiquetas: list[str] = ()
) -> CampoExtraido | None:
    base = {"campo": nombre, "fuente": fuente}
    diferencias: list[localizar.Verificacion] = []
    sin_contexto: list[tuple[str, localizar.Verificacion]] = []
    for esperado in sorted(esperados):
        v = localizar.verificar(doc, nombre, esperado, etiquetas)
        if v.estado == "confirmado":
            return CampoExtraido(valor=esperado, valor_crudo=v.valor_encontrado, pagina=v.pagina, bbox=list(v.bbox), **base)
        if v.estado == "diferencia":
            diferencias.append(v)
        elif v.estado == "confirmado_sin_contexto":
            sin_contexto.append((esperado, v))
    if diferencias:
        v = min(diferencias, key=lambda d: d.distancia or 0)
        return CampoExtraido(
            valor=normalizar(nombre, v.valor_encontrado), valor_crudo=v.valor_encontrado,
            pagina=v.pagina, bbox=list(v.bbox) if v.bbox else None, confianza=0.9, **base,
        )
    extraido = localizar.extraer(doc, nombre, etiquetas)
    if extraido:
        return CampoExtraido(valor=normalizar(nombre, extraido), valor_crudo=extraido, confianza=0.8, **base)
    if sin_contexto:
        # El valor está en el documento pero lejos de cualquier etiqueta del campo.
        esperado, v = sin_contexto[0]
        return CampoExtraido(valor=esperado, valor_crudo=v.valor_encontrado, pagina=v.pagina, confianza=0.5, **base)
    return None


def leer(ruta: Path, fuente: str) -> DocumentoExtraido:
    """Documento sin lector propio (lector = "generico" o fuera del catálogo): solo se revisa
    que sea legible; los campos los pone ``completar``."""
    doc = DocumentoExtraido(fuente=fuente, archivo=ruta.name, sha256=sha256(ruta))
    try:
        pdf = pdf_text.leer(ruta)
    except Exception as e:
        doc.errores.append(f"PDF ilegible: {type(e).__name__}")
        return doc
    doc.paginas_sin_texto = pdf.paginas_sin_texto
    doc.requiere_ocr = pdf.todo_escaneado
    return doc


def completar(
    doc_extraido: DocumentoExtraido,
    ruta: Path,
    esperados: dict[str, set[str]],
    tipo: TipoDocumento | None = None,
    ajuste: AjusteDocumento | None = None,
) -> DocumentoExtraido:
    """Reemplaza los campos del documento por los del localizador genérico. Sin ``tipo`` (PDF
    fuera del catálogo) se buscan solo los campos de identidad en la primera página."""
    tipo = tipo or catalogo.get().por_fuente(doc_extraido.fuente)
    if doc_extraido.errores or (
        doc_extraido.requiere_ocr and not doc_extraido.campos and (tipo is None or tipo.lector != "autorizacion")
    ):
        return doc_extraido  # PDF ilegible o escaneado: pendiente de OCR
    lay = layout.desde_pdf(ruta, tipo.max_paginas if tipo else 1)
    formato = None
    if tipo is not None:
        texto = normalize.texto(" ".join(p.texto for p in lay.palabras if p.pagina == 1)) or ""
        if formato := tipo.formato(texto, ajuste.formatos if ajuste else []):
            doc_extraido.extra["formato"] = formato.nombre
    campos = {}
    for nombre in tipo.campos if tipo else CAMPOS_IDENTIDAD:
        etiquetas = [
            *(formato.etiquetas.get(nombre, []) if formato else []),
            *(ajuste.etiquetas.get(nombre, []) if ajuste else []),
        ]
        if c := campo(lay, nombre, doc_extraido.fuente, esperados.get(nombre, set()), etiquetas):
            campos[nombre] = c
    doc_extraido.campos = campos
    return doc_extraido
