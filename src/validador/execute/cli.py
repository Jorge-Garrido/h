"""Línea de comandos.

    validador tarifario importar documentos/base_cups.xlsx --uvb-anio 2025
    validador procesar "nuevaeps/NUEVA EPS EXITOSA" --salida salidas --hospital 800000001
    validador config borrador cambios.toml --hospital 800000001 --eps 900156264 --desde 2027-02-01
    validador config simular 7 "nuevaeps/NUEVA EPS EXITOSA"
    validador config publicar 7
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import shutil
import sys
import time
import tomllib
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .. import __version__, pipeline, reglas, report, salida_errores, simulacion
from ..base.config import CLAVES_INFRA, ResolutorConfig, get_config
from ..base.motivos import MOTIVOS
from ..ingest import discover
from ..store import db
from ..tarifario import importer


def _importar_tarifario(args: argparse.Namespace) -> int:
    cfg = get_config()
    res = importer.importar(Path(args.archivo), Decimal(args.uvb_valor) if args.uvb_valor else None)
    for a in res.avisos:
        print(f"AVISO  {a}")
    for e in res.errores:
        print(f"ERROR  {e}")
    if not res.ok:
        print(f"Tarifario rechazado: {len(res.errores)} errores. Corrija el Excel y vuelva a importar.")
        return 1
    engine = db.conectar(cfg.db_url)
    version_id = db.guardar_tarifario(engine, res, args.nombre or res.archivo, args.uvb_anio)
    if args.csv:
        importer.exportar_csv(res, Path(args.csv))
    print(f"Tarifario importado: versión {version_id}, {len(res.items)} ítems, UVB {res.uvb_valor}, sha256 {res.sha256[:12]}")
    return 0


def _trabajo(carpeta, corrida_id, cfg, tarifario):
    try:
        return pipeline.procesar_carpeta(carpeta, corrida_id, cfg, tarifario, None)
    except Exception as e:  # una factura rota no detiene la corrida, pero queda en los reportes
        logging.getLogger(__name__).error("factura %s: error %s", carpeta.name, type(e).__name__)
        return pipeline.resultado_fallido(carpeta, corrida_id, e)


def _procesar_todas(carpetas, corrida_id, cfg, tarifario, workers: int):
    """Procesa en paralelo por procesos (el OCR usa CPU). Cada proceso limita sus hilos."""
    if workers <= 1:
        for c in carpetas:
            yield _trabajo(c, corrida_id, cfg, tarifario)
        return
    os.environ["VALIDADOR_OCR_HILOS"] = str(max(1, (os.cpu_count() or 2) // workers))
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futuros = [ex.submit(_trabajo, c, corrida_id, cfg, tarifario) for c in carpetas]
        for f in as_completed(futuros):
            yield f.result()


def _resolutor(engine, hospital_id: str | None, sin_ocr: bool = False, extra=()) -> ResolutorConfig:
    capas = (db.capas_publicadas(engine, hospital_id) if engine is not None else []) + list(extra)
    return ResolutorConfig(get_config(), capas, hospital_id, {"ocr_habilitado": False} if sin_ocr else None)


def _procesar(args: argparse.Namespace) -> int:
    base = get_config()
    engine = None if args.sin_db else db.conectar(base.db_url)
    cfg = _resolutor(engine, args.hospital, args.sin_ocr)
    tarifario = db.cargar_tarifario(engine, args.tarifario) if engine is not None else None
    if tarifario is None:
        print("AVISO  sin tarifario cargado: no se validan tarifas (use 'validador tarifario importar').")

    corrida_id = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}-{secrets.token_hex(2)}"
    salida = Path(args.salida) / corrida_id.replace(":", "")
    if engine is not None:
        db.iniciar_corrida(engine, corrida_id, __version__)

    carpetas = discover.carpetas_de_facturas(Path(args.ruta))
    if args.limite:
        carpetas = carpetas[: args.limite]
    resultados = []
    t0 = time.perf_counter()
    for i, r in enumerate(_procesar_todas(carpetas, corrida_id, cfg, tarifario, args.workers), 1):
        if engine is not None:
            pipeline.marcar_duplicados(r, engine)
            db.guardar_resultado(engine, r)
        resultados.append(r)
        # El JSON trae los valores de cada fuente (datos del paciente): solo se escribe si hay algo que
        # revisar, salvo que se pida con --json-todas (depuración).
        if args.json_todas or r.estado in ("aviso", "grave"):
            report.escribir_factura(r, salida)
        print(f"\r  {i}/{len(carpetas)} facturas ({time.perf_counter() - t0:.0f} s)", end="", flush=True)
    print()

    resumen = report.resumen(corrida_id, resultados)
    resumen["hospital_id"] = args.hospital
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    report.escribir_casos(resultados, salida / "casos_con_posible_error.csv")
    report.escribir_propuestas(resultados, salida / "propuestas_correccion.json")
    eps = salida_errores.nombre_eps(Path(args.ruta))
    carpeta_eps = salida_errores.preparar_destino(Path(args.errores), eps)
    salida_errores.copiar_errores(resultados, carpeta_eps)
    copiadas = {r.factura for r in resultados if salida_errores.discrepancias(r)}
    reporte = carpeta_eps / f"REPORTE {eps}.xlsx"
    salida_errores.escribir_excel(resultados, reporte, eps, corrida_id, copiadas)
    shutil.copy(reporte, salida / reporte.name)
    if engine is not None:
        db.cerrar_corrida(engine, corrida_id, resumen)
    _imprimir_discrepancias(resultados)
    print(f"\nFacturas: {len(resultados)} | con discrepancias: {len(copiadas)} | sin discrepancias: {len(resultados) - len(copiadas)}")
    print(f"Carpetas con error: {carpeta_eps}")
    print(f"Reporte Excel:      {reporte}")
    print(f"Detalle técnico:    {salida}")
    return 0


# --- capas de configuración ---------------------------------------------------


def _fecha(texto: str | None) -> date | None:
    return date.fromisoformat(texto) if texto else None


def _config_borrador(args: argparse.Namespace) -> int:
    base = get_config()
    datos = tomllib.loads(Path(args.archivo).read_text(encoding="utf-8"))
    try:
        capa_id = db.guardar_borrador(
            db.conectar(base.db_url), base, datos,
            hospital_id=args.hospital, eps_nit=args.eps, vigente_desde=_fecha(args.desde), vigente_hasta=_fecha(args.hasta),
            comentario=args.comentario, creado_por=args.autor,
        )
    except ValueError as e:  # incluye los errores de validación de Pydantic
        print(f"Capa rechazada:\n{e}")
        return 1
    print(f"Borrador {capa_id} guardado. Simúlelo con 'validador config simular {capa_id} <carpeta>' antes de publicarlo.")
    return 0


def _config_estado(args: argparse.Namespace) -> int:
    try:
        db.cambiar_estado_capa(db.conectar(get_config().db_url), args.id, args.nuevo_estado)
    except ValueError as e:
        print(e)
        return 1
    print(f"Capa {args.id}: {args.nuevo_estado}")
    return 0


def _config_listar(args: argparse.Namespace) -> int:
    for c in db.listar_capas(db.conectar(get_config().db_url), args.hospital):
        ambito = f"hospital={c['hospital_id'] or '*'} eps={c['eps_nit'] or '*'}"
        vigencia = f"{c['vigente_desde']} a {c['vigente_hasta'] or '...'}"
        print(f"{c['id']:>4}  {c['estado']:9}  {ambito:38}  {vigencia:25}  {c['creado_por']:12}  {c['comentario'] or ''}")
    return 0


def _diferencias(base: dict, efectiva: dict, prefijo: str = "") -> dict:
    out = {}
    for k, v in efectiva.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out |= _diferencias(base[k], v, f"{prefijo}{k}.")
        elif base.get(k) != v:
            out[f"{prefijo}{k}"] = v
    return out


def _config_ver(args: argparse.Namespace) -> int:
    base = get_config()
    r = _resolutor(db.conectar(base.db_url), args.hospital).para(args.eps, _fecha(args.fecha))
    print(f"Capas aplicadas: {list(r.capas) or 'ninguna'} | huella {r.huella}")
    excluir = set(CLAVES_INFRA)
    cambios = _diferencias(base.model_dump(mode="json", exclude=excluir), r.cfg.model_dump(mode="json", exclude=excluir))
    print(json.dumps(cambios, ensure_ascii=False, indent=2) if cambios else "Igual a la configuración base.")
    return 0


def _config_simular(args: argparse.Namespace) -> int:
    base = get_config()
    engine = db.conectar(base.db_url)
    fila = db.capa(engine, args.id)
    if fila is None or fila["estado"] != "borrador":
        print(f"La capa {args.id} no existe o no es un borrador.")
        return 1
    hospital = fila["hospital_id"]
    actual = _resolutor(engine, hospital, args.sin_ocr)
    nuevo = _resolutor(engine, hospital, args.sin_ocr, extra=[db.a_capa(fila)])
    carpetas = discover.carpetas_de_facturas(Path(args.ruta))[: args.limite]
    tarifario = db.cargar_tarifario(engine)
    # Sin base de datos en el procesamiento: la simulación no guarda resultados ni marca duplicados.
    antes = list(_procesar_todas(carpetas, "simulacion", actual, tarifario, args.workers))
    despues = list(_procesar_todas(carpetas, "simulacion", nuevo, tarifario, args.workers))
    cambios = simulacion.comparar(antes, despues)
    for c in cambios:
        print(f"  {c.factura}  {c.antes} -> {c.despues}  +{c.aparecen}  -{c.desaparecen}")
    print(json.dumps(simulacion.resumen(cambios, len(carpetas)), ensure_ascii=False, indent=2))
    return 0


def _reglas_listar(args: argparse.Namespace) -> int:
    """Registro de reglas con sus códigos, severidad por defecto y el ajuste de la configuración activa."""
    cfg = get_config()
    for r in reglas.REGISTRO.values():
        print(f"{r.id}  [{r.ambito}]  {r.descripcion}")
        for codigo, defecto in r.severidades.items():
            ajuste = cfg.reglas.get(codigo)
            if ajuste is not None and not ajuste.activa:
                efectivo = "-> desactivada"
            elif ajuste is not None and ajuste.severidad:
                efectivo = f"-> {ajuste.severidad}"
            else:
                efectivo = ""
            print(f"    {codigo:34} {defecto or 'según el caso':14} {efectivo:16} {MOTIVOS[codigo]}")
    return 0


def _imprimir_discrepancias(resultados) -> None:
    filas = [(r.factura, d) for r in resultados for d in salida_errores.discrepancias(r)]
    if not filas:
        print("Sin discrepancias.")
        return
    print("\nCAMPOS CON DISCREPANCIA")
    for factura, d in filas:
        valores = "  ".join(
            f"{salida_errores.NOMBRE_FUENTE.get(k, k)}={v}{' (!)' if k in d['sospechosos'] else ''}" for k, v in d["valores"].items()
        )
        print(f"  {factura}  {d['severidad'].upper():5}  {d['campo'][:40]:40}  {valores}")
        if d["motivo"] and (d["tipo"] != "Campo" or not valores):
            print(f"  {'':9}  {'':5}  {'':40}  → {d['motivo']}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="validador")
    sub = p.add_subparsers(dest="comando", required=True)

    t = sub.add_parser("tarifario", help="gestión del tarifario").add_subparsers(dest="accion", required=True)
    imp = t.add_parser("importar", help="importa el Excel del tarifario a la base de datos")
    imp.add_argument("archivo")
    imp.add_argument("--nombre")
    imp.add_argument("--uvb-anio", type=int)
    imp.add_argument("--uvb-valor", help="si el Excel no trae el valor de la UVB en el encabezado")
    imp.add_argument("--csv", help="exporta además una copia normalizada en CSV")
    imp.set_defaults(func=_importar_tarifario)

    pr = sub.add_parser("procesar", help="valida una carpeta de factura o una carpeta con varias")
    pr.add_argument("ruta")
    pr.add_argument("--salida", default="salidas")
    pr.add_argument("--limite", type=int)
    pr.add_argument("--tarifario", type=int, help="id de versión del tarifario (por defecto la última)")
    pr.add_argument("--sin-db", action="store_true", help="no usa la base de datos (sin tarifario ni duplicados)")
    pr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 4), help="procesos en paralelo")
    pr.add_argument("--sin-ocr", action="store_true", help="no lee las autorizaciones escaneadas")
    pr.add_argument("--json-todas", action="store_true", help="escribe el JSON de todas las facturas (por defecto solo aviso/grave)")
    pr.add_argument("--errores", default="CON ERROR", help="carpeta donde se copian las facturas con discrepancias (una subcarpeta por EPS)")
    pr.add_argument("--hospital", help="id del hospital: aplica sus capas de configuración publicadas")
    pr.set_defaults(func=_procesar)

    rg = sub.add_parser("reglas", help="reglas de validación registradas").add_subparsers(dest="accion", required=True)
    rg.add_parser("listar", help="códigos, severidad por defecto y ajustes de la configuración (VALIDADOR_CONFIG)").set_defaults(
        func=_reglas_listar
    )

    c = sub.add_parser("config", help="capas de configuración en la base de datos").add_subparsers(dest="accion", required=True)
    b = c.add_parser("borrador", help="valida un TOML con las claves que cambian y lo guarda como borrador")
    b.add_argument("archivo")
    b.add_argument("--hospital", help="sin --hospital la capa es global")
    b.add_argument("--eps", help="NIT de la EPS sin dígito de verificación")
    b.add_argument("--desde", required=True, help="AAAA-MM-DD (fecha de emisión de la factura)")
    b.add_argument("--hasta")
    b.add_argument("--comentario")
    b.add_argument("--autor", default=getpass.getuser())
    b.set_defaults(func=_config_borrador)
    for accion, estado in (("publicar", "publicada"), ("retirar", "retirada")):
        e = c.add_parser(accion, help=f"marca la capa como {estado}")
        e.add_argument("id", type=int)
        e.set_defaults(func=_config_estado, nuevo_estado=estado)
    ls = c.add_parser("listar", help="lista las capas (todas, o las que aplican a un hospital)")
    ls.add_argument("--hospital")
    ls.set_defaults(func=_config_listar)
    v = c.add_parser("ver", help="configuración efectiva para un hospital, una EPS y una fecha")
    v.add_argument("--hospital")
    v.add_argument("--eps")
    v.add_argument("--fecha", help="AAAA-MM-DD (por defecto hoy)")
    v.set_defaults(func=_config_ver)
    s = c.add_parser("simular", help="procesa facturas con y sin el borrador y muestra qué cambia")
    s.add_argument("id", type=int)
    s.add_argument("ruta")
    s.add_argument("--limite", type=int)
    s.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 4))
    s.add_argument("--sin-ocr", action="store_true")
    s.set_defaults(func=_config_simular)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
