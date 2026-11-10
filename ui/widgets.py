# -*- coding: utf-8 -*-
"""
Części panelu:
    SourcePicker – krok 1: co pobrać (lista źródeł z wyszukiwarką)
    ScopePicker  – krok 2: skąd (jednostka, mapa, warstwa, ID, obręb + nr)
    OutputPicker – krok 3: jak zapisać
    TasksPanel   – kolejka zadań z postępem i anulowaniem
"""
from dataclasses import dataclass, field
from typing import Optional

from qgis.core import QgsApplication, QgsSettings
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QCompleter, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QSizePolicy, QTabWidget, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import tr
from ..core import catalog, layers, tasks, uldk
from ..core.catalog import EGIB_DZIALKI_ATTRIBUTES
from ..qt_compat import LAYER_FILTER_POLYGON
from ..skorowidz_services import SKOROWIDZ_BY_LABEL, SKOROWIDZ_SERVICES
from ..teryt_scope_widget import TerytScopeWidget
from .admin_units import normalize_teryt

SETTINGS = layers.SETTINGS_PREFIX
ROLE = Qt.ItemDataRole.UserRole
EZIUDP = "Usługi powiatowe (EZiUDP)"

_KIND_NAMES = {
    "dzialki": "Działki", "budynki": "Budynki", "lokale": "Lokale",
}
_PRG_GROUPS = [
    ("A", "Granice administracyjne i ewidencyjne"), ("R", "Rejony i obwody statystyczne"),
    ("S", "Sądy"), ("P", "Prokuratury"), ("K", "Policja, straże, obrona cywilna"),
    ("U", "Urzędy i instytucje"), ("W", "Obszary morskie"),
]


def hint_label(name: str = "hint") -> QLabel:
    lbl = QLabel()
    lbl.setWordWrap(True)
    lbl.setObjectName(name)
    return lbl


def set_state(widget, state: str) -> None:
    """Ustawia właściwość „state” używaną w style.qss i odświeża wygląd."""
    if widget.property("state") == state:
        return
    widget.setProperty("state", state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _collapsed_key(key: str) -> str:
    return f"{SETTINGS}/ui/collapsed/{key}"


class _ElideLabel(QLabel):
    """Etykieta skracana wielokropkiem, gdy brakuje miejsca (nie poszerza panelu)."""

    def paintEvent(self, e):
        from qgis.PyQt.QtGui import QPainter
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        p.drawText(self.rect(), int(self.alignment()), text)
        p.end()


class _Header(QWidget):
    """Nagłówek karty – kliknięcie w dowolnym miejscu zwija/rozwija kartę."""
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class StepCard(QFrame):
    """
    Karta kroku: strzałka zwijania, numer w kółku (szary → kolor, gdy krok
    kompletny), tytuł i skrót wyboru. Zwinięta karta pokazuje tylko nagłówek
    ze skrótem; stan zwinięcia jest pamiętany między sesjami.
    """
    toggled = pyqtSignal(bool)

    def __init__(self, number: int, title: str, parent=None, key: str = ""):
        super().__init__(parent)
        self.setObjectName("stepCard")
        self.key = key or f"step{number}"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)
        self.header = _Header()
        self.header.setObjectName("stepHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setToolTip(tr("Kliknij, aby zwinąć lub rozwinąć"))
        head = QHBoxLayout(self.header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.arrow = QToolButton()
        self.arrow.setObjectName("stepArrow")
        self.arrow.setAutoRaise(True)
        self.arrow.clicked.connect(self.toggle)
        head.addWidget(self.arrow)
        self.badge = QLabel(str(number))
        self.badge.setObjectName("stepBadge")
        self.badge.setProperty("number", str(number))
        head.addWidget(self.badge)
        self.title = QLabel(title)
        self.title.setObjectName("stepTitle")
        head.addWidget(self.title)
        self.summary = _ElideLabel()
        self.summary.setObjectName("stepSummary")
        self.summary.setWordWrap(False)
        self.summary.setMinimumWidth(0)
        # skrót nie może poszerzać panelu – w wąskim panelu jest przycinany
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.summary, 1)
        self.header.clicked.connect(self.toggle)
        outer.addWidget(self.header)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.content)
        self._summary = ""
        self._state = "idle"
        self.set_collapsed(QgsSettings().value(_collapsed_key(self.key), False, type=bool), save=False)
        self.set_state("idle")

    # ---------------------------------------------------------------- zwijanie
    def is_collapsed(self) -> bool:
        return self.content.isHidden()

    def set_collapsed(self, collapsed: bool, save: bool = True) -> None:
        self.content.setVisible(not collapsed)
        self.arrow.setText("▸" if collapsed else "▾")
        set_state(self.header, "collapsed" if collapsed else "open")
        self._update_summary()
        if save:
            QgsSettings().setValue(_collapsed_key(self.key), bool(collapsed))
        self.toggled.emit(collapsed)

    def toggle(self) -> None:
        self.set_collapsed(not self.is_collapsed())

    # ---------------------------------------------------------------- stan
    def set_state(self, state: str, summary: str = "") -> None:
        """idle – jeszcze nie dotyczy, todo – czegoś brakuje, done – gotowe."""
        self._state, self._summary = state, summary
        set_state(self, state)
        set_state(self.badge, state)
        self.badge.setText("✓" if state == "done" else self.badge.property("number") or self.badge.text())
        self._update_summary()

    def _update_summary(self) -> None:
        text = self._summary
        if not text and self.is_collapsed() and self._state == "todo":
            text = tr("do uzupełnienia – rozwiń")
        self.summary.setToolTip(text)
        self.summary.setText(text)


def format_area(m2: float) -> str:
    """Powierzchnia zakresu: m², ha albo km² – zależnie od wielkości."""
    if m2 < 10_000:
        return f"{m2:.0f} m²"
    if m2 < 1_000_000:
        return f"{m2 / 10_000:.1f} ha".replace(".", ",")
    return f"{m2 / 1_000_000:.2f} km²".replace(".", ",")


