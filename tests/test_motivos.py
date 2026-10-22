import pytest
from pydantic import ValidationError

from fabrica import Caso, carpeta
from validador.base.config import Config, ReglaDocumentos
from validador.base.motivos import MOTIVOS
from validador.base.schemas import Hallazgo
from validador.pipeline import procesar_carpeta

CFG = Config(documentos_minimos=[ReglaDocumentos(nombre="exigente", documentos={"HEV": "grave", "PDE": "aviso"})])


@pytest.fixture
def resultado(tmp_path):
    # Factura con varias novedades: documento y autorización distintos en el RIPS, sin HC ni autorización.
    caso = Caso()
    d = carpeta(tmp_path, caso, cambios_rips={"numero_id": "1000009", "autorizacion": "17458299"})
    return caso, procesar_carpeta(d, "c1", CFG)


def test_toda_novedad_tiene_codigo_del_catalogo(resultado):
    _, r = resultado
    assert r.estado == "grave"
    for c in r.campos:
        assert c.codigos and set(c.codigos) <= MOTIVOS.keys(), c.campo
    for l in r.lineas:
        assert set(l.codigos) <= MOTIVOS.keys()
        assert l.resultado == "ok" or l.codigos, l.codigo
    assert all(h.codigo in MOTIVOS for h in r.hallazgos)
    por_campo = {c.campo: c.codigos for c in r.campos}
    assert "CAMPO.SIN_MAYORIA" in por_campo["paciente_id"]  # RIPS contra XML: uno a uno, sin mayoría
    assert {("DOC.FALTA", "HEV"), ("DOC.FALTA", "PDE")} <= {(h.codigo, h.ref) for h in r.hallazgos}


def test_codigos_y_referencias_sin_datos_del_paciente(resultado):
    caso, r = resultado
    sensibles = {caso.numero_id, "1000009", caso.autorizacion, "17458299", *caso.nombre.split()}
    guardado = [*(k for c in r.campos for k in c.codigos), *(k for l in r.lineas for k in l.codigos)]
    guardado += [x for h in r.hallazgos for x in (h.codigo, h.ref) if x]
    assert not [g for g in guardado if any(s in g for s in sensibles)]


def test_codigo_desconocido_se_rechaza():
    with pytest.raises(ValidationError, match="desconocido"):
        Hallazgo(tipo="documentos", codigo="DOC.INVENTADO", resultado="info", motivo="x")
