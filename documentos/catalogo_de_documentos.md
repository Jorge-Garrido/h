# Catálogo de documentos, formatos por hospital y plantillas de regla

Este documento explica cómo el validador maneja documentos que cambian entre hospitales y sistemas
(por ejemplo la epicrisis), documentos con prefijos raros o desconocidos, y validaciones que se
configuran sin escribir código.

## 1. El problema

Antes, "qué documentos existen" estaba repetido en el código: prefijos en `discover.py`, campos por
documento en `generico.py`, palabras de clasificación en `unificado.py`, etiquetas en
`localizar.py`, nombres en `salida_errores.py` y `explicar.py`, acciones en `corrections/adapter.py`
y un `if` por documento en `pipeline.extraer()`. Un documento nuevo exigía tocar ocho archivos. Si
se olvidaba uno, el valor se comparaba pero no aparecía en el Excel, o la explicación fallaba.

Además, un PDF con un prefijo que nadie conocía (`EPC_…pdf`) se ignoraba en silencio.

## 2. Tres niveles: qué vive dónde

| Nivel | Dónde | Quién lo cambia | Ejemplo |
|---|---|---|---|
| **Qué es** un documento o un campo | `src/validador/catalogo/catalogo.toml` | PR al repositorio (diff, revisión, pruebas en CI) | "La epicrisis aporta documento, nombre y fechas del paciente; su primera página dice EPICRISIS" |
| **Cómo lo produce** el sistema de un hospital | Configuración por capas: `documentos`, `plantillas`, `reglas` | Borrador → simular → publicar (BD), con autor, vigencia y huella | "En el hospital X la epicrisis se llama EPC y su sistema escribe «Ident. usuario»" |
| **La lógica** | Código Python registrado (`@regla`, `@plantilla`, lectores) | PR con pruebas | "Comparar el tipo de consulta facturado contra la HC" |

El catálogo es igual para todos los hospitales. Lo que cambia por hospital nunca se escribe en
el catálogo, y no hay un archivo por hospital: va en sus capas.

## 3. El caso de la epicrisis: distintos sistemas y distintos hospitales

Hay cuatro situaciones, de la más fácil a la más difícil:

**a) La epicrisis usa etiquetas comunes** ("Identificación", "Nombre del paciente"). Basta con la
entrada del catálogo:

```toml
[documentos.EPI]
nombre = "epicrisis"
lector = "generico"            # sin código propio: la lee el localizador genérico
fuente = "epicrisis"
campos = ["paciente_id", "paciente_nombre", "fecha_ingreso", "fecha_egreso"]
max_paginas = 1
clasificar = ["EPICRISIS", "RESUMEN DE EGRESO"]
```

La epicrisis vota en la comparación de campos, aparece como columna en el Excel y en
`_REVISION.txt`, y tiene su explicación, sin ningún cambio en el pipeline.

**b) Un hospital la nombra distinto** (`EPC_…pdf`). Va en la capa del hospital:

```toml
[documentos.EPI]
alias = ["EPC"]
```

Mientras no se declare, el validador la reconoce igual por su contenido y deja un
`DOC.PREFIJO_INESPERADO` (info) para que alguien agregue el alias.

**c) El sistema del hospital usa etiquetas propias** ("Ident. usuario:"). Sin ayuda, el localizador
no encuentra el campo y un error del documento pasa sin verse. Se declara el **formato** de ese
sistema:

```toml
[[documentos.EPI.formatos]]
nombre = "his-hospital-x"
detectar = ["SISTEMA B", "RESUMEN DE EGRESO"]      # todos deben estar en la primera página
etiquetas = { paciente_id = ["IDENT USUARIO"], fecha_egreso = ["FECHA SALIDA"] }
```

El formato se reconoce por el contenido del documento, no por el hospital. Si dos hospitales usan
el mismo sistema, el formato puede subir al catálogo (`[[documentos.EPI.formatos]]` en
`catalogo.toml`) y sirve para todos. El formato detectado queda en `extra["formato"]` del documento.

**d) El documento tiene una estructura que el localizador genérico no puede leer** (tablas, notas
con fechas, como la HC). Se escribe un lector con código y prueba (`LECTORES` en `pipeline.py`) y
el catálogo apunta a él con `lector = "..."`. Es la excepción, no la regla.

Para probar con un PDF real cómo lo reconoce el sistema (solo imprime nombres, no el contenido):

```
validador catalogo ver --clasificar EPC_800000001_FE123.pdf
EPI (epicrisis); formato: his-hospital-x
```

## 4. Prefijos raros y documentos desconocidos

Cada PDF se identifica primero por su prefijo y luego por su contenido (la primera página contra los
textos `clasificar` del catálogo; gana el documento con más textos encontrados):

| Situación | Qué hace | Código |
|---|---|---|
| Prefijo del catálogo o alias | Se lee con su lector | — |
| Prefijo desconocido, contenido reconocido (`EPC_` que dice EPICRISIS) | Se trata como ese documento y vota | `DOC.PREFIJO_INESPERADO` (info) |
| PDF sin prefijo junto a la factura (`epicrisis paciente.pdf`) | Igual: se reconoce por su primera página | `DOC.PREFIJO_INESPERADO` (ref `UNI>EPI`) |
| PDF sin prefijo y sin factura PDF | Se separa por páginas (PDF unificado), como antes | — |
| Prefijo del catálogo con contenido de otro documento (`HEV_` que es una factura) | Se lee como dice el prefijo y se avisa | `DOC.CONTENIDO_NO_COINCIDE` (aviso) |
| Ni el prefijo ni el contenido se reconocen | **Verifica identidad, no vota** | `DOC.DESCONOCIDO` (info) |
| …y el documento trae otro paciente | | `DOC.OTRO_PACIENTE` (aviso) |

