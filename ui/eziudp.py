# -*- coding: utf-8 -*-
"""
Usługi powiatowe (EZiUDP) w panelu: zakres z kroku 2, zapis z kroku 3,
postęp na liście zadań – tak samo jak dla usług zbiorczych.
"""
import os

from qgis.core import Qgis, QgsGeometry

from . import tr
from ..core import tasks
from ..eziudp_wfs_task import AVAILABLE_LAYERS_BY_KEY, EziudpWfsTask
from ..powiaty_index import PowiatyIndex

# Zakresy, dla których usługi powiatowe mają sens (nie mają wspólnego pola ID ani obrębu)
SCOPES = ("teryt", "extent", "rect", "polygon", "layer")


def scope_problem(scope) -> str:
    if scope.kind not in SCOPES:
        return tr("Dla usług powiatowych wybierz zakres: jednostkę, mapę albo warstwę.")
    return ""


def _index() -> PowiatyIndex:
    return PowiatyIndex.instance(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def powiaty_for(geom: QgsGeometry) -> list:
    """Kody powiatów, które przecina geometria (EPSG:2180)."""
    idx = _index()
    return sorted(idx.teryts_intersecting(geom)) if idx.is_loaded() else []


def _powiat_rects(teryts: list) -> dict:
    """Prostokąty powiatów – cały powiat pobierany kaflami zamiast stronicowania."""
    idx = _index()
    out = {}
    for t in teryts:
        g = idx.geometry_for_teryt(t) if idx.is_loaded() else None
        if g is not None and not g.isEmpty():
            out[t] = g.boundingBox()
    return out


def start(dock, sel, scope, out) -> None:
    """Start pobierania z usług powiatowych dla wyboru z kroków 1–3."""
    keys = list(sel.eziudp)
    if scope.kind == "teryt":
        teryt = scope.teryt
        if len(teryt) == 2:
            teryts = sorted(t for t in dock.units.powiaty if t.startswith(teryt))
            launch(dock, keys, teryts, None, None, out, scope.describe())
        elif len(teryt) == 4:
            launch(dock, keys, [teryt], None, None, out, scope.describe())
        else:
            _start_with_boundary(dock, keys, teryt, out, scope.describe())
        return

    if scope.kind in ("extent", "rect"):
        rect = dock.canvas.extent() if scope.kind == "extent" else scope.rect
        geom = dock._to_2180(QgsGeometry.fromRect(rect))
    elif scope.kind == "polygon":
        geom = dock._to_2180(scope.geom)
    else:
        feats = scope.layer.selectedFeatures() if scope.selected_only else scope.layer.getFeatures()
        parts = [dock._to_2180(f.geometry(), scope.layer.crs()) for f in feats
                 if f.hasGeometry() and not f.geometry().isEmpty()]
        geom = QgsGeometry.unaryUnion(parts) if parts else QgsGeometry()
    if geom.isEmpty():
        dock.notify(tr("Zakres jest pusty."), Qgis.MessageLevel.Warning)
        return
    teryts = powiaty_for(geom)
    if not teryts:
        dock.notify(tr("W tym zakresie nie ma żadnego powiatu."), Qgis.MessageLevel.Warning)
        return
    launch(dock, keys, teryts, geom.boundingBox(), geom, out, scope.describe())


def _start_with_boundary(dock, keys, teryt, out, label) -> None:
    """Gmina / obręb: granica z PRG w tle, potem pobieranie z usługi powiatu przycięte do granicy."""
    row = dock.tasks.add(_title(keys, [teryt[:4]], label))
    task = tasks.BoundaryTask(teryt)
    row.attach(task)

    def done(geom):
        if not row.running:          # anulowane w trakcie pobierania granicy
            return
        if geom is None:
            row.finish(tr("Nie udało się pobrać granicy jednostki z PRG."), ok=False)
            row.offer_retry(lambda: _start_with_boundary(dock, keys, teryt, out, label))
            return
        launch(dock, keys, [teryt[:4]], geom.boundingBox(), geom, out, label, row=row)
    dock._run(task, done)


def _title(keys, teryts, label) -> str:
    what = (tr(AVAILABLE_LAYERS_BY_KEY[keys[0]]["label"]) if len(keys) == 1
            else tr("Usługi powiatowe: warstw ") + str(len(keys)))
    where = label if len(teryts) == 1 else label + " · " + tr("powiatów: ") + str(len(teryts))
    return f"{what} · {where}"


def launch(dock, keys, teryts, rect, clip, out, label, row=None, pairs=None) -> None:
    whole = rect is None
    task = EziudpWfsTask(teryts, keys, rect, clip, out, pairs, scope_text=label,
                         powiat_rects=_powiat_rects(teryts) if whole else None)
    if row is None:
        row = dock.tasks.add(_title(keys, teryts, label), task)
    else:
        row.attach(task)
    task.count_changed.connect(row.set_count)

    def done(report):
        names = ", ".join(sorted(lyr.name() for lyr in report.written))
        n = report.total_features
        dock._batch["objects"] += n
        dock._batch["layers"].update(lyr.name() for lyr in report.written)
        problems = list(report.errors)
        if problems or report.canceled:
            dock._batch["problems"] += 1
        count = f"{n:,}".replace(",", " ")
        if report.canceled:
            text = tr("Przerwano. Zapisano obiektów: ") + count
        elif n:
            text = count + tr(" obiektów → ") + names
        elif problems:
            text = tr("Nie pobrano danych")
        elif report.skipped:
            text = tr("Powiat nie udostępnia tych warstw")
        else:
            text = tr("Brak obiektów w tym zakresie.")
        if problems and not report.canceled:
            text += "; " + (tr("1 problem: ") if len(problems) == 1
                            else tr("problemów: ") + f"{len(problems)}, " + tr("pierwszy: ")) + problems[0]
        if report.skipped and n:
            text += "; " + tr("pominięto warstw, których powiat nie udostępnia: ") + str(len(report.skipped))
        row.finish(text, ok=not problems and not report.canceled and (n > 0 or not report.skipped))
        if report.failed_pairs and not report.canceled:
            failed = list(report.failed_pairs)
            row.offer_retry(lambda: launch(dock, keys, sorted({t for t, _ in failed}), rect, clip, out,
                                           label + tr(" (ponowienie)"), pairs=failed))
        if report.written:
            row.offer_show(lambda: dock._zoom_to_layers(report.written))
    dock._run(task, done)
