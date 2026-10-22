from datetime import date

import pytest

from fabrica import NIT_EPS, Caso, carpeta
from validador import simulacion
from validador.base import config as config_mod
from validador.base.config import CapaConfig, Config, ReglaDocumentos, ResolutorConfig, validar_capa
from validador.execute import cli
from validador.pipeline import procesar_carpeta
from validador.store import db

HOSPITAL = "800000001"
BASE = Config(documentos_minimos=[ReglaDocumentos(nombre="pruebas", documentos={"RIPS": "info", "CUV": "info"})])
EXIGE_HC = {"documentos_minimos": [{"nombre": "contrato", "documentos": {"HEV": "grave"}}]}


def capa(id, hospital=None, eps=None, desde="2026-01-01", hasta=None, **datos):
    return CapaConfig(
        id=id, hospital_id=hospital, eps_nit=eps, vigente_desde=date.fromisoformat(desde),
        vigente_hasta=date.fromisoformat(hasta) if hasta else None, datos=datos,
    )


def tolerancia(r: ResolutorConfig, eps=NIT_EPS, fecha=date(2026, 7, 31)) -> int:
    return r.para(eps, fecha).cfg.tarifa.tolerancia_pesos


def test_la_capa_mas_especifica_gana():
    r = ResolutorConfig(BASE, [
        capa(1, tarifa={"tolerancia_pesos": 200}),
        capa(2, eps=NIT_EPS, tarifa={"tolerancia_pesos": 300}),
        capa(3, hospital=HOSPITAL, tarifa={"tolerancia_pesos": 400}, si_solo_pdf="grave"),
        capa(4, hospital=HOSPITAL, eps=NIT_EPS, tarifa={"tolerancia_pesos": 500}),
    ], HOSPITAL)
    resuelta = r.para(NIT_EPS, date(2026, 7, 31))
    assert resuelta.capas == (1, 2, 3, 4) and resuelta.cfg.tarifa.tolerancia_pesos == 500
    assert resuelta.cfg.si_solo_pdf == "grave"  # la capa 3 aporta lo que la 4 no cambia
    assert resuelta.cfg.tarifa.score_minimo == BASE.tarifa.score_minimo  # la mezcla es por clave
    assert tolerancia(r, eps="111") == 400  # otra EPS: no aplican las capas 2 ni 4


def test_capas_de_otro_hospital_no_aplican():
    r = ResolutorConfig(BASE, [capa(1, hospital="otro", tarifa={"tolerancia_pesos": 999})], HOSPITAL)
    assert tolerancia(r) == BASE.tarifa.tolerancia_pesos and r.para(NIT_EPS, None).capas == ()


def test_vigencia_por_fecha_de_la_factura():
    r = ResolutorConfig(BASE, [
        capa(1, hospital=HOSPITAL, desde="2026-01-01", tarifa={"tolerancia_pesos": 200}),
        capa(2, hospital=HOSPITAL, desde="2027-02-01", tarifa={"tolerancia_pesos": 500}),  # contrato nuevo
    ], HOSPITAL)
    assert tolerancia(r, fecha=date(2027, 1, 31)) == 200
    assert tolerancia(r, fecha=date(2027, 2, 1)) == 500
    assert tolerancia(r, fecha=date(2025, 12, 31)) == BASE.tarifa.tolerancia_pesos


def test_huella_identifica_la_configuracion_efectiva():
    r = ResolutorConfig(BASE, [capa(1, eps=NIT_EPS, tarifa={"tolerancia_pesos": 200})])
    assert r.para(NIT_EPS, None).huella != r.para("111", None).huella
    assert r.para("111", None).huella == ResolutorConfig(BASE).para(None, None).huella


@pytest.mark.parametrize(
    "datos, error",
    [
        ({"tarifa": {"tolerancia_peso": 200}}, "tolerancia_peso"),  # clave mal escrita
        ({"si_solo_pdf": "gravisimo"}, "si_solo_pdf"),
        ({"db_url": "sqlite:///otra.db"}, "infraestructura"),
    ],
)
def test_capa_invalida_se_rechaza(datos, error):
    with pytest.raises(ValueError, match=error):
        validar_capa(BASE, datos)


def test_ciclo_borrador_publicar_retirar(tmp_path):
    engine = db.conectar(f"sqlite:///{tmp_path / 'v.db'}")
    guardar = lambda tol, desde: db.guardar_borrador(  # noqa: E731
        engine, BASE, {"tarifa": {"tolerancia_pesos": tol}}, hospital_id=HOSPITAL, eps_nit=None,
        vigente_desde=date.fromisoformat(desde), creado_por="prueba",
    )
    v1 = guardar(200, "2026-01-01")
    assert db.capas_publicadas(engine, HOSPITAL) == []  # un borrador no rige
    db.cambiar_estado_capa(engine, v1, "publicada")
    v2 = guardar(300, "2026-06-01")
    db.cambiar_estado_capa(engine, v2, "publicada")
    assert tolerancia(ResolutorConfig(BASE, db.capas_publicadas(engine, HOSPITAL), HOSPITAL)) == 300
    db.cambiar_estado_capa(engine, v2, "retirada")  # vuelta atrás: rige otra vez la v1
    assert tolerancia(ResolutorConfig(BASE, db.capas_publicadas(engine, HOSPITAL), HOSPITAL)) == 200
    with pytest.raises(ValueError, match="retirada"):
        db.cambiar_estado_capa(engine, v2, "publicada")
    assert db.capas_publicadas(engine, "otro") == []


def test_pipeline_aplica_la_capa_y_registra_cual(tmp_path):
    d = carpeta(tmp_path, Caso())  # sin HC
    sin_capa = procesar_carpeta(d, "c1", BASE)
    r = ResolutorConfig(BASE, [capa(7, hospital=HOSPITAL, eps=NIT_EPS, **EXIGE_HC)], HOSPITAL)
    con_capa = procesar_carpeta(d, "c1", r)
    assert sin_capa.estado != "grave" and con_capa.estado == "grave"
    assert con_capa.config_capas == [7] and con_capa.hospital_id == HOSPITAL
    assert con_capa.config_huella != sin_capa.config_huella
    cambios = simulacion.comparar([sin_capa], [con_capa])
    assert cambios[0].despues == "grave" and "documentos: grave" in cambios[0].aparecen


def test_cli_borrador_simular_publicar(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VALIDADOR_DB_URL", f"sqlite:///{tmp_path / 'v.db'}")
    config_mod.get_config.cache_clear()
    try:
        carpeta(tmp_path / "lote", Caso())
        toml = tmp_path / "capa.toml"
        toml.write_text('[[documentos_minimos]]\nnombre = "contrato"\ndocumentos = { HEV = "grave" }\n', encoding="utf-8")
        assert cli.main(["config", "borrador", str(toml), "--hospital", HOSPITAL, "--eps", NIT_EPS, "--desde", "2026-01-01"]) == 0
        assert cli.main(["config", "simular", "1", str(tmp_path / "lote"), "--workers", "1", "--sin-ocr"]) == 0
        assert '"cambian": 1' in capsys.readouterr().out
        assert cli.main(["config", "publicar", "1"]) == 0
        assert cli.main(["config", "ver", "--hospital", HOSPITAL, "--eps", NIT_EPS]) == 0
        assert "Capas aplicadas: [1]" in capsys.readouterr().out
        toml.write_text("[tarifa]\ntolerancia_peso = 5\n", encoding="utf-8")
        assert cli.main(["config", "borrador", str(toml), "--desde", "2026-01-01"]) == 1
    finally:
        config_mod.get_config.cache_clear()
