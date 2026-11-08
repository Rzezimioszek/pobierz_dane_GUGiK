# -*- coding: utf-8 -*-
"""Typy pól, zapis do GeoPackage i warstwy EZiUDP tworzone w głównym wątku."""
import importlib
import os

from conftest import PLUGIN_DIR, PLUGIN_NAME


def mod(name):
    return importlib.import_module(f"{PLUGIN_NAME}.{name}")


SQUARE = "POLYGON((284000 366000, 284010 366000, 284010 366010, 284000 366010, 284000 366000))"


def feats(rows):
    return [{"geom": SQUARE, "attrs": dict(r)} for r in rows]


# ---------------------------------------------------------------- typy pól

def test_rcn_prices_become_numbers_when_all_values_are_numbers():
    from qgis.core import QgsProject
    layers, catalog = mod("core.layers"), mod("core.catalog")
    src = catalog.source_for_label("dzialki (RCN)")
    project = QgsProject()
    vl = layers.write(feats([
        {"tran_lokalny_id_iip": "a", "dzi_cena_brutto": "125000,50", "dzi_pow_ewid": "0.1234"},
        {"tran_lokalny_id_iip": "b", "dzi_cena_brutto": "99000", "dzi_pow_ewid": ""},
    ]), "R", "MultiPolygon", src.unique_field, numeric=src.numeric_fields, project=project)
    f = vl.fields()
    assert f.field("dzi_cena_brutto").isNumeric() and f.field("dzi_pow_ewid").isNumeric()
    values = sorted(x["dzi_cena_brutto"] for x in vl.getFeatures())
    assert values == [99000.0, 125000.5]


def test_text_or_leading_zero_keeps_field_as_text():
    from qgis.core import QgsProject
    layers = mod("core.layers")
    project = QgsProject()
    vl = layers.write(feats([
        {"kond": "3", "kod": "0003"},
        {"kond": "brak_uprawnień", "kod": "0010"},
    ]), "T", "MultiPolygon", numeric={"kond": "int", "kod": "int"}, project=project)
    assert not vl.fields().field("kond").isNumeric()
    assert not vl.fields().field("kod").isNumeric()
    assert {x["kond"] for x in vl.getFeatures()} == {"3", "brak_uprawnień"}


def test_egib_fields_stay_text():
    catalog = mod("core.catalog")
    assert catalog.source_for_label("budynki (EGIB)").numeric_fields is None
    assert catalog.source_for_label("dzialki (EGIB)").numeric_fields is None


# ---------------------------------------------------------------- GeoPackage

def test_gpkg_create_append_dedupe_and_reopen(tmp_path):
    from qgis.core import QgsProject, QgsVectorLayer
    layers = mod("core.layers")
    path = str(tmp_path / "wyniki.gpkg")
    project = QgsProject()
    rows = [{"ID": str(i), "N": "x"} for i in range(3)]
    vl = layers.write(feats(rows), "Dzialki", "MultiPolygon", "ID", project=project, gpkg_path=path)
    assert vl.providerType() == "ogr" and os.path.exists(path)
    layers.write(feats(rows + [{"ID": "3", "N": "y"}]), "Dzialki", "MultiPolygon", "ID",
                 project=project, gpkg_path=path)
    layers.write(feats([{"ID": "9", "N": "z"}]), "Budynki", "MultiPolygon", "ID",
                 project=project, gpkg_path=path)
    del vl
    project.removeAllMapLayers()
    reopened = QgsVectorLayer(f"{path}|layername=Dzialki", "d", "ogr")
    assert reopened.isValid() and reopened.featureCount() == 4
    assert QgsVectorLayer(f"{path}|layername=Budynki", "b", "ogr").featureCount() == 1


def test_gpkg_appends_to_file_layer_from_earlier_session(tmp_path):
    from qgis.core import QgsProject, QgsVectorLayer
    layers = mod("core.layers")
    path = str(tmp_path / "w.gpkg")
    layers.write(feats([{"ID": "1"}]), "D", "MultiPolygon", "ID", project=QgsProject(), gpkg_path=path)
    project = QgsProject()   # „nowa sesja”: warstwy nie ma w projekcie, jest w pliku
    layers.write(feats([{"ID": "1"}, {"ID": "2"}]), "D", "MultiPolygon", "ID",
                 project=project, gpkg_path=path)
    assert QgsVectorLayer(f"{path}|layername=D", "d", "ogr").featureCount() == 2


