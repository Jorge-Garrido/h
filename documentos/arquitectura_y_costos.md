# Plataforma: cola de trabajos, cómputo, base de datos y costos

Documento de decisión para la plataforma multitenant. Horizonte: **hasta 20 hospitales y unos 100 usuarios**.
Arquitectura: monolito modular (una API) más un worker aparte, en AWS. Ver la sección 6 para los costos.

---

## 1. SQS no es el cómputo: es la lista de trabajos pendientes

Hay dos piezas distintas, y es fácil confundirlas:

| Pieza | Qué es | Analogía |
|---|---|---|
| **SQS** (cola) | Una lista durable de trabajos: "validar la factura FE123 del lote 45 del hospital 7" | La bandeja de pendientes |
| **Workers** (cómputo) | Contenedores en **ECS Fargate** que toman un trabajo de la cola, lo ejecutan (incluido el OCR) y lo borran de la cola | Las personas que toman papeles de la bandeja |

SQS no ejecuta nada ni "tiene" un worker asignado. Son los workers los que **preguntan** a la cola si hay trabajo (*long polling*). Lo que los une es el **autoescalado**:

```
API "cerrar lote" ──▶ SQS: 7.000 mensajes
                         │
CloudWatch mide "mensajes visibles en la cola" ──▶ ECS Service Auto Scaling
                         │                              │
                         │                   0 tareas → 10 tareas (más mensajes, más tareas)
                         ▼                              │
            worker: recibe mensaje → baja la carpeta de S3 → procesar_carpeta (con OCR)
                    → guarda en Postgres y S3 → borra el mensaje
                         │
            cola vacía unos minutos ──▶ vuelve a 0 tareas (no se paga cómputo)
```

**Por qué SQS:**
- **Nada se pierde.** Si un worker se cae a mitad de una factura, el mensaje reaparece en la cola tras el *visibility timeout* y otro worker la toma.
- **Reintentos y cuarentena.** Una factura que falla 3 veces pasa a la cola de mensajes fallidos (*dead-letter queue*) para revisarla, sin frenar el lote.
- **Desacopla.** La API responde "lote recibido" en milisegundos aunque validar tome 20 minutos, y la pantalla de los analistas no se pone lenta por el OCR.
- **Costo prácticamente cero.** El primer millón de solicitudes al mes es gratis, y 20 hospitales generan menos de un millón.
- **Sin servidores.** No hay que instalar ni mantener nada (a diferencia de Redis o RabbitMQ).

## 2. Otras opciones para cola y cómputo

| Opción | Cómo funciona | A favor | En contra | Veredicto |
|---|---|---|---|---|
| **SQS + ECS Fargate** | Cola + contenedores que escalan con la cola | La misma imagen que la API; sin límite de tiempo; Tesseract y modelos ONNX van en la imagen; igual en local y en la nube | Hay que configurar el autoescalado (una vez, en CDK) | **Recomendado** |
| **SQS + Lambda** | Lambda se dispara sola con cada mensaje | Escala a cero de forma natural, sin configurar autoescalado. Costo similar (~$10 al mes con 20 hospitales) | Arranque en frío cargando los modelos de OCR (varios segundos); la CPU depende de la memoria asignada; empaquetar Tesseract y onnxruntime en una imagen para Lambda; límite de 15 min por invocación | **Alternativa válida.** Conviene si se quiere cero configuración de escalado |
| **AWS Batch** | Servicio de trabajos por lotes que maneja cola y cómputo (EC2 Spot o Fargate) | Muy barato con Spot para grandes volúmenes; trabajos en arreglo (*array jobs*) | Tarda minutos en arrancar; está pensado para trabajos largos, no para miles de facturas pequeñas; más piezas | Solo si el volumen fuera 10 veces mayor |
| **Step Functions (Distributed Map)** | Orquesta: "para cada factura del lote, ejecutar X" | Se ve el avance de cada lote en una consola | Una pieza más; cobra por transición | No hace falta: el avance del lote se ve en Postgres |
| **Postgres como cola** (`SELECT … FOR UPDATE SKIP LOCKED`) | La tabla `factura` con estado "pendiente" hace de cola | Ningún servicio extra; transaccional con los resultados | Hay que construir los reintentos, la cuarentena y la métrica de escalado | Viable a esta escala, pero SQS da todo eso hecho por casi $0 |
| **Celery/RQ + Redis** | Cola clásica de Python | Muy conocida | Redis administrado cuesta ~$12+ al mes y es una pieza más que operar | No |

