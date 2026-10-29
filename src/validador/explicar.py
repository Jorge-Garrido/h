"""Explicaciones en lenguaje claro para quien valida a mano (no para quien programa).

Para cada discrepancia se generan tres textos:
  - explicacion: qué está mal, dicho como lo diría una persona ("al RIPS le falta un 0")
  - verificar:   qué archivo abrir y qué buscar para comprobarlo
  - accion:      qué hacer si se confirma (propuesta; confirmar con contabilidad/jurídica)
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import catalogo
from .base.schemas import Hallazgo, ResultadoCampo, ResultadoFactura, ResultadoLinea

_CAT = catalogo.get()


class _Nombres(dict):
    """Nombre de una fuente para el texto ("la historia clínica"); las que no están en el
    catálogo ("doc_EPX") se nombran con su prefijo."""

    def __init__(self, atributo: str):
        super().__init__()
        self.atributo = atributo

    def __missing__(self, fuente: str) -> str:
        return getattr(_CAT.fuente(fuente), self.atributo)

    def get(self, fuente: str, defecto=None) -> str:
        return self[fuente]


DOC_CORTO = _Nombres("corto")
DOC_ART = _Nombres("articulo")


def _quienes(fuentes: list[str]) -> tuple[str, str]:
    """('El RIPS, el XML y el PDF de la factura', 'tienen')."""
    nombres = [DOC_ART.get(f, f) for f in fuentes]
    texto = nombres[0] if len(nombres) == 1 else ", ".join(nombres[:-1]) + " y " + nombres[-1]
    return texto[0].upper() + texto[1:], ("tiene" if len(nombres) == 1 else "tienen")


def miles(valor) -> str:
    try:
        return f"{float(valor):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return str(valor)


# "Dónde mirar" por (campo, fuente) y qué hacer si una fuente está mal: catálogo (donde, accion).
DONDE = {(c, n): t for n, f in _CAT.fuentes.items() for c, t in f.donde.items()}
ACCION = {n: f.accion for n, f in _CAT.fuentes.items() if f.accion}


class _Archivos(dict):
    def __missing__(self, prefijo: str) -> str:
        return "(no está en la carpeta)"


def _archivos(r: ResultadoFactura) -> dict[str, str]:
    """Primer archivo de cada documento presente, por prefijo; los ausentes, "(no está en la carpeta)"."""
    return _Archivos({k: v[0] for k, v in r.documentos_presentes.items() if v})


def _donde(r: ResultadoFactura, campo: str, fuentes: list[str]) -> str:
    arch = _archivos(r)
    pasos = []
    for f in fuentes:
        plantilla = DONDE.get((campo, f))
        if plantilla and plantilla.format_map(arch) not in pasos:
            pasos.append(plantilla.format_map(arch))
    return " | ".join(pasos)


def diferencia_digitos(correcto: str, otro: str) -> str:
    """Describe cómo difiere 'otro' de 'correcto': dígito que falta, que sobra, cambiado..."""
    if re.search(r"[A-Z]", otro) and not re.search(r"[A-Z]", correcto):
        letras = "".join(sorted(set(re.findall(r"[A-Z]", otro))))
        return f"tiene la letra '{letras}' dentro del número"
    ops = SequenceMatcher(None, correcto, otro).get_opcodes()
    cambios = [(tag, correcto[i1:i2], otro[j1:j2], i1) for tag, i1, i2, j1, j2 in ops if tag != "equal"]
    if len(cambios) == 1:
        tag, a, b, pos = cambios[0]
        if tag == "delete" and len(a) <= 2:
            return f"le falta {'un' if len(a) == 1 else 'los dígitos'} '{a}' (posición {pos + 1})"
        if tag == "insert" and len(b) <= 2:
            return f"le sobra {'un' if len(b) == 1 else 'los dígitos'} '{b}' (posición {pos + 1})"
        if tag == "replace" and len(a) == len(b) <= 2:
            return f"tiene '{b}' donde va '{a}' (posición {pos + 1})"
    prefijo = next((i for i, (x, y) in enumerate(zip(correcto, otro)) if x != y), min(len(correcto), len(otro)))
    if prefijo >= len(correcto) * 0.5:
        return (f"coincide en los primeros {prefijo} dígitos y cambia en los últimos {len(correcto) - prefijo}"
                " (puede ser otra autorización del mismo paciente)")
    return "es un número completamente distinto"


def _explicar_campo(r: ResultadoFactura, c: ResultadoCampo) -> tuple[str, str, str]:
    sospechosos = [s for s in (c.documento_sospechoso or "").split(",") if s]
    ref = c.valor_final or ""
    buenos = [f for f, v in c.valores_por_fuente.items() if f not in sospechosos and v == ref]
    partes: list[str] = []

    if c.campo in ("numero_autorizacion", "paciente_id", "numero_factura", "id_interno_paciente"):
        autoridad = "autorizacion_scan" in c.valores_por_fuente and "autorizacion_scan" not in sospechosos and c.campo == "numero_autorizacion"
        if autoridad:
            partes.append(f"La autorización emitida por la EPS dice {ref}.")
        elif ref:
            partes.append(f"El valor correcto sería {ref} (coinciden: {', '.join(DOC_CORTO[b] for b in buenos) or '—'}).")
        doc_pac = next((cc.valor_final for cc in r.campos if cc.campo == "paciente_id"), None) or ""
        num_pac = re.sub(r"\D", "", doc_pac)
        agrupados: dict[str, list[str]] = {}
        for s in sospechosos:
            agrupados.setdefault(c.valores_por_fuente.get(s) or "", []).append(s)
        for valor, fuentes in agrupados.items():
            quien, verbo = _quienes(fuentes)
            if c.campo == "numero_autorizacion" and num_pac and re.sub(r"\D", "", valor) == num_pac:
                partes.append(f"En {quien[0].lower() + quien[1:]} se digitó el documento del paciente ({valor}) en lugar del número de autorización.")
            elif c.campo == "paciente_id" and valor.split()[:1] != ref.split()[:1]:
                partes.append(f"{quien} {verbo} otro tipo y número de documento ({valor}). Puede ser un recién nacido cuyo documento cambió (CN → RC) o un paciente distinto.")
            else:
                partes.append(f"{quien} {verbo} {valor}: {diferencia_digitos(re.sub(r'[^0-9A-Z]', '', ref), re.sub(r'[^0-9A-Z]', '', valor))}.")
        if autoridad and len(sospechosos) >= 3:
            partes.append("Todas las fuentes digitales coinciden entre sí, pero no con la EPS: el error viene del sistema de facturación.")
        elif not autoridad and sospechosos == ["autorizacion_scan"]:
            partes.append("Solo difiere la lectura del documento escaneado: puede ser un error de lectura del sistema.")
    elif c.campo == "paciente_nombre":
        if sospechosos:
            for s in sospechosos:
                v = c.valores_por_fuente.get(s) or ""
                faltan = set(ref.split()) - set(v.split())
                sobran = set(v.split()) - set(ref.split())
                detalle = f"dice '{' '.join(sorted(sobran))}' donde los demás dicen '{' '.join(sorted(faltan))}'" if faltan and sobran else "el nombre está incompleto o en otro orden"
                partes.append(f"En {DOC_ART[s]} el nombre es '{v}': {detalle}. Los demás documentos dicen '{ref}'.")
        else:
            vals = "; ".join(f"{DOC_CORTO[f]}: {v}" for f, v in c.valores_por_fuente.items())
            partes.append(f"Los documentos no coinciden en el nombre y no hay mayoría ({vals}).")
    else:
        partes.append(c.motivo or "")

    verificar = _donde(r, c.campo, sospechosos + [b for b in buenos if b != "xml"][:2])
    if sospechosos == ["autorizacion_scan"]:
        accion = f"Mirar el documento escaneado: si dice {ref}, fue un error de lectura y no hay nada que corregir; si dice otro valor, verificar con la EPS."
    else:
        accion = " ".join(dict.fromkeys(ACCION[s] for s in sospechosos if s in ACCION)) or "Revisar cuál documento tiene el dato correcto."
    return " ".join(partes), verificar, accion


def _explicar_linea(r: ResultadoFactura, l: ResultadoLinea) -> tuple[str, str, str]:
    arch = _archivos(r)
    partes, verificar, accion = [], [], []
    desc = f" ({l.descripcion[:70]})" if l.descripcion else ""
    for m in l.motivos:
        if m.startswith("el CUPS facturado no está en la autorización"):
            lista = re.search(r"lista: ([^)]*)", m)
            partes.append(
                f"Se facturó el CUPS {l.codigo}{desc}, pero la autorización de la EPS no lo incluye"
                + (f"; la autorización lista {lista.group(1)}." if lista else ".")
            )
            verificar.append(f"{arch['PDE']}: tabla de servicios autorizados")
            accion.append("Confirmar si el servicio prestado es el autorizado; si no, gestionar la autorización correcta o ajustar la factura.")
        elif m.startswith("valor unitario"):
            t = l.tarifa
            cobrado = miles(t.valor_unitario_facturado) if t else "?"
            tarifa = miles(t.valor_unitario_tarifa) if t else "?"
            partes.append(
                f"Se cobró ${cobrado} por unidad por el CUPS {l.codigo}{desc}; el tarifario institucional"
                + (f" ({t.codigo_institucional}: {t.descripcion_tarifa[:60]})" if t and t.codigo_institucional else "")
                + f" dice ${tarifa}."
                + " Si esta EPS tiene otro contrato con otros valores, la diferencia puede ser correcta."
            )
            verificar.append(f"{arch['FEV']}: columna VALOR UNITARIO; tarifario (documentos/base_cups.xlsx)")
            accion.append("Confirmar la tarifa pactada en el contrato con la EPS.")
        elif m.startswith("fecha de atención"):
            ingreso = next((c.valor_final for c in r.campos if c.campo == "fecha_ingreso"), "?")
            egreso = next((c.valor_final for c in r.campos if c.campo == "fecha_egreso"), "?")
            partes.append(f"El RIPS registra una atención del CUPS {l.codigo}{desc} con fecha fuera del periodo de la factura (ingreso {ingreso}, egreso {egreso}). Suele pasar en terapias de varias sesiones.")
            verificar.append(f"{arch['RIPS']}: \"fechaInicioAtencion\" de ese servicio")
            accion.append("Confirmar las fechas de las sesiones; si la factura debe cubrirlas, ajustar el periodo o la factura.")
        elif m.startswith("el código no aparece") or m.startswith("el valor difiere") or m.startswith("la cantidad"):
            partes.append(f"El servicio {l.codigo}{desc} no coincide entre documentos: {m}.")
            verificar.append(f"{arch['RIPS']} y {arch['FEV']}: líneas del servicio {l.codigo}")
            accion.append("Revisar qué documento tiene el servicio correcto.")
    return " ".join(partes), " | ".join(dict.fromkeys(verificar)), " ".join(dict.fromkeys(accion))


def _explicar_hallazgo(r: ResultadoFactura, h: Hallazgo) -> tuple[str, str, str]:
    arch = _archivos(r)
    m = h.motivo
    if h.codigo == "HC.TIPO_CONSULTA_DIFIERE":
        g = re.search(r"consulta (\d+) facturada como (.+?) y la HC registra (.+)", m)
        if g:
            return (
                f"La factura cobra la consulta {g.group(1)} como '{g.group(2)}', pero la nota de la historia clínica dice '{g.group(3)}'. "
                "El código CUPS de la consulta cambia según sea primera vez o control.",
                f"{arch['HEV']}: \"Tipo de consulta\" | {arch['FEV']}: descripción del servicio",
                "Confirmar el tipo de consulta con el área clínica y, si hace falta, corregir el código facturado.",
            )
    if h.codigo == "HC.FECHA_FUERA_PERIODO":
        ingreso = next((c.valor_final for c in r.campos if c.campo == "fecha_ingreso"), "?")
        egreso = next((c.valor_final for c in r.campos if c.campo == "fecha_egreso"), "?")
        return (
            f"Ninguna nota de la historia clínica tiene fecha dentro del periodo facturado ({ingreso} a {egreso}). "
            "Puede ser un resultado leído días después.",
            f"{arch['HEV']}: fechas de las notas (\"Fecha:\")",
            "Confirmar que la atención facturada tenga soporte en la historia clínica.",
        )
    if h.tipo == "ocr":
        aut = next((c.valor_final for c in r.campos if c.campo == "numero_autorizacion"), None)
        return (
            "El sistema no pudo leer con seguridad el número de autorización en el documento escaneado"
            + (f". Verificar a ojo que diga {aut}." if aut else "."),
            f"{arch['PDE']}: \"No. Autorización\" (o \"CASO NRO.\")",
            "Si no coincide, gestionar con la EPS; si la imagen es ilegible, volver a escanear.",
        )
    if h.codigo == "DOC.SOLO_PDF":
        return (
            "La carpeta no trae el XML de la factura electrónica ni el RIPS. Se validó comparando el PDF de la factura "
            "con la autorización y la historia clínica, pero no se pudo verificar el CUFE ni el RIPS.",
            f"Carpeta original: {', '.join(r.documentos_presentes.get('UNI', [])) or 'archivos PDF'}",
            "Adjuntar el XML (AttachedDocument) y, si la EPS lo exige, el RIPS con su CUV antes de radicar.",
        )
    if h.codigo == "DOC.FALTA":
        return (f"Falta un documento obligatorio: {m}.", "Carpeta original de la factura", "Completar los soportes antes de radicar.")
    return (m, "", "")


def explicar(r: ResultadoFactura, obj) -> tuple[str, str, str]:
    if isinstance(obj, ResultadoCampo):
        return _explicar_campo(r, obj)
    if isinstance(obj, ResultadoLinea):
        return _explicar_linea(r, obj)
    return _explicar_hallazgo(r, obj)
