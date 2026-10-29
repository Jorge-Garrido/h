"""Matriz de severidad (sección 6 del plan). Las reglas vienen de config.py."""

from __future__ import annotations

import re

from . import catalogo
from .attribution import votar
from .compare import distancia_digitos, iguales
from .base.config import Config, Severidad
from .base.schemas import CampoExtraido, ResultadoCampo

ORDEN: dict[str, int] = {"ok": 0, "info": 1, "aviso": 2, "grave": 3}
FUENTES_OCR = catalogo.get().fuentes_ocr()  # fuentes con ocr = true en el catálogo


def peor(*resultados: Severidad) -> Severidad:
    return max(resultados, key=lambda r: ORDEN[r], default="ok")


def _por_prioridad(valores: dict[str, str], cfg: Config) -> str | None:
    for fuente in cfg.prioridad_fuentes:
        if fuente in valores:
            return valores[fuente]
    return next(iter(valores.values()), None)


def evaluar_campo(campo: str, extraidos: list[CampoExtraido], cfg: Config) -> ResultadoCampo:
    regla = cfg.campos[campo]
    valores = {e.fuente: e.valor for e in extraidos if e.valor}
    base = {"campo": campo, "valores_por_fuente": dict(valores)}

    if not valores:
        return ResultadoCampo(
            valor_final=None, resultado=regla.si_falta_en_todas, motivo="no encontrado en ninguna fuente",
            codigos=["CAMPO.NO_ENCONTRADO"], **base
        )

    patron = cfg.formatos.get(campo)
    # Una lectura de OCR con formato imposible es ruido de lectura, no un dato del documento.
    for f in [f for f in valores if f in FUENTES_OCR and patron and not re.fullmatch(patron, valores[f])]:
        del valores[f]
    confianza_ocr = {e.fuente: e.confianza for e in extraidos if e.fuente in FUENTES_OCR}
    invalidos = [f for f, v in valores.items() if patron and not re.fullmatch(patron, v)]
    validos = {f: v for f, v in valores.items() if f not in invalidos}

    # Fuente con autoridad (p. ej. el escaneo de la EPS para la autorización) leída con confianza.
    autoridad = cfg.fuente_autoridad.get(campo)
    ext_autoridad = next((e for e in extraidos if e.fuente == autoridad and e.valor), None)
    if ext_autoridad is not None and ext_autoridad.confianza >= cfg.min_confianza_autoridad and len(valores) > 1:
        ref = ext_autoridad.valor
        distintos = [f for f, v in valores.items() if f != autoridad and not iguales(regla.comparador, v, ref)]
        base_etapa = {"etapa_resolucion": ext_autoridad.etapa, "confianza": ext_autoridad.confianza}
        if not distintos:
            return ResultadoCampo(valor_final=ref, resultado="ok", motivo="coincide con el documento de la EPS",
                                  codigos=["CAMPO.COINCIDE_AUTORIDAD"], **base, **base_etapa)
        distancia = min(
            (distancia_digitos(ref, valores[f]) for f in distintos if "," not in valores[f] and "," not in ref), default=None
        )
        return ResultadoCampo(
            valor_final=ref,
            resultado=regla.si_difiere,
            motivo=f"el documento de la EPS dice {ref}; difiere: {', '.join(distintos)}",
            codigos=["CAMPO.DIFIERE_AUTORIDAD"],
            documento_sospechoso=",".join(distintos),
            distancia=distancia,
            **base,
            **base_etapa,
        )

    voto = votar(valores, regla.comparador)
    if voto.valor_mayoria is not None and not voto.sospechosos:
        if invalidos:
            return ResultadoCampo(
                valor_final=voto.valor_mayoria,
                resultado="grave",
                motivo=f"formato inválido en todas las fuentes ({', '.join(invalidos)})",
                codigos=["CAMPO.FORMATO_INVALIDO"],
                **base,
            )
        codigo = "CAMPO.COINCIDE" if len(valores) > 1 else "CAMPO.UNA_FUENTE"
        motivo = "coincide en todas las fuentes" if len(valores) > 1 else "una sola fuente"
        return ResultadoCampo(valor_final=voto.valor_mayoria, resultado="ok", motivo=motivo, codigos=[codigo], **base)

    if voto.empate:
        final = _por_prioridad(validos or valores, cfg)
        return ResultadoCampo(
            valor_final=final,
            resultado="grave" if regla.si_difiere == "grave" else regla.si_difiere,
            motivo="las fuentes discrepan sin mayoría; requiere revisión humana",
            codigos=["CAMPO.SIN_MAYORIA"],
            **base,
        )

    sospechosos = voto.sospechosos
    distancia = None
    if regla.comparador in ("exacta", "conjunto") and len(sospechosos) == 1:
        v = valores[sospechosos[0]]
        if "," not in v and "," not in voto.valor_mayoria:
            distancia = distancia_digitos(voto.valor_mayoria, v)

    if set(sospechosos) <= FUENTES_OCR:
        # Las fuentes digitales coinciden y solo el escaneo difiere. Si la lectura del escaneo
        # es de un solo motor, es probablemente un error de OCR (info); si dos motores la
        # confirman, el escaneo realmente dice otra cosa (aviso).
        confiable = all(confianza_ocr.get(f, 0) >= cfg.min_confianza_autoridad for f in sospechosos)
        resultado: Severidad = "aviso" if confiable else cfg.si_ocr_incierto_difiere
        codigos = ["CAMPO.OCR_DIFIERE_CONFIRMADO" if confiable else "CAMPO.OCR_DIFIERE_INCIERTO"]
        motivo = (
            "el escaneo dice otro valor (confirmado por dos lecturas) y las fuentes digitales coinciden entre sí"
            if confiable else "el escaneo difiere pero las fuentes digitales coinciden (probable error de OCR)"
        )
    else:
        resultado = regla.si_difiere
        codigos = ["CAMPO.DIFIERE_MAYORIA"]
        motivo = f"{', '.join(sospechosos)} difiere de la mayoría"
        if distancia is not None:
            motivo += f" (distancia {distancia})"
    if any(f in invalidos for f in sospechosos):
        resultado = "grave"
        codigos.append("CAMPO.FORMATO_INVALIDO")
        motivo += "; formato inválido"
    if mayoria_invalida := [f for f in invalidos if f not in sospechosos]:
        resultado = "grave"
        if "CAMPO.FORMATO_INVALIDO" not in codigos:
            codigos.append("CAMPO.FORMATO_INVALIDO")
        motivo += f"; el valor de la mayoría tiene formato inválido ({', '.join(mayoria_invalida)})"
    return ResultadoCampo(
        valor_final=voto.valor_mayoria,
        resultado=resultado,
        motivo=motivo,
        codigos=codigos,
        documento_sospechoso=",".join(sospechosos),
        distancia=distancia,
        **base,
    )
