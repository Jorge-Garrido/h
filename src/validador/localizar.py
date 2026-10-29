"""Localizador genérico: funciona con cualquier formato usando solo posiciones y un
diccionario de etiquetas (sinónimos), sin reglas por plantilla.

Dos modos:

- ``verificar``: ya conocemos el valor esperado (del RIPS/XML). Se busca en el
  documento exacto, contenido (etiqueta pegada: 'No.FE123') o con distancia pequeña,
  y se exige una etiqueta del campo cerca (misma fila a la izquierda, o una o dos
  filas arriba) para aceptar que es ese campo y no otro número parecido.
- ``extraer``: sin valor esperado. Se busca la etiqueta y se toma el valor a su
  derecha o debajo, filtrado por el tipo del campo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from . import catalogo
from .data_preprocessing import normalize
from .compare import levenshtein
from .data_preprocessing.layout import DocLayout, Fila, Palabra

TipoCampo = Literal["codigo", "documento", "nombre", "dinero", "fecha"]

# Tipo y diccionario genérico de etiquetas (sinónimos habituales en documentos de salud en
# Colombia) de cada campo: vienen del catálogo (catalogo/catalogo.toml). No describen ninguna
# plantilla en particular; las de un formato o un hospital llegan en ``extra``.
TIPOS: dict[str, TipoCampo] = {n: c.tipo for n, c in catalogo.get().campos.items()}
ETIQUETAS: dict[str, list[str]] = {n: list(c.etiquetas) for n, c in catalogo.get().campos.items()}

# Cercanía aceptada entre la etiqueta y el valor (puntos PDF).
MAX_DISTANCIA_H = 300
FILAS_ABAJO = 2
# Campos que viven en una columna de tabla: el valor puede estar muchas filas debajo del encabezado.
FILAS_ABAJO_CAMPO = {"cups": 25}
MAX_DIST_EDICION = 1

Estado = Literal["confirmado", "confirmado_sin_contexto", "diferencia", "parcial", "ambiguo", "no_encontrado"]


@dataclass
class Ancla:
    texto: str
    x0: float
    y0: float
    x1: float
    y1: float
    fila: int
    pagina: int
    dos_puntos: bool
    palabras: list[Palabra]
    rango: int = 0  # posición de la etiqueta en el diccionario (0 = más específica)
    campo: str = ""


@dataclass
class Verificacion:
    campo: str
    estado: Estado
    valor_encontrado: str | None = None
    distancia: int | None = None
    pagina: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    etiqueta: str | None = None
    detalle: str | None = None
    candidatos: list[str] = field(default_factory=list)


# --- utilidades --------------------------------------------------------------


def _compacto(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", normalize.texto(s) or "")


def _piezas_etiqueta(p: Palabra) -> list[str]:
    """'No.FE123' -> ['NO', 'FE123']; 'AUTORIZACIÓN:' -> ['AUTORIZACION']; 'NIT/ID:' -> ['NIT/ID']."""
    t = normalize.texto(p.texto) or ""
    return [x for x in re.split(r"[:.]", t) if x]


def anclas(doc: DocLayout, campo: str, extra: list[str] = ()) -> list[Ancla]:
    """Todas las apariciones de las etiquetas del campo, más específicas primero. ``extra`` son
    las etiquetas del formato o del hospital: van antes que las genéricas del catálogo."""
    resultado: list[Ancla] = []
    lista = list(dict.fromkeys([*extra, *ETIQUETAS.get(campo, [])]))
    for etiqueta in sorted(lista, key=lambda e: -len(e)):
        objetivo = etiqueta.split()
        for fila in doc.filas:
            piezas: list[tuple[str, Palabra]] = [(pz, p) for p in fila.palabras for pz in _piezas_etiqueta(p)]
            for i in range(len(piezas) - len(objetivo) + 1):
                tramo = [pz for pz, _ in piezas[i : i + len(objetivo)]]
                if len(tramo) < len(objetivo) or not all(
                    pz == tok
                    or (len(tok) >= 7 and abs(len(pz) - len(tok)) <= 1 and levenshtein(pz, tok) <= 1)  # 'AUTORIZACLON' (OCR)
                    or (k == len(objetivo) - 1 and len(tok) >= 5 and pz.startswith(tok) and pz[len(tok):].isalpha())
                    for k, (pz, tok) in enumerate(zip(tramo, objetivo))
                ):
                    continue  # la última palabra puede venir pegada a la siguiente por el OCR: 'CODIGOCANT'
                pals = []
                for _, p in piezas[i : i + len(objetivo)]:
                    if p not in pals:
                        pals.append(p)
                if any(a.palabras[0] is pals[0] and len(a.palabras) >= len(pals) for a in resultado):
                    continue  # ya cubierta por una etiqueta más larga
                resultado.append(
                    Ancla(
                        texto=etiqueta,
                        x0=pals[0].x0,
                        y0=min(p.y0 for p in pals),
                        x1=pals[-1].x1,
                        y1=max(p.y1 for p in pals),
                        fila=fila.indice,
                        pagina=fila.pagina,
                        dos_puntos=pals[-1].texto.rstrip().endswith(":"),
                        palabras=pals,
                        rango=lista.index(etiqueta),
                        campo=campo,
                    )
                )
    return resultado


def _cerca(ancla: Ancla, palabras: list[Palabra]) -> bool:
    p0 = palabras[0]
    if p0.pagina != ancla.pagina:
        return False
    if any(p is a for p in palabras for a in ancla.palabras):
        return True  # etiqueta pegada al valor: 'No.FE123'
    if p0.fila == ancla.fila or _solapa_vertical(ancla, palabras):
        # A la derecha de la etiqueta. Se usa solape vertical y no solo "misma fila":
        # un valor en dos líneas puede quedar con la etiqueta centrada entre ellas.
        return ancla.x1 <= p0.x0 + 2 and p0.x0 - ancla.x1 <= MAX_DISTANCIA_H
    if 0 < p0.fila - ancla.fila <= FILAS_ABAJO_CAMPO.get(ancla.campo, FILAS_ABAJO):
        if ancla.campo in FILAS_ABAJO_CAMPO:
            return True  # columna de tabla: el encabezado suele ir centrado sobre la celda
        return ancla.x0 - 40 <= p0.x0 <= ancla.x1 + 150
    return False


def _solapa_vertical(ancla: Ancla, palabras: list[Palabra]) -> bool:
    for p in palabras:
        solape = min(ancla.y1, p.y1) - max(ancla.y0, p.y0)
        if solape > 0.3 * min(ancla.y1 - ancla.y0, p.y1 - p.y0):
            return True
    return False


def _con_contexto(anc: list[Ancla], palabras: list[Palabra]) -> Ancla | None:
    return next((a for a in anc if _cerca(a, palabras)), None)


@dataclass
class _Candidato:
    compacto: str
    palabras: list[Palabra]

    @property
    def texto(self) -> str:
        return " ".join(p.texto for p in self.palabras)


def _candidatos_codigo(doc: DocLayout) -> list[_Candidato]:
    cands: list[_Candidato] = []
    filas = doc.filas
    for i, fila in enumerate(filas):
        ps = fila.palabras
        for j, p in enumerate(ps):
            cands.append(_Candidato(_compacto(p.texto), [p]))
            # '891.800.231-0' -> '891.800.231' (NIT sin DV); 'No.FE123' -> 'FE123' (etiqueta pegada)
            partes = {x for x in re.split(r"[-:/;,]", p.texto) if x}
            if re.search(r"[A-Za-z]", p.texto):
                partes |= {x for x in re.split(r"[-:/;,.]", p.texto) if x}
            if len(partes) > 1:
                cands.extend(_Candidato(_compacto(x), [p]) for x in partes)
            for k in (2, 3):  # números o documentos partidos en varias palabras: 'CC 123', '313 558 347'
                if j + k <= len(ps):
                    cands.append(_Candidato("".join(_compacto(x.texto) for x in ps[j : j + k]), ps[j : j + k]))
        # tipo de documento al final de una fila y el número en la siguiente: '..., CC' / '1049613198,'
        for p in ps:
            if re.fullmatch(r"(CC|TI|RC|CE|CN|PT|PA|PE|MS|AS|SC)[.:]?", normalize.texto(p.texto) or "") and i + 1 < len(filas):
                for q in filas[i + 1].palabras:
                    if filas[i + 1].pagina == fila.pagina and re.match(r"\d{4,}", q.texto):
                        cands.append(_Candidato(_compacto(p.texto) + _compacto(q.texto), [p, q]))
        # valor que continúa en la fila siguiente (CUFE partido)
        if i + 1 < len(filas) and filas[i + 1].pagina == fila.pagina and ps and filas[i + 1].palabras:
            a, b = ps[-1], filas[i + 1].palabras[0]
            cands.append(_Candidato(_compacto(a.texto) + _compacto(b.texto), [a, b]))
    return [c for c in cands if c.compacto]


def _bbox(palabras: list[Palabra]) -> tuple[float, float, float, float]:
    return (min(p.x0 for p in palabras), min(p.y0 for p in palabras), max(p.x1 for p in palabras), max(p.y1 for p in palabras))


def _resultado(campo: str, estado: Estado, cand: _Candidato | None, ancla: Ancla | None, **extra) -> Verificacion:
    return Verificacion(
        campo=campo,
        estado=estado,
        valor_encontrado=cand.texto if cand else None,
        pagina=cand.palabras[0].pagina if cand else None,
        bbox=_bbox(cand.palabras) if cand else None,
        etiqueta=ancla.texto if ancla else None,
        **extra,
    )


# --- verificación --------------------------------------------------------------


def verificar(doc: DocLayout, campo: str, esperado: str, extra: list[str] = ()) -> Verificacion:
    tipo = TIPOS[campo]
    anc = anclas(doc, campo, extra)
    if tipo in ("codigo", "documento"):
        return _verificar_codigo(doc, campo, esperado, anc)
    if tipo == "nombre":
        return _verificar_nombre(doc, campo, esperado, anc)
    if tipo == "dinero":
        return _verificar_por_parseo(doc, campo, normalize.dinero(esperado), anc, _dinero_de)
    return _verificar_por_parseo(doc, campo, normalize.fecha(esperado), anc, _fecha_de)


def _contenido_valido(c: str, e: str) -> bool:
    """El valor esperado está dentro del candidato y lo que sobra no son dígitos: es una
    etiqueta o una palabra pegada, no un número más largo."""
    i = c.find(e)
    izq, der = c[:i], c[i + len(e) :]
    if re.search(r"\d", izq + der):
        return False
    if len(izq) + len(der) <= 6:
        return True
    # Sobra mucho texto: solo si hay cambio de letras a dígitos en el borde (palabras pegadas).
    return (not izq or izq[-1].isalpha() != e[0].isalpha()) and (not der or der[0].isalpha() != e[-1].isalpha())


def _verificar_codigo(doc: DocLayout, campo: str, esperado: str, anc: list[Ancla]) -> Verificacion:
    e = _compacto(esperado)
    exactos: list[_Candidato] = []
    cercanos: list[tuple[int, _Candidato]] = []
    for c in _candidatos_codigo(doc):
        if c.compacto == e:
            exactos.append(c)
        elif len(e) >= 5 and e in c.compacto and _contenido_valido(c.compacto, e):
            exactos.append(c)  # etiqueta pegada 'NOFE4420485' o palabra pegada por el OCR 'CC123BUITRAGO'
        elif len(e) >= 6 and abs(len(c.compacto) - len(e)) <= MAX_DIST_EDICION:
            d = levenshtein(c.compacto, e)
            if d <= MAX_DIST_EDICION:
                cercanos.append((d, c))

    for c in exactos:
        if a := _con_contexto(anc, c.palabras):
            return _resultado(campo, "confirmado", c, a, distancia=0)
    cercanos_ctx = [(d, c, a) for d, c in cercanos if (a := _con_contexto(anc, c.palabras))]
    distintos = {c.compacto for _, c, _ in cercanos_ctx}
    if len(distintos) == 1:
        d, c, a = cercanos_ctx[0]
        return _resultado(campo, "diferencia", c, a, distancia=d)
    if len(distintos) > 1:
        return _resultado(campo, "ambiguo", None, None, candidatos=sorted(distintos))
    if exactos:
        return _resultado(campo, "confirmado_sin_contexto", exactos[0], None, distancia=0)
    return _resultado(campo, "no_encontrado", None, None)


def _tokens_nombre(fila: Fila) -> list[tuple[str, Palabra]]:
    out = []
    for p in fila.palabras:
        t = re.sub(r"[^A-Z]", "", normalize.texto(p.texto) or "")
        if len(t) >= 2:
            out.append((t, p))
    return out


def _verificar_nombre(doc: DocLayout, campo: str, esperado: str, anc: list[Ancla]) -> Verificacion:
    objetivo = sorted(normalize.tokens(normalize.nombre(esperado), quitar_stopwords=False))
    if not objetivo:
        return _resultado(campo, "no_encontrado", None, None)
    mejor = None
    for i, fila in enumerate(doc.filas):
        grupos = [fila.palabras]
        if i + 1 < len(doc.filas) and doc.filas[i + 1].pagina == fila.pagina:
            grupos.append(fila.palabras + doc.filas[i + 1].palabras)  # nombre partido en dos filas
        for palabras in grupos:
            toks = _tokens_nombre(Fila(fila.pagina, i, palabras))
            opciones: list[tuple[bool, list[Palabra]]] = []
            for t in objetivo:
                exacto = [p for x, p in toks if x == t]
                if exacto:
                    opciones.append((True, exacto))
                elif len(t) >= 4 and (aprox := [p for x, p in toks if levenshtein(t, x) <= 1]):
                    opciones.append((False, aprox))
            if not opciones:
                continue
            # Tokens repetidos en la fila (p. ej. 'DE'): se elige la palabra más cercana a las inequívocas.
            unicas = [ps[0].x0 for _, ps in opciones if len(ps) == 1] or [ps[0].x0 for _, ps in opciones]
            ref_x = sorted(unicas)[len(unicas) // 2]
            usados = [min(ps, key=lambda p: abs(p.x0 - ref_x)) for _, ps in opciones]
            exactos = sum(1 for es, _ in opciones if es)
            cerca = len(opciones) - exactos
            usados.sort(key=lambda p: (p.fila, p.x0))
            clave = (exactos + cerca, exactos, -len(palabras))
            if mejor is None or clave > mejor[0]:
                mejor = (clave, exactos, cerca, usados)
    if mejor is None:
        return _resultado(campo, "no_encontrado", None, None)
    _, exactos, cerca, usados = mejor
    cand = _Candidato("", usados)
    a = _con_contexto(anc, usados)
    n = len(objetivo)
    if exactos == n:
        estado: Estado = "confirmado" if a else "confirmado_sin_contexto"
        return _resultado(campo, estado, cand, a, distancia=0)
    if exactos + cerca == n and a:
        return _resultado(campo, "diferencia", cand, a, distancia=cerca, detalle=f"{cerca} token(s) con una letra distinta")
    if exactos + cerca >= max(2, (n + 1) // 2) and a:
        return _resultado(campo, "parcial", cand, a, detalle=f"faltan {n - exactos - cerca} de {n} tokens")
    return _resultado(campo, "no_encontrado", None, None)


_RE_DINERO = re.compile(r"^\$?\d[\d.,]*$")
_RE_FECHA = re.compile(r"^(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}/\d{2}/\d{4})$")


def _dinero_de(p: Palabra) -> Decimal | None:
    return normalize.dinero(p.texto) if _RE_DINERO.match(p.texto.strip()) else None


def _fecha_de(p: Palabra) -> date | None:
    t = p.texto.strip()
    if not _RE_FECHA.match(t):
        return None
    return normalize.fecha(t.replace("/", "-")) if re.match(r"^\d{4}", t) else normalize.fecha(t)


def _verificar_por_parseo(doc: DocLayout, campo: str, esperado, anc: list[Ancla], parsear) -> Verificacion:
    if esperado is None:
        return _resultado(campo, "no_encontrado", None, None)
    iguales = [p for p in doc.palabras if parsear(p) == esperado]
    for p in iguales:
        if a := _con_contexto(anc, [p]):
            return _resultado(campo, "confirmado", _Candidato("", [p]), a, distancia=0)
    # Hay una etiqueta del campo con un valor distinto al lado: diferencia.
    for a in anc:
        vecinos = [p for p in doc.palabras if parsear(p) is not None and _cerca(a, [p])]
        if vecinos:
            p = min(vecinos, key=lambda w: (w.fila != a.fila, abs(w.x0 - a.x1)))
            if not iguales:
                return _resultado(campo, "diferencia", _Candidato("", [p]), a)
    if iguales:
        return _resultado(campo, "confirmado_sin_contexto", _Candidato("", [iguales[0]]), None, distancia=0)
    return _resultado(campo, "no_encontrado", None, None)


# --- extracción sin valor esperado ---------------------------------------------

_PATRONES_CODIGO = {
    "numero_factura": r"[A-Z]{1,5}\d{3,}",
    "numero_autorizacion": r"[0-9A-Z]*\d[0-9A-Z]{4,14}",
    "id_interno_paciente": r"\d{3,15}",
    "nit_emisor": r"\d{6,12}",
    "nit_eps": r"\d{6,12}",
    "cufe": r"[0-9a-fA-F]{20,}",
}


_TODAS_ETIQUETAS = sorted({tuple(e.split()) for lista in ETIQUETAS.values() for e in lista if len(e.split()) > 1}, key=len, reverse=True)


def _inicia_etiqueta(palabras: list[Palabra], i: int) -> bool:
    """¿Empieza en palabras[i] una etiqueta conocida de varias palabras (p. ej. 'FECHA INGRESO')?"""
    piezas = [pz for p in palabras[i : i + 6] for pz in _piezas_etiqueta(p)]
    return any(tuple(piezas[: len(e)]) == e for e in _TODAS_ETIQUETAS)


def _vecinos(doc: DocLayout, a: Ancla) -> list[Palabra]:
    """Valor de una etiqueta: palabras a su derecha en la misma fila hasta la siguiente
    etiqueta ('XXX:') o un hueco grande; si no hay, la fila de abajo."""
    fila = doc.filas[a.fila]
    derecha: list[Palabra] = []
    x_prev = a.x1
    palabras = fila.palabras
    contiguas = [doc.filas[j] for j in (a.fila - 1, a.fila + 1) if 0 <= j < len(doc.filas) and doc.filas[j].pagina == a.pagina]
    extra = [p for f in contiguas for p in f.palabras if p.x0 >= a.x1 - 1 and _solapa_vertical(a, [p])]
    if extra and not [p for p in palabras if p.x0 >= a.x1 - 1 and p not in a.palabras]:
        # La etiqueta quedó sola en su fila y el valor está en las filas de al lado (multilínea).
        return sorted(extra, key=lambda p: (p.y0, p.x0))
    for i, p in enumerate(palabras):
        siguiente = palabras[i + 1] if i + 1 < len(palabras) else None
        inicia_etiqueta = _inicia_etiqueta(palabras, i) or (
            derecha and siguiente is not None and siguiente.texto.endswith(":") and siguiente.x0 - p.x1 < 15
        )
        if p.x0 < a.x1 - 1 or any(p is w for w in a.palabras) or not re.search(r"[0-9A-Za-z]", p.texto):
            continue
        if p.x0 - x_prev > 60 or (derecha and (p.texto.endswith(":") or inicia_etiqueta)):
            break
        if p.texto.endswith(":") and not derecha:
            break
        derecha.append(p)
        x_prev = p.x1
    if derecha:
        return derecha
    if a.fila + 1 < len(doc.filas) and doc.filas[a.fila + 1].pagina == a.pagina:
        return [p for p in doc.filas[a.fila + 1].palabras if a.x0 - 40 <= p.x0 <= a.x1 + 150]
    return []


def extraer(doc: DocLayout, campo: str, extra: list[str] = ()) -> str | None:
    tipo = TIPOS[campo]
    todas = anclas(doc, campo, extra)
    # Primero la etiqueta más específica del diccionario; luego el orden de lectura.
    for a in sorted(todas, key=lambda a: (a.rango, a.fila)):
        pegado = [x for x in _piezas_etiqueta(a.palabras[-1]) if x != a.texto.split()[-1]]
        palabras = _vecinos(doc, a)
        if tipo == "codigo":
            patron = _PATRONES_CODIGO.get(campo, r"[0-9A-Z]*\d[0-9A-Z]{2,}")
            textos = pegado + [x for p in palabras for x in re.split(r"[-:/;,.\s]", p.texto) if x]
            if campo.startswith("nit"):
                textos = [p.texto for p in palabras]
            if campo == "cufe" and palabras:
                sig = doc.filas[a.fila + 1].palabras[:1] if a.fila + 1 < len(doc.filas) else []
                junto = "".join(p.texto for p in palabras + sig if re.fullmatch(r"[0-9a-fA-F]+", p.texto))
                if len(junto) >= 96:
                    return junto[:96].lower()
            for t in textos:
                if re.fullmatch(patron, _compacto(t) or ""):
                    return normalize.nit(t) if campo.startswith("nit") else _compacto(t)
        elif tipo == "documento":
            toks = [p.texto for p in palabras]
            for i, t in enumerate(toks):
                if re.fullmatch(r"[A-Za-z]{2,3}", t) and i + 1 < len(toks) and re.search(r"\d", toks[i + 1]):
                    return normalize.documento_id(t, toks[i + 1])
        elif tipo == "nombre":
            letras = [p.texto for p in palabras if re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", p.texto)]
            if len(letras) >= 2:
                return normalize.nombre(" ".join(letras))
        elif tipo == "dinero":
            for p in palabras:
                if (v := _dinero_de(p)) is not None:
                    return normalize.dinero_texto(v)
        elif tipo == "fecha":
            for p in palabras:
                if (v := _fecha_de(p)) is not None:
                    return v.isoformat()
    return None
