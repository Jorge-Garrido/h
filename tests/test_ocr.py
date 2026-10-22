"""OCR de autorizaciones escaneadas (sintéticas): texto -> imagen -> PDF -> OCR -> verificación."""

import numpy as np
import pymupdf
import pytest

from fabrica import NIT_IPS, Caso, carpeta

from validador import localizar
from validador.base.config import Config, ReglaDocumentos
from validador.ocr import a_layout, imagenes, motor, tesseract
from validador.pipeline import procesar_carpeta

pytest.importorskip("rapidocr")


def _pdf_escaneado(tmp_path, rotar: int = 0):
    """Genera una 'autorización' como imagen (sin texto digital), opcionalmente girada."""
    src = pymupdf.open()
    p = src.new_page(width=420, height=300)
    t = lambda x, y, s: p.insert_text((x, y), s, fontsize=11)  # noqa: E731
    t(20, 40, "AUTORIZACION DE SERVICIOS")
    t(20, 80, "No. Autorizacion : (POS) P020-307952247")
    t(20, 110, "Afiliado : CC 1000001 PEREZ GOMEZ JUAN")
    t(20, 160, "Codigo        Cant   Descripcion")
    t(20, 180, "SS-2001-930860   2   ELECTROMIOGRAFIA")
    t(20, 200, "SS-2001-891509   2   NEUROCONDUCCION")
    pix = p.get_pixmap(dpi=200)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    if rotar:
        img = np.ascontiguousarray(np.rot90(img, k=rotar // 90))
    out = pymupdf.open()
    h, w = img.shape[:2]
    pagina = out.new_page(width=612, height=792)
    pm = pymupdf.Pixmap(pymupdf.csRGB, w, h, img.tobytes(), False)
    ancho = 500
    pagina.insert_image(pymupdf.Rect(50, 100, 50 + ancho, 100 + ancho * h / w), pixmap=pm)
    ruta = tmp_path / f"PDE_{rotar}.pdf"
    out.save(ruta)
    return ruta


@pytest.mark.parametrize("rotar", [0, 90])
def test_ocr_confirma_autorizacion_y_cups(tmp_path, rotar):
    ruta = _pdf_escaneado(tmp_path, rotar)
    imgs = imagenes.extraer(ruta)
    assert len(imgs) == 1
    pares = [(i, motor.leer(i.imagen)) for i in imgs]
    doc = a_layout.a_layout(pares)
    assert localizar.verificar(doc, "numero_autorizacion", "307952247").estado == "confirmado"
    # El RIPS con un dígito faltante: el escaneo lo muestra como diferencia de 1
    v = localizar.verificar(doc, "numero_autorizacion", "37952247")
    assert v.estado == "diferencia" and v.distancia == 1
    assert localizar.verificar(doc, "paciente_id", "CC 1000001").estado == "confirmado"
    assert localizar.verificar(doc, "cups", "930860").estado == "confirmado"
    assert localizar.verificar(doc, "cups", "891509").estado == "confirmado"
    if rotar:
        assert pares[0][1].rotacion != 0  # detectó el giro


# --- pipeline con autorización escaneada ---------------------------------------------

LINEA_EMG = [("39143", "930860 ELECTROMIOGRAFIA", 1, 100000, "procedimientos", "930860")]


def _cfg(tmp_path):
    return Config(
        documentos_minimos=[ReglaDocumentos(nombre="pruebas", documentos={"RIPS": "info", "CUV": "info"})],
        ocr_cache_dir=str(tmp_path / "cache"),
    )


def _con_pde(tmp_path, caso, **kw):
    d = carpeta(tmp_path, caso, **kw)
    pde = _pdf_escaneado(tmp_path)
    pde.rename(d / f"PDE_{NIT_IPS}_{caso.factura}.pdf")
    return d


def _campo(r, nombre):
    return next(c for c in r.campos if c.campo == nombre)


def test_pipeline_escaneo_confirma_xml_y_senala_rips(tmp_path):
    caso = Caso(autorizacion="307952247", lineas=LINEA_EMG)
    r = procesar_carpeta(_con_pde(tmp_path, caso, cambios_rips={"autorizacion": "37952247"}), "c1", _cfg(tmp_path))
    c = _campo(r, "numero_autorizacion")
    assert c.valores_por_fuente["autorizacion_scan"] == "307952247"
    assert c.resultado == "grave" and c.documento_sospechoso == "rips" and c.valor_final == "307952247"
    linea = next(l for l in r.lineas if l.codigo == "930860")
    assert "autorización escaneada" not in " ".join(linea.motivos)  # el CUPS sí está en el escaneo


# Con un solo motor la confianza no alcanza min_confianza_autoridad: hace falta la segunda lectura.
@pytest.mark.skipif(not tesseract.disponible(), reason="requiere Tesseract (apt install tesseract-ocr)")
def test_autoridad_del_documento_eps_cuando_xml_y_rips_coinciden_mal(tmp_path):
    # XML, nota, PDF y RIPS dicen lo mismo, pero la autorización de la EPS dice otro número.
    caso = Caso(autorizacion="3079522477", lineas=LINEA_EMG)
    r = procesar_carpeta(_con_pde(tmp_path, caso), "c1", _cfg(tmp_path))
    c = _campo(r, "numero_autorizacion")
    assert c.valor_final == "307952247"
    assert set(c.documento_sospechoso.split(",")) >= {"xml", "rips"}
    assert c.resultado == "grave"