def step_box(number: int, title: str, key: str = "") -> tuple:
    card = StepCard(number, title, key=key)
    return card, card.body


# ====================================================================== krok 1

@dataclass
class Selection:
    sources: list = field(default_factory=list)       # catalog.Source
    skorowidz: Optional[object] = None                # SkorowidzService
    years: list = field(default_factory=list)         # roczniki skorowidza
    eziudp: bool = False

    @property
    def empty(self) -> bool:
        return not self.sources and self.skorowidz is None and not self.eziudp

    def names(self) -> str:
        if self.eziudp:
            return EZIUDP
        if self.skorowidz is not None:
            return f"skorowidz: {self.skorowidz.label}"
        return ", ".join(SourcePicker.display_name(s) for s in self.sources)


class SourcePicker(QWidget):
    """Lista źródeł w grupach. Warstwy WFS można łączyć; skorowidz i EZiUDP są wyłączne."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Szukaj: działki, budynki, NMT, adresy…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setObjectName("sourceTree")
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumHeight(255)
        self.tree.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.tree)

        # Roczniki skorowidza
        self.years_row = QWidget()
        yl = QHBoxLayout(self.years_row)
        yl.setContentsMargins(0, 0, 0, 0)
        yl.addWidget(QLabel(tr("Rocznik od:")))
        self.year_from = QComboBox()
        yl.addWidget(self.year_from, 1)
        yl.addWidget(QLabel(tr("do:")))
        self.year_to = QComboBox()
        yl.addWidget(self.year_to, 1)
        self.years_row.setVisible(False)
        lay.addWidget(self.years_row)

        self.summary = hint_label()
        lay.addWidget(self.summary)

        self._updating = False
        self._items = []
        self._build()
        self._restore()
        self._refresh()

    # ------------------------------------------------------------------ budowa

    @staticmethod
    def display_name(source) -> str:
        kind = source.label.split(" ")[0]
        if source.group in ("EGIB", "RCN"):
            return f"{_KIND_NAMES.get(kind, kind)} ({'EGiB' if source.group == 'EGIB' else 'RCN'})"
        return kind.replace("_", " ")

    def _group(self, title: str, parent=None) -> QTreeWidgetItem:
        item = QTreeWidgetItem([title])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _leaf(self, parent, text: str, data: tuple, tooltip: str = "") -> QTreeWidgetItem:
        item = QTreeWidgetItem([text])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        item.setData(0, ROLE, data)
        if tooltip:
            item.setToolTip(0, tooltip)
        parent.addChild(item)
        self._items.append(item)
        return item

    def _build(self) -> None:
        by_group = {}
        for s in catalog.SOURCES:
            by_group.setdefault(s.group, []).append(s)

        g = self._group(tr("EGiB – działki i budynki"))
        for s in by_group["EGIB"]:
            self._leaf(g, self.display_name(s), ("wfs", s.label), tr("Usługa zbiorcza WFS EGiB"))
        g.setExpanded(True)

        g = self._group(tr("RCN – ceny nieruchomości"))
        for s in by_group["RCN"]:
            self._leaf(g, self.display_name(s), ("wfs", s.label), tr("Rejestr cen nieruchomości"))

        g = self._group(tr("PRG – adresy i ulice"))
        for s in by_group["PRG"]:
            self._leaf(g, self.display_name(s), ("wfs", s.label))

        g = self._group(tr("PRNG – nazwy geograficzne"))
        for s in by_group["PRNG"]:
            self._leaf(g, self.display_name(s), ("wfs", s.label))

        g = self._group(tr("PRG – granice i jednostki"))
        for letter, title in _PRG_GROUPS:
            sub = self._group(tr(title), g)
            for s in by_group.get(f"PRG_{letter}", []):
                self._leaf(sub, self.display_name(s), ("wfs", s.label))

        g = self._group(tr("Skorowidze: ortofotomapy, NMT, LIDAR"))
        for svc in SKOROWIDZ_SERVICES:
            self._leaf(g, svc.label, ("skorowidz", svc.label),
                       tr("Wyszukuje arkusze w zakresie; wybierasz je na mapie albo w tabeli i pobierasz"))

        g = self._group(tr("Zaawansowane"))
        self._leaf(g, tr("Usługi powiatowe (EZiUDP)"), ("eziudp", ""),
                   tr("Osnowa, EGiB, RCN, BDOT500 z powiatowych usług WFS"))

    # ------------------------------------------------------------------ zachowanie

    def _on_item_changed(self, item, _col) -> None:
        if self._updating or item.checkState(0) != Qt.CheckState.Checked:
            self._refresh()
            return
        kind = item.data(0, ROLE)[0]
        self._updating = True
        for other in self._items:
            if other is item or other.checkState(0) != Qt.CheckState.Checked:
                continue
            okind = other.data(0, ROLE)[0]
            # Warstwy WFS można łączyć ze sobą; skorowidz i EZiUDP wykluczają resztę
            if kind != "wfs" or okind != "wfs":
                other.setCheckState(0, Qt.CheckState.Unchecked)
        self._updating = False
        self._refresh()

    def _filter(self, text: str) -> None:
        text = text.strip().lower()

        def visit(item) -> bool:
            if item.childCount() == 0:
                hit = not text or text in item.text(0).lower() or \
                    text in str(item.data(0, ROLE)[1]).lower()
                item.setHidden(not hit)
                return hit
            any_hit = False
            for i in range(item.childCount()):
                any_hit = visit(item.child(i)) or any_hit
            item.setHidden(not any_hit)
            if text and any_hit:
                item.setExpanded(True)
            return any_hit

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def _refresh(self) -> None:
        sel = self.selection(with_years=False)
        svc = sel.skorowidz
        has_years = svc is not None and svc.year_range is not None
        if has_years and self.year_from.property("svc") != svc.label:
            newest, oldest = svc.year_range
            years = [str(y) for y in range(newest, oldest - 1, -1)]
            for combo in (self.year_from, self.year_to):
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(years)
                combo.blockSignals(False)
            self.year_from.setCurrentIndex(0)
            self.year_to.setCurrentIndex(0)
            self.year_from.setProperty("svc", svc.label)
        self.years_row.setVisible(has_years)
        self.summary.setText(tr("Wybrano: ") + sel.names() if not sel.empty
                             else tr("Zaznacz jedną lub kilka warstw."))
        self._save()
        self.changed.emit()

    def selection(self, with_years: bool = True) -> Selection:
        sel = Selection()
        for item in self._items:
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            kind, key = item.data(0, ROLE)
            if kind == "wfs":
                sel.sources.append(catalog.source_for_label(key))
            elif kind == "skorowidz":
                sel.skorowidz = SKOROWIDZ_BY_LABEL.get(key)
            else:
                sel.eziudp = True
        if with_years and sel.skorowidz is not None and sel.skorowidz.year_range is not None:
            a, b = int(self.year_from.currentText()), int(self.year_to.currentText())
            sel.years = [str(y) for y in range(max(a, b), min(a, b) - 1, -1)]
        return sel

    def select_labels(self, labels: list) -> None:
        """Zaznacza źródła o podanych etykietach z katalogu / skorowidzów (reszta odznaczona)."""
        self._updating = True
        for item in self._items:
            key = item.data(0, ROLE)[1] or EZIUDP
            on = key in labels
            item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            if on:
                self._reveal(item)
        self._updating = False
        self._refresh()

    @staticmethod
    def _reveal(item) -> None:
        p = item.parent()
        while p is not None:
            p.setExpanded(True)
            p = p.parent()

    def _save(self) -> None:
        keys = [item.data(0, ROLE)[1] or EZIUDP for item in self._items
                if item.checkState(0) == Qt.CheckState.Checked]
        QgsSettings().setValue(f"{SETTINGS}/sources", keys)

    def _restore(self) -> None:
        keys = QgsSettings().value(f"{SETTINGS}/sources", ["dzialki (EGIB)"])
        if isinstance(keys, str):
            keys = [keys]
        self._updating = True
        for item in self._items:
            key = item.data(0, ROLE)[1] or EZIUDP
            if key in (keys or []):
                item.setCheckState(0, Qt.CheckState.Checked)
                self._reveal(item)
        self._updating = False


# ====================================================================== krok 2

@dataclass
class Scope:
    kind: str                       # teryt | extent | rect | polygon | layer | ids | obreb
    teryt: str = ""
    precise: bool = False
    rect = None                     # QgsRectangle w układzie mapy (extent / rect)
    geom = None                     # QgsGeometry w układzie mapy (polygon)
    layer = None                    # QgsVectorLayer
    selected_only: bool = False
    ids: list = field(default_factory=list)
    obreb: str = ""
    nr: str = ""

    def describe(self) -> str:
        return {
            "teryt": f"TERYT {self.teryt}", "extent": "widok mapy", "rect": "narysowany prostokąt",
            "polygon": "narysowany wielokąt",
            "layer": f"warstwa {self.layer.name() if self.layer else ''}",
            "ids": f"{len(self.ids)} identyfikatorów", "obreb": f"obręb {self.obreb}, działka {self.nr}",
        }.get(self.kind, self.kind)

    def tag(self) -> str:
        """Krótki fragment nazwy warstwy, gdy każde pobranie trafia do osobnej warstwy."""
        return {
            "teryt": self.teryt, "extent": "widok", "rect": "prostokat", "polygon": "wielokat",
            "layer": self.layer.name() if self.layer else "warstwa", "ids": "ID",
            "obreb": f"{self.obreb}_{self.nr}",
        }.get(self.kind, self.kind)


class ScopePicker(QWidget):
    """Zakładki sposobów wyboru zakresu; w danej chwili działa jeden."""
    changed = pyqtSignal()
    draw_requested = pyqtSignal(str)   # "rect" | "polygon"
    show_unit_requested = pyqtSignal(str)
    submit = pyqtSignal()   # Enter w polu tekstowym – „pobierz, jeśli gotowe”

    TABS = ["teryt", "map", "layer", "ids", "obreb"]

    def __init__(self, units, parent=None):
        super().__init__(parent)
        self.units = units
        self._uldk_cache = {}     # gmina → (obręby, opis) – na czas sesji
        self._uldk_tasks = []
        self.drawn_rect = None
        self.drawn_polygon = None   # QgsGeometry w układzie mapy
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("scopeTabs")
        lay.addWidget(self.tabs)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setUsesScrollButtons(True)
        for widget, title, tip in (
                (self._tab_unit(), tr("Jednostka"), tr("Województwo, powiat, gmina albo obręb")),
                (self._tab_map(), tr("Mapa"), tr("Widok mapy albo narysowany prostokąt")),
                (self._tab_layer(), tr("Warstwa"), tr("Obiekty warstwy poligonowej")),
                (self._tab_ids(), tr("ID"), tr("Lista identyfikatorów działek lub budynków")),
                (self._tab_obreb(), tr("Obręb"), tr("Nazwa obrębu i numer działki"))):
            i = self.tabs.addTab(widget, title)
            self.tabs.setTabToolTip(i, tip)
        self.tabs.setCurrentIndex(QgsSettings().value(f"{SETTINGS}/scope_tab", 0, type=int))
        for edit in self.findChildren(QLineEdit):
            edit.returnPressed.connect(self.submit.emit)
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, i):
        QgsSettings().setValue(f"{SETTINGS}/scope_tab", i)
        self.changed.emit()

    # ------------------------------------------------------------------ zakładki

    def _tab_unit(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.teryt = TerytScopeWidget(with_obreby=True)
        self.teryt.gmina_of = lambda t: self.units.gmina_for(t.split(".")[0])
        self.teryt.inject_scope_data(self.units.wojewodztwa, self.units.powiaty,
                                     self.units.gminy, self.units.name_to_teryt)
        self.teryt.teryt_changed.connect(self._unit_changed)
        self.teryt.gmina_selected.connect(self._load_obreby)
        lay.addWidget(self.teryt)
        self.unit_hint = hint_label()
        self.unit_hint.setVisible(False)
        lay.addWidget(self.unit_hint)
        self.chk_precise = QCheckBox(tr("Dokładny kształt jednostki"))
        self.chk_precise.setToolTip(tr("Zapytanie z dokładną granicą zamiast prostokąta – "
                                       "mniej zbędnych obiektów, wolniej."))
        lay.addWidget(self.chk_precise)
        row = QHBoxLayout()
        self.btn_show_unit = QPushButton(tr("Pokaż na mapie"))
        self.btn_show_unit.setToolTip(tr("Pobiera granicę z PRG i podświetla ją na mapie."))
        self.btn_show_unit.clicked.connect(
            lambda: self.show_unit_requested.emit(self.teryt.get_resolved_teryt()))
        row.addWidget(self.btn_show_unit)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------------ obręby gminy

    def _load_obreby(self, gmina: str) -> None:
        """Najpierw lista ze słownika (od razu), potem aktualna z ULDK w tle."""
        if gmina in self._uldk_cache:
            self.teryt.set_obreby(gmina, *self._uldk_cache[gmina])
            return
        known = self.units.obreby_for_gmina(gmina)
        if not uldk.ENABLED:
            self.teryt.set_obreby(gmina, known, tr("Obręby ze słownika ") + self.units.date_text() + ".")
            return
        self.teryt.set_obreby(gmina, known, tr("Obrębów ze słownika: ") + str(len(known))
                              + tr(" – sprawdzam aktualną listę w ULDK…"))
        task = tasks.RegionsTask(gmina, self.units.jednostki_for_gmina(gmina))
        task.done.connect(self._uldk_done)
        self._uldk_tasks.append(task)
        QgsApplication.taskManager().addTask(task)

    def _uldk_done(self, gmina: str, obreby: list, errors: list) -> None:
        self._uldk_tasks = [t for t in self._uldk_tasks if t.key != gmina]
        if not obreby:
            known = self.units.obreby_for_gmina(gmina)
            note = (tr("ULDK nie odpowiada – lista ze słownika ") if errors
                    else tr("ULDK nie zwrócił obrębów – lista ze słownika ")) + self.units.date_text() + "."
            self.teryt.set_obreby(gmina, known, note)
            return
        old = {c for c, _ in self.units.obreby_for_gmina(gmina)}
        added = len({c for c, _ in obreby} - old)
        gone = len(old - {c for c, _ in obreby})
        self.units.add_obreby(obreby)
        note = tr("Obrębów: ") + str(len(obreby)) + tr(" – aktualna lista z ULDK")
        if added or gone:
            note += f" ({added} " + tr("nowych") + f", {gone} " + tr("nieaktualnych w słowniku") + ")"
        if errors:
            note += tr("; część jednostek bez odpowiedzi")
        self._uldk_cache[gmina] = (obreby, note + ".")
        self.teryt.set_obreby(gmina, obreby, note + ".")

    def cancel_tasks(self) -> None:
        for t in self._uldk_tasks:
            try:
                t.done.disconnect(self._uldk_done)
                t.cancel()
            except (TypeError, RuntimeError):
                pass
        self._uldk_tasks = []

    def _unit_changed(self, *_):
        t = normalize_teryt(self.teryt.get_resolved_teryt())
        level, note = self.units.check(t) if t else ("", "")
        text = note or (self.units.describe(t) if t else "")
        self.unit_hint.setText(text)
        set_state(self.unit_hint, "error" if t and not level else "warn" if note else "")
        self.unit_hint.setVisible(bool(text))
        self.changed.emit()

    def _tab_map(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.rb_extent = QRadioButton(tr("Bieżący widok mapy"))
        self.rb_rect = QRadioButton(tr("Prostokąt"))
        self.rb_polygon = QRadioButton(tr("Wielokąt (dowolny kształt)"))
        self.rb_extent.setChecked(True)
        grp = QButtonGroup(w)
        for rb in (self.rb_extent, self.rb_rect, self.rb_polygon):
            grp.addButton(rb)
            lay.addWidget(rb)
            rb.toggled.connect(self._map_mode_changed)
        row = QHBoxLayout()
        row.addSpacing(20)
        self.btn_draw = QPushButton(tr("Rysuj na mapie…"))
        self.btn_draw.clicked.connect(self._start_draw)
        row.addWidget(self.btn_draw)
        self.btn_clear_drawn = QPushButton(tr("Wyczyść"))
        self.btn_clear_drawn.setToolTip(tr("Usuwa narysowany prostokąt lub wielokąt z mapy"))
        self.btn_clear_drawn.clicked.connect(self.clear_drawn)
        row.addWidget(self.btn_clear_drawn)
        row.addStretch(1)
        lay.addLayout(row)
        self.map_hint = hint_label()
        lay.addWidget(self.map_hint)
        lay.addStretch(1)
        self._map_mode_changed()
        return w

    def map_mode(self) -> str:
        return "polygon" if self.rb_polygon.isChecked() else "rect" if self.rb_rect.isChecked() else "extent"

    def _map_mode_changed(self, *_):
        mode = self.map_mode()
        self.btn_draw.setEnabled(mode != "extent")
        drawn = self.drawn_rect if mode == "rect" else self.drawn_polygon if mode == "polygon" else None
        self.btn_clear_drawn.setEnabled(drawn is not None)
        if mode == "extent":
            self.map_hint.setText(tr("Zakres = to, co widać teraz na mapie."))
        elif drawn is None:
            self.map_hint.setText(tr("Kliknij „Rysuj na mapie…”."))
        self.changed.emit()

    def _start_draw(self):
        if self.map_mode() == "extent":
            self.rb_rect.setChecked(True)
        if self.map_mode() == "polygon":
            self.map_hint.setText(tr("Klikaj kolejne wierzchołki. Prawy przycisk albo dwuklik kończy, "
                                     "Backspace cofa punkt, Esc anuluje."))
        else:
            self.map_hint.setText(tr("Przeciągnij myszą na mapie, aby zaznaczyć prostokąt."))
        self.draw_requested.emit(self.map_mode())

    def set_drawn_rect(self, rect, area_m2: float = 0.0) -> None:
        self.drawn_rect = rect
        self.rb_rect.setChecked(True)
        self.map_hint.setText(tr("Zaznaczono prostokąt") + (f": {format_area(area_m2)}." if area_m2 else "."))
        self.btn_clear_drawn.setEnabled(True)
        self.changed.emit()

    def set_drawn_polygon(self, geom, area_m2: float = 0.0) -> None:
        self.drawn_polygon = geom
        self.rb_polygon.setChecked(True)
        n = max(0, geom.constGet().nCoordinates() - 1)
        self.map_hint.setText(tr("Narysowano wielokąt, wierzchołków: ") + str(n)
                              + (f" · {format_area(area_m2)}" if area_m2 else ""))
        self.btn_clear_drawn.setEnabled(True)
        self.changed.emit()

    def clear_drawn(self) -> None:
        """Kasuje narysowany kształt bieżącego trybu (podświetlenie znika samo)."""
        if self.map_mode() == "polygon":
            self.drawn_polygon = None
        else:
            self.drawn_rect = None
        self._map_mode_changed()

    def _emit_changed(self, *_) -> None:
        self.changed.emit()

    def draw_cancelled(self) -> None:
        self._map_mode_changed()

    def _tab_layer(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(tr("Warstwa poligonowa:")))
        self.cmb_layer = QgsMapLayerComboBox()
        self.cmb_layer.setFilters(LAYER_FILTER_POLYGON)
        self.cmb_layer.layerChanged.connect(self._layer_changed)
        # W QGIS 4 pierwsza warstwa dodana do projektu wybiera się na liście bez
        # sygnału layerChanged – pilnujemy też samej listy.
        model = self.cmb_layer.model()
        for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            sig.connect(self._layer_changed)
        lay.addWidget(self.cmb_layer)
        self.chk_selected = QCheckBox(tr("Tylko zaznaczone obiekty"))
        self.chk_selected.toggled.connect(self._layer_changed)
        lay.addWidget(self.chk_selected)
        self.layer_hint = hint_label()
        lay.addWidget(self.layer_hint)
        self._layer_changed()
        lay.addStretch(1)
        return w

    def refresh_layer_hint(self) -> None:
        """Ile obiektów (= zapytań) da wybrana warstwa; wołane też po zmianie zaznaczenia."""
        layer = self.cmb_layer.currentLayer()
        if layer is None:
            self.layer_hint.setText(tr("Zapytanie idzie osobno dla każdego obiektu warstwy."))
            return
        n = layer.selectedFeatureCount() if self.chk_selected.isChecked() else layer.featureCount()
        text = tr("Obiektów: ") + str(n) + tr(" – każdy to osobne zapytanie.")
        if n > 100:
            text += tr(" To dużo: rozważ połączenie obiektów albo zakres „Jednostka”.")
        self.layer_hint.setText(text)
        set_state(self.layer_hint, "warn" if n > 100 else "")

    def _layer_changed(self, *_) -> None:
        self.refresh_layer_hint()
        self.changed.emit()

    def _tab_ids(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.txt_ids = QPlainTextEdit()
        self.txt_ids.setPlaceholderText(tr("Jeden identyfikator w wierszu, np.\n141201_1.0001.123/4\n"
                                           "Można użyć * i ? jako wieloznaczników."))
        self.txt_ids.textChanged.connect(self._ids_changed)
        lay.addWidget(self.txt_ids)
        row = QHBoxLayout()
        self.ids_hint = hint_label()
        row.addWidget(self.ids_hint, 1)
        btn = QPushButton(tr("Wczytaj z pliku…"))
        btn.clicked.connect(self._load_ids)
        row.addWidget(btn)
        lay.addLayout(row)
        return w

    def _ids_changed(self, emit=True):
        n = len(self.ids())
        parts = [tr("Identyfikatorów: ") + str(n)] if n else []
        if getattr(self, "_id_fields", ""):
            parts.append(self._id_fields)
        self.ids_hint.setText(" · ".join(parts))
        if emit:
            self.changed.emit()

    def _load_ids(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Plik z identyfikatorami"), "",
                                              "TXT/CSV (*.txt *.csv);;*.*")
        if not path:
            return
        ids = []
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                first = line.replace(";", ",").split(",")[0].strip().strip('"')
                if first and first.lower() not in ("id", "id_dzialki", "identyfikator"):
                    ids.append(first)
        self.txt_ids.setPlainText("\n".join(ids))

    def set_id_fields(self, sources: list) -> None:
        """Podpowiedź w zakładce ID: w jakim polu szukane są identyfikatory wybranych danych."""
        names = sorted({f"{s.id_field} ({SourcePicker.display_name(s)})" for s in sources if s.id_field})
        self._id_fields = (tr("Szukane w polu: ") + ", ".join(names)) if names else ""
        self._ids_changed(emit=False)

    def ids(self) -> list:
        return [line.strip() for line in self.txt_ids.toPlainText().splitlines() if line.strip()]

    def _tab_obreb(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(tr("Nazwa albo kod obrębu:")))
        self.txt_obreb = QLineEdit()
        self.txt_obreb.setPlaceholderText(tr("np. Krzyki albo 026401_1.0012"))
        comp = QCompleter(sorted(self.units.obreby_by_name.keys()), self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        self.txt_obreb.setCompleter(comp)
        self.txt_obreb.textChanged.connect(self._obreb_changed)
        lay.addWidget(self.txt_obreb)
        lay.addWidget(QLabel(tr("Numer działki:")))
        self.txt_nr = QLineEdit()
        self.txt_nr.setPlaceholderText(tr("np. 123/4"))
        self.txt_nr.textChanged.connect(self._emit_changed)
        lay.addWidget(self.txt_nr)
        self.obreb_hint = hint_label()
        lay.addWidget(self.obreb_hint)
        lay.addStretch(1)
        return w

    def _obreb_changed(self, *_):
        name = self.txt_obreb.text().strip()
        found = self.units.find_obreby(name)
        if not name:
            text = ""
        elif not found:
            text = (tr("Nie znaleziono obrębu o tej nazwie w słowniku ") + self.units.date_text()
                    + tr(". Nowy obręb? Wpisz jego kod, np. 146501_1.0001."))
        elif found == [name] and name not in self.units.obreby_teryt_to_info:
            text = tr("Kodu obrębu nie ma w słowniku ") + self.units.date_text() + \
                tr(" – zostanie użyty bez sprawdzania.")
        elif found == [name]:
            text = self.units.describe(name)
        elif len(found) > 1:
            text = tr("Obrębów o tej nazwie: ") + f"{len(found)} – " + \
                tr("wybierzesz właściwy przy pobieraniu.")
        else:
            text = ""
        self.obreb_hint.setText(text)
        self.changed.emit()

    # ------------------------------------------------------------------ wynik

    def scope(self) -> Scope:
        tab = self.TABS[self.tabs.currentIndex()]
        if tab == "teryt":
            return Scope("teryt", teryt=normalize_teryt(self.teryt.get_resolved_teryt()),
                         precise=self.chk_precise.isChecked())
        if tab == "map":
            s = Scope(self.map_mode())
            s.rect = self.drawn_rect if s.kind == "rect" else None
            s.geom = self.drawn_polygon if s.kind == "polygon" else None
            return s
        if tab == "layer":
            s = Scope("layer", selected_only=self.chk_selected.isChecked())
            s.layer = self.cmb_layer.currentLayer()
            return s
        if tab == "ids":
            return Scope("ids", ids=self.ids())
        return Scope("obreb", obreb=self.txt_obreb.text().strip(), nr=self.txt_nr.text().strip())


# ====================================================================== krok 3

class OutputPicker(QWidget):
    """Gdzie zapisać wyniki, styl, metoda dla dużych zakresów, pola działek EGiB."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        s = QgsSettings()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.rb_memory = QRadioButton(tr("Warstwy tymczasowe"))
        self.rb_memory.setToolTip(tr("Znikają po zamknięciu projektu, chyba że je zapiszesz."))
        self.rb_gpkg = QRadioButton(tr("Plik GeoPackage:"))
        grp = QButtonGroup(self)
        grp.addButton(self.rb_memory)
        grp.addButton(self.rb_gpkg)
        lay.addWidget(self.rb_memory)
        row = QHBoxLayout()
        row.addWidget(self.rb_gpkg)
        self.fw_gpkg = QgsFileWidget()
        self.fw_gpkg.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.fw_gpkg.setFilter("GeoPackage (*.gpkg)")
        row.addWidget(self.fw_gpkg, 1)
        lay.addLayout(row)

        gpkg_mode = s.value(f"{SETTINGS}/output_mode", "memory", type=str) == "gpkg"
        self.fw_gpkg.setFilePath(s.value(f"{SETTINGS}/gpkg_path", "", type=str))
        (self.rb_gpkg if gpkg_mode else self.rb_memory).setChecked(True)

        self.chk_style = QCheckBox(tr("Wczytaj domyślny styl"))
        self.chk_style.setChecked(s.value(f"{SETTINGS}/load_style", True, type=bool))
        lay.addWidget(self.chk_style)
        prow = QHBoxLayout()
        prow.addWidget(QLabel(tr("Prefiks nazw:")))
        self.ed_prefix = QLineEdit()
        self.ed_prefix.setPlaceholderText(tr("np. ProjektA"))
        self.ed_prefix.setMinimumWidth(60)
        self.ed_prefix.setClearButtonEnabled(True)
        self.ed_prefix.setToolTip(tr("Warstwy dostaną nazwy z prefiksem (ProjektA_Dzialki) i trafią "
                                     "do podgrupy GUGiK/ProjektA. Kolejne pobrania z tym samym "
                                     "prefiksem dopisują się do tych warstw."))
        self.ed_prefix.setText(s.value(f"{SETTINGS}/layer_prefix", "", type=str))
        prow.addWidget(self.ed_prefix, 1)
        lay.addLayout(prow)
        self.prefix_hint = hint_label()
        lay.addWidget(self.prefix_hint)
        self.chk_separate = QCheckBox(tr("Każde pobranie w osobnej warstwie"))
        self.chk_separate.setToolTip(tr("Np. Dzialki_1465_1, Dzialki_1465_2 zamiast dopisywania "
                                        "kolejnych pobrań do jednej warstwy „Dzialki”."))
        self.chk_separate.setChecked(s.value(f"{SETTINGS}/separate_layers", False, type=bool))
        lay.addWidget(self.chk_separate)

        # Paczki opendata dla dużych zakresów (EGiB/RCN)
        self.package_row = QWidget()
        pr = QHBoxLayout(self.package_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(QLabel(tr("Cały powiat:")))
        self.cmb_package = QComboBox()
        self.cmb_package.addItem(tr("WFS do warstwy"), "wfs")
        self.cmb_package.addItem(tr("Paczka GPKG (szybciej)"), "package")
        self.cmb_package.setToolTip(tr("EGiB i RCN dla całego powiatu można pobrać jako gotową "
                                       "paczkę GeoPackage z opendata.geoportal.gov.pl"))
        self.cmb_package.setCurrentIndex(
            max(0, self.cmb_package.findData(s.value(f"{SETTINGS}/powiat_method", "wfs", type=str))))
        pr.addWidget(self.cmb_package, 1)
        self.package_row.setVisible(False)
        lay.addWidget(self.package_row)
        self.package_hint = hint_label()
        self.package_hint.setVisible(False)
        lay.addWidget(self.package_hint)

        # Pola działek EGiB
        self.btn_fields = QToolButton()
        self.btn_fields.setObjectName("link")
        self.btn_fields.setText(tr("Pola działek EGiB ▸"))
        self.btn_fields.setCheckable(True)
        lay.addWidget(self.btn_fields)
        self.list_attrs = QListWidget()
        self.list_attrs.setMaximumHeight(170)
        saved = s.value(f"{SETTINGS}/egib_fields", EGIB_DZIALKI_ATTRIBUTES)
        saved = [saved] if isinstance(saved, str) else (saved or [])
        for a in EGIB_DZIALKI_ATTRIBUTES:
            it = QListWidgetItem(a)
            it.setCheckState(Qt.CheckState.Checked if a in saved else Qt.CheckState.Unchecked)
            self.list_attrs.addItem(it)
        self.list_attrs.setVisible(False)
        lay.addWidget(self.list_attrs)
        self.btn_fields.toggled.connect(self._toggle_fields)

        for sig in (self.rb_gpkg.toggled, self.fw_gpkg.fileChanged, self.chk_style.toggled,
                    self.chk_separate.toggled, self.ed_prefix.textChanged,
                    self.cmb_package.currentIndexChanged, self.list_attrs.itemChanged):
            sig.connect(self._save)
        self._save()

    def _toggle_fields(self, on):
        self.list_attrs.setVisible(on)
        self.btn_fields.setText(tr("Pola działek EGiB ▾") if on else tr("Pola działek EGiB ▸"))

    def _save(self, *_):
        s = QgsSettings()
        path = self.gpkg_path()
        s.setValue(f"{SETTINGS}/output_mode", "gpkg" if self.rb_gpkg.isChecked() and path else "memory")
        s.setValue(f"{SETTINGS}/gpkg_path", path)
        s.setValue(f"{SETTINGS}/load_style", self.chk_style.isChecked())
        s.setValue(f"{SETTINGS}/separate_layers", self.chk_separate.isChecked())
        prefix = layers.clean_prefix(self.ed_prefix.text())
        s.setValue(f"{SETTINGS}/layer_prefix", prefix)
        self.prefix_hint.setText(tr("Np. ") + f"{prefix}_Dzialki" if prefix else "")
        self.prefix_hint.setVisible(bool(prefix))
        s.setValue(f"{SETTINGS}/powiat_method", self.cmb_package.currentData())
        s.setValue(f"{SETTINGS}/egib_fields", self.selected_attributes() or EGIB_DZIALKI_ATTRIBUTES)
        self.fw_gpkg.setEnabled(self.rb_gpkg.isChecked())
        self.changed.emit()

    def gpkg_path(self) -> str:
        path = self.fw_gpkg.filePath().strip()
        if path and not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        return path

    def output(self) -> layers.Output:
        return layers.Output(self.gpkg_path() if self.rb_gpkg.isChecked() else None,
                             self.chk_style.isChecked(), self.chk_separate.isChecked(),
                             self.ed_prefix.text())

    def selected_attributes(self) -> Optional[list]:
        """None = wszystkie pola (bez propertyName w zapytaniu)."""
        on = [self.list_attrs.item(i).text() for i in range(self.list_attrs.count())
              if self.list_attrs.item(i).checkState() == Qt.CheckState.Checked]
        return None if len(on) == self.list_attrs.count() else on

    def powiat_method(self) -> str:
        return self.cmb_package.currentData()

    def set_package_context(self, show_choice: bool, forced_text: str = "") -> None:
        """Pokazuje wybór metody dla powiatu albo informację o wymuszonej paczce."""
        self.package_row.setVisible(show_choice)
        self.package_hint.setVisible(bool(forced_text))
        self.package_hint.setText(forced_text)


# ====================================================================== zadania

class TaskRow(QFrame):
    ended = pyqtSignal()      # zadanie zakończone (sukces, błąd albo anulowanie)

    def __init__(self, title: str, task=None, parent=None):
        super().__init__(parent)
        self.setObjectName("taskRow")
        self.task = task
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 6, 2, 6)
        lay.setSpacing(4)
        top = QHBoxLayout()
        self.title = QLabel(title)
        self.title.setObjectName("taskTitle")
        self.title.setWordWrap(True)
        top.addWidget(self.title, 1)
        self.btn_cancel = QToolButton()
        self.btn_cancel.setObjectName("link")
        self.btn_cancel.setText(tr("Anuluj"))
        self.btn_cancel.clicked.connect(self._cancel)
        top.addWidget(self.btn_cancel)
        lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setObjectName("taskBar")
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100 if task is not None else 0)
        lay.addWidget(self.bar)
        self.count = hint_label("taskCount")
        self.count.setVisible(False)
        lay.addWidget(self.count)
        bottom = QHBoxLayout()
        self.result = hint_label("taskResult")
        self.result.setVisible(False)
        bottom.addWidget(self.result, 1)
        self.btn_retry = QToolButton()
        self.btn_retry.setObjectName("link")
        self.btn_retry.setText(tr("Ponów"))
        self.btn_retry.setToolTip(tr("Ponów tylko zapytania zakończone błędem"))
        self.btn_retry.setVisible(False)
        self.btn_retry.clicked.connect(self._retry)
        bottom.addWidget(self.btn_retry, 0, Qt.AlignmentFlag.AlignTop)
        self.btn_show = QToolButton()
        self.btn_show.setObjectName("link")
        self.btn_show.setText(tr("Pokaż"))
        self.btn_show.setToolTip(tr("Przybliż mapę do pobranych danych"))
        self.btn_show.setVisible(False)
        self.btn_show.clicked.connect(self._show)
        bottom.addWidget(self.btn_show, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(bottom)
        self._on_retry = None
        self._on_show = None
        self.running = True
        if task is not None:
            task.progressChanged.connect(lambda p: self.bar.setValue(int(p)))

    def attach(self, task) -> None:
        """Podmienia zadanie (np. po pobraniu granicy startuje właściwe pobieranie)."""
        self.task = task
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        task.progressChanged.connect(lambda p: self.bar.setValue(int(p)))

    def set_count(self, n: int) -> None:
        """Liczba pobranych obiektów w trakcie zadania."""
        if self.running:
            self.count.setText(tr("pobrano obiektów: ") + f"{n:,}".replace(",", " "))
            self.count.setVisible(True)

    def offer_retry(self, callback, text: str = "", tooltip: str = "") -> None:
        """Pokazuje „Ponów” (albo `text`) po zakończeniu; `callback()` uruchamia ponowienie."""
        self._on_retry = callback
        self.btn_retry.setText(text or tr("Ponów"))
        self.btn_retry.setToolTip(tooltip or tr("Ponów tylko zapytania zakończone błędem"))
        self.btn_retry.setVisible(True)

    def offer_show(self, callback) -> None:
        """Pokazuje „Pokaż” – `callback()` przybliża mapę do wyniku."""
        self._on_show = callback
        self.btn_show.setVisible(True)

    def _show(self) -> None:
        if self._on_show is not None:
            self._on_show()

    def _retry(self) -> None:
        self.btn_retry.setVisible(False)
        callback, self._on_retry = self._on_retry, None
        if callback is not None:
            callback()

    def _cancel(self):
        if self.task is not None:
            self.task.cancel()
        self.finish(tr("Anulowano."), ok=False)

    def finish(self, text: str, ok: bool = True) -> None:
        if not self.running:
            return
        self.running = False
        self.ended.emit()
        self.bar.setVisible(False)
        self.count.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.result.setText(("✓ " if ok else "⚠ ") + text)
        set_state(self.result, "ok" if ok else "warn")
        self.result.setVisible(True)


class TasksPanel(QWidget):
    """Lista zadań; nagłówek zwija listę (nowe zadanie zawsze ją rozwija)."""
    open_log = pyqtSignal()
    cancel_all_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.header = _Header()
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        head = QHBoxLayout(self.header)
        head.setContentsMargins(0, 0, 0, 0)
        self.arrow = QToolButton()
        self.arrow.setObjectName("stepArrow")
        self.arrow.setAutoRaise(True)
        self.arrow.clicked.connect(self.toggle)
        head.addWidget(self.arrow)
        self.title = QLabel(tr("Zadania"))
        self.title.setObjectName("stepTitle")
        head.addWidget(self.title)
        head.addStretch(1)
        btn_log = QToolButton()
        btn_log.setObjectName("link")
        btn_log.setText(tr("Dziennik"))
        btn_log.setToolTip(tr("Szczegóły w dzienniku komunikatów, zakładka PD_GUGiK"))
        btn_log.clicked.connect(self.open_log.emit)
        self.btn_cancel_all = QToolButton()
        self.btn_cancel_all.setObjectName("link")
        self.btn_cancel_all.setText(tr("Anuluj wszystkie"))
        self.btn_cancel_all.setToolTip(tr("Przerywa wszystkie trwające zadania i kolejkę plików"))
        self.btn_cancel_all.setVisible(False)
        self.btn_cancel_all.clicked.connect(self.cancel_all)
        head.addWidget(btn_log)
        btn_clear = QToolButton()
        btn_clear.setObjectName("link")
        btn_clear.setText(tr("Wyczyść"))
        btn_clear.clicked.connect(self.clear_finished)
        head.addWidget(btn_clear)
        self.header.clicked.connect(self.toggle)
        lay.addWidget(self.header)
        self.content = QWidget()
        body = QVBoxLayout(self.content)
        body.setContentsMargins(0, 0, 0, 0)
        # „Anuluj wszystkie” w osobnym wierszu – nagłówek mieści się w wąskim panelu
        row_all = QHBoxLayout()
        row_all.addStretch(1)
        row_all.addWidget(self.btn_cancel_all)
        body.addLayout(row_all)
        self.rows = QVBoxLayout()
        body.addLayout(self.rows)
        self.empty = hint_label()
        self.empty.setText(tr("Tu pojawi się postęp pobierania. Szczegóły zawsze w „Dzienniku”."))
        body.addWidget(self.empty)
        lay.addWidget(self.content)
        self.set_collapsed(QgsSettings().value(_collapsed_key("tasks"), False, type=bool), save=False)

    def is_collapsed(self) -> bool:
        return self.content.isHidden()

    def set_collapsed(self, collapsed: bool, save: bool = True) -> None:
        self.content.setVisible(not collapsed)
        self.arrow.setText("▸" if collapsed else "▾")
        self._update_title()
        if save:
            QgsSettings().setValue(_collapsed_key("tasks"), bool(collapsed))

    def toggle(self) -> None:
        self.set_collapsed(not self.is_collapsed())

    def _update_title(self) -> None:
        n = self.rows.count()
        self.title.setText(tr("Zadania") + (f" ({n})" if n else ""))
        self.btn_cancel_all.setVisible(len(self.running_rows()) >= 2)

    def running_rows(self) -> list:
        return [w for w in (self.rows.itemAt(i).widget() for i in range(self.rows.count()))
                if w is not None and w.running]

    def cancel_all(self) -> None:
        self.cancel_all_requested.emit()       # panel czyści kolejki (np. pliki skorowidza)
        for row in self.running_rows():
            row._cancel()
        self._update_title()

    def add(self, title: str, task=None) -> TaskRow:
        row = TaskRow(title, task)
        row.ended.connect(self._update_title)
        self.rows.insertWidget(0, row)
        self.empty.setVisible(False)
        if self.is_collapsed():
            self.set_collapsed(False, save=False)
        self._update_title()
        return row

    def clear_finished(self) -> None:
        for i in reversed(range(self.rows.count())):
            w = self.rows.itemAt(i).widget()
            if w is not None and not w.running:
                w.setParent(None)
        self.empty.setVisible(self.rows.count() == 0)
        self._update_title()
