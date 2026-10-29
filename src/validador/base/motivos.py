"""Catálogo de códigos de motivo.

Cada hallazgo, campo y línea con novedad lleva un código de este catálogo. El código (y su
``ref``) es lo que se guarda y se cuenta en la base de datos: **nunca lleva datos del paciente**.
El texto ``motivo`` con los valores concretos queda en el JSON del resultado, que tiene la
misma retención que los documentos.

``ref`` es una referencia no sensible: el campo, el CUPS, el prefijo del documento o el
código DIAN/CUV. Nunca un número de documento, un nombre ni un número de autorización.
"""

from __future__ import annotations

MOTIVOS: dict[str, str] = {
    # --- campos comparados entre fuentes (ResultadoCampo.codigos) ---
    "CAMPO.COINCIDE": "coincide en todas las fuentes",
    "CAMPO.UNA_FUENTE": "solo una fuente trae el campo",
    "CAMPO.NO_ENCONTRADO": "no se encontró en ninguna fuente",
    "CAMPO.DIFIERE_MAYORIA": "una o más fuentes difieren de la mayoría",
    "CAMPO.SIN_MAYORIA": "las fuentes discrepan sin mayoría; requiere revisión humana",
    "CAMPO.FORMATO_INVALIDO": "el valor no cumple el formato esperado",
    "CAMPO.COINCIDE_AUTORIDAD": "coincide con el documento de la EPS",
    "CAMPO.DIFIERE_AUTORIDAD": "las fuentes digitales difieren del documento de la EPS",
    "CAMPO.OCR_DIFIERE_CONFIRMADO": "el escaneo dice otro valor (dos lecturas) y las fuentes digitales coinciden",
    "CAMPO.OCR_DIFIERE_INCIERTO": "el escaneo difiere pero las fuentes digitales coinciden (probable error de OCR)",
    # --- líneas de servicio (ResultadoLinea.codigos) ---
    "LINEA.CODIGO_FALTA": "el servicio no aparece en todas las fuentes",
    "LINEA.VALOR_DIFIERE": "el valor del servicio difiere entre fuentes",
    "LINEA.CANTIDAD_DIFIERE": "la cantidad del servicio difiere entre fuentes",
    "LINEA.FECHA_FUERA_PERIODO": "fecha de atención fuera del periodo de la factura",
    "LINEA.CUPS_NO_EN_AUTORIZACION": "el CUPS facturado no está en la autorización escaneada",
    "TARIFA.VALOR_DIFIERE": "el valor unitario difiere de la tarifa contratada",
    "TARIFA.NO_DETERMINABLE": "la descripción no corresponde a ningún ítem del tarifario",
    "TARIFA.NO_EN_TARIFARIO": "el CUPS no está en el tarifario",
    # --- documentos de la carpeta (Hallazgo) ---
    "DOC.SOLO_PDF": "sin XML ni RIPS: se validó con el PDF de la factura",
    "DOC.SIN_RIPS_NI_XML": "no hay RIPS ni XML de la factura",
    "DOC.SIN_RIPS": "sin RIPS: se usó el XML como respaldo",
    "DOC.SIN_XML": "falta el XML de la factura electrónica",
    "DOC.FALTA": "falta un documento exigido por la matriz de documentos",
    "DOC.DESCONOCIDO": "un PDF no está en el catálogo y su contenido no se reconoció: solo se verificó la identidad",
    "DOC.PREFIJO_INESPERADO": "el prefijo del archivo no está en el catálogo; el documento se reconoció por su contenido",
    "DOC.CONTENIDO_NO_COINCIDE": "el contenido del PDF corresponde a otro documento que el que indica su prefijo",
    "DOC.OTRO_PACIENTE": "un documento que solo verifica identidad parece ser de otro paciente",
    "EXTRACCION.ERROR": "no se pudo leer un documento",
    # --- OCR ---
    "OCR.HC_ESCANEADA": "historia clínica escaneada: identidad pendiente de OCR",
    "OCR.AUTORIZACION_PENDIENTE": "autorización: número y CUPS pendientes de OCR",
    "OCR.NO_CONCLUYENTE": "lectura del escaneo no concluyente (un solo motor); revisión humana",
    "OCR.NO_LEGIBLE": "un campo del escaneo no es legible; revisión humana",
    # --- DIAN y validación del Ministerio ---
    "DIAN.NO_VALIDADO": "la DIAN no validó el documento",
    "DIAN.CODIGO": "código de respuesta de la DIAN",
    "CUV.RECHAZADO": "la validación del Ministerio fue rechazada",
    "CUV.CODIGO": "código de la validación del Ministerio",
    # --- historia clínica ---
    "HC.FECHA_FUERA_PERIODO": "ninguna nota de la HC está dentro del periodo de la factura",
    "HC.TIPO_CONSULTA_DIFIERE": "el tipo de consulta facturado no coincide con la HC",
    "HC.ESPECIALIDAD_NO_VERIFICABLE": "la especialidad de la consulta no se pudo verificar contra la HC",
    # --- plantillas de regla (Config.plantillas; ref = nombre de la plantilla) ---
    "PLANTILLA.CAMPO_FALTA": "un campo exigido por una plantilla no se encontró en el documento",
    "PLANTILLA.VALOR_NO_PERMITIDO": "el valor del campo no está entre los permitidos por una plantilla",
    # --- corrida ---
    "DUPLICADO.CUFE": "el CUFE ya está registrado con otra factura",
    "DUPLICADO.YA_PROCESADA": "la factura ya se procesó en otra corrida",
    "PROCESAMIENTO.ERROR": "la factura no se pudo procesar; revisar manualmente",
    "REGLA.ERROR": "una regla de validación falló; el resto de la factura sí se validó",
}


def validar(codigo: str) -> str:
    if codigo not in MOTIVOS:
        raise ValueError(f"código de motivo desconocido: {codigo}")
    return codigo