## 3. ¿El cómputo es elástico?

**Solo el de validación.** Conviene separar lo fijo de lo que crece con el uso:

| Componente | Tipo | Comportamiento |
|---|---|---|
| Workers (validación + OCR) | **Elástico** | 0 tareas sin lotes; N tareas durante un lote. Se paga por segundo (mínimo 1 minuto por tarea) |
| S3, SQS, CloudWatch | **Por uso** | Crecen con el número de facturas y de documentos guardados |
| API (FastAPI) | **Fijo** | Siempre encendida para que los analistas entren a cualquier hora |
| Postgres (RDS) | **Fijo** | Siempre encendido; se cambia de tamaño a mano al crecer |
| Balanceador (ALB) | **Fijo** | Precio por hora más un componente pequeño por uso |

Con estos volúmenes, **lo elástico (validar y hacer OCR) es lo más barato**: unos pocos dólares al mes. El costo lo pone la parte fija: base de datos, balanceador y API.

## 4. Bases de datos: ¿una o dos?

**Una sola instancia de Postgres, una sola base de datos y dos esquemas.** No conviene una base para el validador y otra para la plataforma.

```
RDS Postgres
└── base "plataforma"
    ├── esquema plataforma     tenant, usuario, membresia, permiso, lote, factura, hallazgo,
    │                          revision, asignacion, auditoria
    └── esquema configuracion  config_capa, tarifario_version, tarifario_item, catalogo
```

**El motor (`src/validador`) no debería tener base de datos propia en producción.** Es una biblioteca: recibe una carpeta, la configuración y el tarifario, y devuelve un `ResultadoFactura`. El que guarda es el worker de la plataforma:

| Hoy (CLI local) | En la plataforma |
|---|---|
| `corridas` | `lote` (tiene hospital, quién lo subió, estado, avance) |
| `resultados` (JSON completo) | `factura` + `hallazgo` (solo estados y **códigos de motivo**). El JSON con valores del paciente va a S3 con la misma retención que los documentos |
| `previos_por_cufe` | Misma consulta, pero filtrada por hospital |
| `config_capa`, `tarifario_*` | Esquema `configuracion`, con `tenant_id` |

`store/db.py` se queda para el modo local (CLI con SQLite), que sigue siendo útil para desarrollo y pruebas.

**Por qué no dos bases de datos:**
- **Consultas cruzadas.** El dashboard une `factura` con `usuario` (a quién está asignada) y con `config_capa` (con qué reglas se validó). Entre dos bases eso son dos consultas unidas en código.
- **Transacciones.** "Guardar resultado + marcar la factura como validada + actualizar el avance del lote" debe ser todo o nada. Entre dos bases no se puede garantizar fácilmente.
- **Costo y operación.** Habría dos instancias, dos copias de seguridad y dos juegos de migraciones y de políticas RLS.

**La separación se logra con esquemas y permisos:**
- **El rol de BD del worker** puede escribir en `factura` y `hallazgo` y leer `configuracion`, pero no puede tocar `usuario` ni `membresia`.
- **El rol de la API** tiene RLS por tenant en todas las tablas.

## 5. Supuestos del cálculo de costos

