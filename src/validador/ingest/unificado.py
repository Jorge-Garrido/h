"""PDF unificado: todos los soportes de la factura en un solo archivo sin prefijo
(p. ej. ``FE4070942.pdf`` con factura + autorización + historia clínica).

Cada página se clasifica por su contenido (textos ``clasificar`` del catálogo) y se arman PDF
separados (FEV, HEV, PDE...) para que el resto del pipeline los trate como documentos normales.
Las páginas sin texto digital se clasifican con OCR; las que no se reconocen continúan el
documento de la página anterior.

``tipo_por_contenido`` usa lo mismo con la primera página para reconocer un PDF suelto con un
prefijo raro o sin prefijo.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

from .. import catalogo
from ..catalogo import TipoDocumento
from ..ocr import imagenes, motor


def _texto_pagina(doc: pymupdf.Document, i: int, ruta: Path, ocr: bool = True) -> str:
    texto = doc[i].get_text()
    if len(texto.strip()) >= 50 or not ocr:
        return texto
    imgs = [im for im in imagenes.extraer(ruta, max_paginas=i + 1) if im.pagina == i + 1]
    if not imgs:
        return texto
    return "\n".join(l.texto for l in motor.leer(imgs[0].imagen).lineas)


def clasificar_paginas(ruta: Path) -> list[str]:
    clases: list[str] = []
    with pymupdf.open(ruta) as doc:
        for i in range(len(doc)):
            tipo = catalogo.get().clasificar(_texto_pagina(doc, i, ruta))
            clases.append(tipo.prefijo if tipo else (clases[-1] if clases else "OTRO"))
    return clases


def texto_primera_pagina(ruta: Path, ocr: bool = False) -> str:
    """Texto de la primera página (con OCR solo si se pide y la página no tiene texto digital)."""
    try:
        with pymupdf.open(ruta) as doc:
            return _texto_pagina(doc, 0, ruta, ocr) if len(doc) else ""
    except Exception:  # PDF dañado: lo reporta el lector del documento
        return ""


def tipo_por_contenido(ruta: Path, ocr: bool = False) -> TipoDocumento | None:
    return catalogo.get().clasificar(texto_primera_pagina(ruta, ocr))


def _rangos(paginas: list[int]) -> str:
    """[1, 2, 3, 6] -> '1-3, 6'"""
    partes, ini = [], None
    for i, p in enumerate(paginas):
        if ini is None:
            ini = p
        if i + 1 == len(paginas) or paginas[i + 1] != p + 1:
            partes.append(f"{ini}-{p}" if ini != p else str(p))
            ini = None
    return ", ".join(partes)


def separar(ruta: Path, destino: Path) -> dict[str, tuple[Path, str]]:
    """Devuelve {prefijo: (pdf separado, etiqueta 'FE123.pdf, págs. 1-2')}."""
    clave = hashlib.sha256(ruta.read_bytes()).hexdigest()[:16]
    carpeta = destino / clave
    clases = clasificar_paginas(ruta)
    por_clase: dict[str, list[int]] = {}
    for i, c in enumerate(clases, 1):
        por_clase.setdefault(c, []).append(i)
    resultado: dict[str, tuple[Path, str]] = {}
    with pymupdf.open(ruta) as src:
        for clase, paginas in por_clase.items():
            if clase == "OTRO":
                continue
            salida = carpeta / f"{clase}_{ruta.stem}.pdf"
            if not salida.exists():
                salida.parent.mkdir(parents=True, exist_ok=True)
                nuevo = pymupdf.open()
                for p in paginas:
                    nuevo.insert_pdf(src, from_page=p - 1, to_page=p - 1)
                nuevo.save(salida)
            resultado[clase] = (salida, f"{ruta.name}, págs. {_rangos(paginas)}")
    return resultado