def test_gpkg_mode_does_not_append_to_memory_layer_with_same_name(tmp_path):
    from qgis.core import QgsProject
    layers = mod("core.layers")
    project = QgsProject()
    mem = layers.write(feats([{"ID": "1"}]), "D", "MultiPolygon", "ID", project=project)
    gp = layers.write(feats([{"ID": "2"}]), "D", "MultiPolygon", "ID", project=project,
                      gpkg_path=str(tmp_path / "x.gpkg"))
    assert mem.featureCount() == 1 and gp.featureCount() == 1 and mem.id() != gp.id()


def test_output_from_settings(tmp_path):
    from qgis.core import QgsSettings
    layers = mod("core.layers")
    s = QgsSettings()
    s.setValue(f"{layers.SETTINGS_PREFIX}/output_mode", "gpkg")
    s.setValue(f"{layers.SETTINGS_PREFIX}/gpkg_path", str(tmp_path / "a.gpkg"))
    assert layers.Output.from_settings().gpkg_path.endswith("a.gpkg")
    s.setValue(f"{layers.SETTINGS_PREFIX}/output_mode", "memory")
    assert layers.Output.from_settings().gpkg_path is None


# ---------------------------------------------------------------- EZiUDP

def test_eziudp_snapshot_keeps_types_and_adds_meta():
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer
    task_mod = mod("eziudp_wfs_task")
    src = QgsVectorLayer("Point?crs=epsg:2180&field=nr:string&field=h:double", "wfs", "memory")
    f = QgsFeature(src.fields())
    f.setGeometry(QgsGeometry.fromWkt("POINT(500000 300000)"))
    f.setAttributes(["P1", 201.25])
    src.dataProvider().addFeatures([f])
    fields, geom_type, features = task_mod._read_snapshot(src, None, "1465", "http://x", lambda: False)
    assert [x.name() for x in fields] == ["nr", "h", "teryt_powiatu", "zrodlo_wfs"]
    assert features[0]["attrs"]["h"] == 201.25 and features[0]["attrs"]["teryt_powiatu"] == "1465"


def test_eziudp_layers_created_only_in_finished():
    from qgis.core import QgsGeometry, QgsProject, QgsVectorLayer
    task_mod = mod("eziudp_wfs_task")
    QgsProject.instance().removeAllMapLayers()
    task = task_mod.EziudpWfsTask([], ["bdsog_poziom"])
    src = QgsVectorLayer("Point?crs=epsg:2180&field=nr:string", "wfs", "memory")
    fields, _, _ = task_mod._read_snapshot(src, None, "1465", "u", lambda: False)
    task._pending.append({"name": "Osnowa_pozioma_1465", "fields": fields, "geom_type": "Point",
                          "style": "Osnowa_pozioma",
                          "features": [{"geom": QgsGeometry.fromWkt("POINT(1 2)"),
                                        "attrs": {"nr": "P1", "teryt_powiatu": "1465", "zrodlo_wfs": "u"}}]})
    assert not QgsProject.instance().mapLayersByName("Osnowa_pozioma_1465")
    task.finished(True)
    vl = QgsProject.instance().mapLayersByName("Osnowa_pozioma_1465")[0]
    assert vl.featureCount() == 1 and vl.renderer() is not None
    QgsProject.instance().removeAllMapLayers()


def test_eziudp_worker_code_does_not_touch_project():
    """QgsProject tylko w __init__ (główny wątek); nigdy w ścieżce run()."""
    src = open(os.path.join(PLUGIN_DIR, "eziudp_wfs_task.py"), encoding="utf-8").read()
    run_part = src[src.index("    def run(self)"):]
    run_part = run_part[:run_part.index("    def finished(")] + \
        run_part[run_part.index("    def _process_one_teryt("):]
    assert "QgsProject" not in run_part
    assert "QgsProject" not in src[src.index("def _read_snapshot("):src.index("class EziudpWfsTask")]


def test_gpkg_with_source_fid_field(tmp_path):
    from qgis.core import QgsFields, QgsField, QgsProject, QgsVectorLayer
    layers = mod("core.layers")
    qc = mod("qt_compat")
    fields = QgsFields()
    fields.append(QgsField("fid", qc.FIELD_STRING))
    fields.append(QgsField("nr", qc.FIELD_STRING))
    path = str(tmp_path / "f.gpkg")
    layers.write(feats([{"fid": "abc", "nr": "1"}]), "F", "MultiPolygon", project=QgsProject(),
                 fields=fields, gpkg_path=path)
    vl = QgsVectorLayer(f"{path}|layername=F", "f", "ogr")
    assert [f["fid"] for f in vl.getFeatures()] == ["abc"]