**¿Por qué un documento desconocido no vota?** El localizador encuentra un campo por tres pistas: la
etiqueta (`ETIQUETAS`), la posición (a la derecha o debajo) y el tipo de valor ("CC 1000001" parece un
documento; "15/03/2026", no). En un formato que nunca se ha visto, las etiquetas genéricas como
"ID" o "NOMBRE" pueden capturar el valor equivocado. Por eso un documento desconocido solo
**verifica**: busca el número de documento que ya se conoce por el RIPS y el XML. Buscar un valor
conocido es mucho más confiable que adivinar uno desconocido. Cuando el documento entra al
catálogo, con etiquetas probadas, ya vota.

Para revisar que un prefijo del catálogo no traiga otro documento solo se usa el texto digital
(sin OCR), y se avisa únicamente si en la página no aparece ninguno de sus propios textos.

## 5. Que las configuraciones no choquen

| Riesgo | Protección |
|---|---|
| Un alias en dos documentos, en el catálogo o entre el catálogo y un hospital | Error al cargar el catálogo o al crear la capa |
| Un texto de clasificación en dos documentos | Error al cargar el catálogo |
| Una fuente alimentada por dos documentos | Error al cargar el catálogo |
| Un campo, documento o fuente que no existe (en el catálogo, en la matriz de documentos, en `prioridad_fuentes`, en etiquetas o en plantillas) | Error al validar (p. ej. `HCE` en vez de `HEV`) |
| Dos capas que cambian lo mismo | Orden fijo: global < EPS < hospital < hospital+EPS; en un mismo ámbito rige la de vigencia más reciente |
| Dos reglas que emiten el mismo código | Prueba `test_cada_codigo_lo_declara_una_sola_regla` |
| Dos plantillas con el mismo nombre | Error al validar |
| Efecto inesperado de un cambio | `validador config simular` sobre un lote antes de publicar |

## 6. Plantillas de regla

Son validaciones que se repiten con distintos parámetros. La lógica está en
`reglas/plantillas.py`, en código y probada; cada uso es un dato en la configuración, validado
contra los parámetros de la plantilla (`base/config.py`).

```toml
[[plantillas]]
usar = "campo_requerido"          # el campo debe poder leerse en el documento
nombre = "egreso-en-epicrisis"    # va como ref del hallazgo: sin datos del paciente
campo = "fecha_egreso"
documentos = ["EPI"]
severidad = "aviso"

[[plantillas]]
usar = "valor_permitido"          # el valor final debe estar en la lista o cumplir el patrón
nombre = "eps-con-contrato"
campo = "nit_eps"
valores = ["900000002"]
severidad = "grave"
```

Códigos: `PLANTILLA.CAMPO_FALTA` y `PLANTILLA.VALOR_NO_PERMITIDO`, con `ref` = nombre de la plantilla.
Una plantilla nueva es una función con `@plantilla("...")` más su modelo de parámetros en
`config.py`; la prueba `test_cada_plantilla_tiene_implementacion` verifica que estén las dos partes.

## 7. Dónde encaja un LLM (todavía no implementado)

Un LLM puede proponer **datos**, nunca publicarlos:

1. Una entrada del catálogo o un formato a partir de un PDF de ejemplo **anonimizado**: textos para
   `detectar`, etiquetas por campo, campos que trae.
2. Plantillas a partir de una descripción ("esta EPS exige la fecha de egreso en la epicrisis").
3. Una regla nueva con su prueba, como PR.

Todo pasa por la misma validación estricta (`extra="forbid"`, campos y documentos del catálogo),
por la simulación y por la aprobación de una persona. El LLM no evalúa facturas: la validación
sigue siendo determinista y explicable ante la EPS.

## 8. Archivos

| Archivo | Rol |
|---|---|
| `catalogo/catalogo.toml` | Campos (nombre, tipo, etiquetas), fuentes (nombres, dónde mirar, acciones) y documentos (lector, campos, alias, textos de clasificación, formatos) |
| `catalogo/__init__.py` | Modelo, validación de consistencia, `clasificar(texto)`, `documento(prefijo)` |
| `base/config.py` | `documentos` (alias, etiquetas, formatos por hospital) y `plantillas` |
| `pipeline.py` | `extraer()` recorre los PDF de la carpeta según el catálogo; reconocimiento por contenido; solo votan los documentos con `vota` |
| `ingest/generico.py` | Campos y páginas del catálogo; detección del formato; etiquetas extra |
| `localizar.py` | `TIPOS` y `ETIQUETAS` desde el catálogo; parámetro `extra` |
| `reglas/documentos.py` | `DOC.RECONOCIMIENTO`: prefijos inesperados, contenido distinto, desconocidos, otro paciente |
| `reglas/plantillas.py` | Plantillas `campo_requerido` y `valor_permitido` |
| `salida_errores.py`, `explicar.py`, `corrections/adapter.py`, `severity.py` | Nombres, columnas, dónde mirar, acciones y fuentes OCR desde el catálogo |

**Pendiente y a propósito:** los lectores de RIPS, XML y CUV siguen en código porque son formatos
normativos, iguales para todos. Las líneas de servicio se siguen comparando solo entre RIPS, XML y
factura PDF (`FUENTES_LINEAS`).

**Por verificar con datos reales:**
- Los nombres de PDX, CRC y RAN en el catálogo.
- Que la clasificación por puntaje (antes se tomaba el primer documento que coincidía) siga
  separando bien los PDF unificados reales.