| Supuesto | Valor | Cómo afecta |
|---|---|---|
| Facturas por hospital | 12.500 al mes | Lineal en S3 y cómputo |
| Tiempo por factura en 1 vCPU (con OCR) | 1,5 s (pesimista: 5 s) | **Se debe medir en el piloto** con `tiempos_ms` |
| Tamaño de una carpeta de factura | ~2 MB, ~10 archivos | S3 (almacenamiento y PUT) |
| Retención de documentos crudos | ~30 días en promedio | Borrado por regla de ciclo de vida al cerrar la revisión |
| Región | us-east-1 | sa-east-1 (São Paulo) es ~30–50 % más cara |
| Precios | Lista pública de AWS que conozco (puede haber cambiado) | **Verificar en calculator.aws antes de cotizar** |
| Red | Subredes públicas con *security groups*, **sin NAT Gateway**, *VPC endpoint* de S3 (gratis) | Ahorra ~$32+ al mes. Cada IP pública cuesta ~$3,65 al mes |
| Cómputo | Fargate ARM (Graviton), ~20 % más barato que x86 | La imagen se construye para `linux/arm64` |

Precios de referencia (us-east-1, aproximados): Fargate ARM $0,0324 por vCPU-hora y $0,0036 por GB-hora; RDS t4g.micro/small/medium $0,016/$0,032/$0,065 por hora (Multi-AZ, el doble); almacenamiento gp3 de RDS ~$0,115 por GB al mes; ALB ~$0,0225 por hora más LCU; IPv4 pública $0,005 por hora; S3 $0,023 por GB al mes y $0,005 por cada 1.000 PUT.

## 6. Costo mensual estimado de infraestructura (USD)

| Componente | **1 hospital** (piloto) | **4 hospitales** | **20 hospitales** |
|---|---|---|---|
| Facturas al mes | 12.500 | 50.000 | 250.000 |
| **Postgres (RDS)** | t4g.micro, 1 zona, 20 GB: **$14** | t4g.small **Multi-AZ**, 50 GB: **$58** | t4g.medium Multi-AZ, 150 GB: **$130** |
| **Balanceador (ALB)** | $18 | $20 | $25 |
| **API (Fargate ARM)** | 1 tarea de 0,25 vCPU / 0,5 GB: $7 | 2 tareas de 0,5 vCPU / 1 GB: $29 | 2 tareas de 0,5–1 vCPU: $30–60 |
| **Workers (elástico, incluye OCR)** | $1–2 | $3–6 | $10–20 |
| IPs públicas | $11 | $15 | $18 |
| S3 (documentos, JSON, recortes) | $2 | $5 | $25 |
| CloudWatch (logs, métricas, alarmas) | $3 | $6 | $15 |
| Secrets, KMS, ECR, Route 53, SQS | $3 | $4 | $8 |
| Cognito, CloudFront, SES | ~$0 | ~$0 | ~$0 |
| **Producción** | **~$60** | **~$140** | **~$280** |
| Staging (apagado fuera de horario) | ~$30 | ~$35 | ~$40 |
| **Total** | **~$90** | **~$175** | **~$320** |
| **Por hospital** | ~$90 | ~$44 | ~$16 |

**Lectura:**
- **Con un hospital casi todo es costo fijo.** Por eso el costo por hospital baja tanto al crecer.
- **El OCR y la validación cuestan menos de $1 por hospital al mes.** Aunque el tiempo real fuera 5 s por factura en vez de 1,5 s, el total casi no cambia.
- **Para 4 hospitales recomiendo Multi-AZ en la base de datos** (+$29 al mes). Si una zona de AWS falla, Postgres pasa a la otra en 1–2 minutos. Con clientes que pagan, vale la pena. En el piloto no.
- **Ahorros posibles en el piloto:** compartir el ALB entre staging y producción en una sola cuenta (enrutando por dominio), o usar App Runner para la API. Ahorran unos $20–30 al mes a cambio de menos aislamiento.

### Costos opcionales y lo que no está incluido

| Concepto | Estimado |
|---|---|
| Textract como respaldo del OCR (DetectDocumentText, ~$1,50 por 1.000 páginas), si se usa en ~10 % de las autorizaciones | 1 hospital ~$2; 20 hospitales ~$40 |
| Bedrock (Claude Haiku) para leer recortes difíciles | Centavos a pocos dólares al mes |
| Plan de soporte de AWS (Developer: ~$29 al mes, o Business) | Opcional, recomendable al vender |
| IVA sobre la factura de AWS en Colombia | Verificar con el contador |
| Dominio | ~$12 al año |
| Desarrollo, soporte, auditoría de seguridad y abogado (contratos de transmisión de datos) | No es infraestructura; suele ser el costo dominante |

## 7. Cómo cambia la infraestructura en cada etapa

| Etapa | Cambio | Esfuerzo |
|---|---|---|
| 1 → 4 hospitales | RDS de micro a small Multi-AZ; API de 1 a 2 tareas | Cambiar dos parámetros en CDK y desplegar (RDS se reinicia en una ventana de mantenimiento) |
| 4 → 20 hospitales | RDS a medium; más workers máximos; cola o prioridad por hospital para repartir el cómputo de forma justa | Parámetros más una mejora en el worker |
| Más de 20 | Réplica de lectura para los dashboards; evaluar AWS Batch con Spot para los workers | Solo si las métricas lo piden |

El código no cambia entre etapas: es la misma imagen, la misma cola y el mismo esquema.

---

## 8. Decisión (2026-10-22): servidor pequeño + Lambda

Tras comparar las opciones (servidor con todo, servidor + RDS, Lightsail, servidor + Lambda, Fargate),
se elige **servidor pequeño para la plataforma + Lambda para el validador**. Reemplaza la propuesta
de Fargate + ALB de las secciones anteriores, que queda como referencia para más de 20 hospitales.

```
Navegador ─▶ EC2 t4g.small · docker compose: nginx + certbot, API FastAPI, despachador (cron)
                 │ cada minuto: hasta N facturas pendientes por hospital (en ronda) ─▶ SQS
                 ▼
            SQS ─▶ Lambda "validador" (en la VPC, concurrencia máxima limitada) ─▶ RDS + S3
```

| Pieza | Elección | Motivo |
|---|---|---|
| Plataforma (API, login, dashboard, visor, configuración, exportación) | EC2 pequeño con Docker Compose | El equipo ya opera servidores Linux con Compose (como ERP_PAS) |
| Validación + OCR | Lambda con imagen de contenedor (Debian + `awslambdaric`, Tesseract y modelos ONNX) | Sin costo cuando no hay lotes (capa gratuita), escala sola, no compite por memoria con la API |
| Cola | SQS, alimentada por un **despachador** que lee las facturas pendientes en Postgres | Reparto justo entre hospitales; el lote grande no bloquea a los pequeños |
| Base de datos | RDS Postgres (micro o small; Multi-AZ desde que haya contratos) | Copias y restauración administradas |
| Documentos | S3 cifrado, con ciclo de vida | URL prefirmadas para cargar y ver |
| Login | Cognito; roles y permisos en Postgres | |
| Migraciones | Alembic | |

**Despachador:** Postgres guarda el estado de cada factura (pendiente, en cola, procesando, lista, fallida).
Cada minuto, un cron del servidor envía a SQS hasta N facturas por hospital, en ronda, sin superar un
máximo en vuelo. Al terminar unas, el siguiente ciclo envía otras. Así:

- todos los hospitales avanzan a la vez;
- se puede dar prioridad a un lote urgente;
- la carga sobre RDS queda acotada.

**Costo estimado al mes (USD, precios de lista aproximados; sin IA, que suma ~$10–20):**

| Hospitales | Configuración | Costo |
|---|---|---|
| 1–2 | t4g.small + Lambda + RDS micro o small | ~$45–60 |
| 3–6 | Igual, RDS small Multi-AZ | ~$90–100 |
| 7–12 | t4g.medium + RDS medium Multi-AZ | ~$170–190 |
| 13–20 | 2 servidores de API + balanceador + RDS medium Multi-AZ | ~$280–300 |

**Plan B:** `procesar_factura(tenant_id, factura_id, s3_prefijo)` no depende de quién la llame. Si Lambda
diera problemas (memoria, tiempo, Tesseract), corre como worker en el servidor sin cambiar el validador.

**Pendiente de medir en el piloto:**
- tiempo y memoria por factura en Lambda;
- tamaño real de las carpetas;
- límite de concurrencia de Lambda de la cuenta (las cuentas nuevas pueden empezar con un límite bajo: pedir el aumento).
