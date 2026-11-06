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

from qgis.core import QgsSettings
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QCompleter, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QTabWidget, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import tr
from ..core import catalog, layers
from ..core.catalog import EGIB_DZIALKI_ATTRIBUTES
from ..qt_compat import LAYER_FILTER_POLYGON
from ..skorowidz_services import SKOROWIDZ_BY_LABEL, SKOROWIDZ_SERVICES
from ..teryt_scope_widget import TerytScopeWidget

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


class StepCard(QFrame):
    """Karta kroku: numer w kółku (szary → kolor, gdy krok kompletny), tytuł, skrót wyboru."""

    def __init__(self, number: int, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("stepCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.badge = QLabel(str(number))
        self.badge.setObjectName("stepBadge")
        head.addWidget(self.badge)
        self.title = QLabel(title)
        self.title.setObjectName("stepTitle")
        head.addWidget(self.title)
        head.addStretch(1)
        self.summary = QLabel()
        self.summary.setObjectName("stepSummary")
        head.addWidget(self.summary)
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.body)
        self.set_state("idle")

    def set_state(self, state: str, summary: str = "") -> None:
        """idle – jeszcze nie dotyczy, todo – czegoś brakuje, done – gotowe."""
        set_state(self, state)
        set_state(self.badge, state)
        self.badge.setText("✓" if state == "done" else self.badge.property("number") or self.badge.text())
        self.summary.setText(summary)


def step_box(number: int, title: str) -> tuple:
    card = StepCard(number, title)
    card.badge.setProperty("number", str(number))
    card.set_state("idle")
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
                       tr("Wyszukuje arkusze i pozwala je pobrać albo wczytać strumieniowo"))

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
    kind: str                       # teryt | extent | rect | layer | ids | obreb
    teryt: str = ""
    precise: bool = False
    rect = None                     # QgsRectangle w układzie mapy (extent / rect)
    layer = None                    # QgsVectorLayer
    selected_only: bool = False
    ids: list = field(default_factory=list)
    obreb: str = ""
    nr: str = ""

    def describe(self) -> str:
        return {
            "teryt": f"TERYT {self.teryt}", "extent": "widok mapy", "rect": "narysowany prostokąt",
            "layer": f"warstwa {self.layer.name() if self.layer else ''}",
            "ids": f"{len(self.ids)} identyfikatorów", "obreb": f"obręb {self.obreb}, działka {self.nr}",
        }.get(self.kind, self.kind)


class ScopePicker(QWidget):
    """Zakładki sposobów wyboru zakresu; w danej chwili działa jeden."""
    changed = pyqtSignal()
    draw_requested = pyqtSignal()
    show_unit_requested = pyqtSignal(str)

    TABS = ["teryt", "map", "layer", "ids", "obreb"]

    def __init__(self, units, parent=None):
        super().__init__(parent)
        self.units = units
        self.drawn_rect = None
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
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, i):
        QgsSettings().setValue(f"{SETTINGS}/scope_tab", i)
        self.changed.emit()

    # ------------------------------------------------------------------ zakładki

    def _tab_unit(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.teryt = TerytScopeWidget()
        self.teryt.inject_scope_data(self.units.wojewodztwa, self.units.powiaty,
                                     self.units.gminy, self.units.name_to_teryt)
        self.teryt.teryt_changed.connect(lambda *_: self._unit_changed())
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

    def _unit_changed(self):
        t = self.teryt.get_resolved_teryt()
        self.unit_hint.setText(self.units.describe(t) if t else "")
        self.unit_hint.setVisible(bool(self.unit_hint.text()))
        self.changed.emit()

    def _tab_map(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.rb_extent = QRadioButton(tr("Bieżący widok mapy"))
        self.rb_rect = QRadioButton(tr("Narysowany prostokąt"))
        self.rb_extent.setChecked(True)
        grp = QButtonGroup(w)
        grp.addButton(self.rb_extent)
        grp.addButton(self.rb_rect)
        lay.addWidget(self.rb_extent)
        lay.addWidget(self.rb_rect)
        row = QHBoxLayout()
        row.addSpacing(20)
        self.btn_draw = QPushButton(tr("Rysuj na mapie…"))
        self.btn_draw.clicked.connect(self._start_draw)
        row.addWidget(self.btn_draw)
        row.addStretch(1)
        lay.addLayout(row)
        self.map_hint = hint_label()
        lay.addWidget(self.map_hint)
        self.rb_extent.toggled.connect(lambda *_: self.changed.emit())
        lay.addStretch(1)
        return w

    def _start_draw(self):
        self.rb_rect.setChecked(True)
        self.map_hint.setText(tr("Przeciągnij myszą na mapie, aby zaznaczyć prostokąt."))
        self.draw_requested.emit()

    def set_drawn_rect(self, rect) -> None:
        self.drawn_rect = rect
        self.rb_rect.setChecked(True)
        self.map_hint.setText(tr("Zaznaczono prostokąt ") +
                              f"{rect.width():.0f} × {rect.height():.0f} " + tr("(jednostki mapy)."))
        self.changed.emit()

    def _tab_layer(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(tr("Warstwa poligonowa:")))
        self.cmb_layer = QgsMapLayerComboBox()
        self.cmb_layer.setFilters(LAYER_FILTER_POLYGON)
        self.cmb_layer.layerChanged.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.cmb_layer)
        self.chk_selected = QCheckBox(tr("Tylko zaznaczone obiekty"))
        self.chk_selected.toggled.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.chk_selected)
        lay.addWidget(hint_label())
        lay.itemAt(lay.count() - 1).widget().setText(
            tr("Zapytanie idzie osobno dla każdego obiektu warstwy."))
        lay.addStretch(1)
        return w

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

    def _ids_changed(self):
        n = len(self.ids())
        self.ids_hint.setText(tr("Identyfikatorów: ") + str(n) if n else "")
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

    def ids(self) -> list:
        return [l.strip() for l in self.txt_ids.toPlainText().splitlines() if l.strip()]

    def _tab_obreb(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(tr("Nazwa obrębu:")))
        self.txt_obreb = QLineEdit()
        comp = QCompleter(sorted(self.units.obreby_by_name.keys()), self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        self.txt_obreb.setCompleter(comp)
        self.txt_obreb.textChanged.connect(lambda *_: self._obreb_changed())
        lay.addWidget(self.txt_obreb)
        lay.addWidget(QLabel(tr("Numer działki:")))
        self.txt_nr = QLineEdit()
        self.txt_nr.setPlaceholderText(tr("np. 123/4"))
        self.txt_nr.textChanged.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.txt_nr)
        self.obreb_hint = hint_label()
        lay.addWidget(self.obreb_hint)
        lay.addStretch(1)
        return w

    def _obreb_changed(self):
        n = len(self.units.find_obreby(self.txt_obreb.text()))
        name = self.txt_obreb.text().strip()
        self.obreb_hint.setText("" if not name else (
            tr("Nie znaleziono obrębu o tej nazwie.") if n == 0 else
            tr("Obrębów o tej nazwie: ") + f"{n} – " + tr("wybierzesz właściwy przy pobieraniu.")
            if n > 1 else ""))
        self.changed.emit()

    # ------------------------------------------------------------------ wynik

    def scope(self) -> Scope:
        tab = self.TABS[self.tabs.currentIndex()]
        if tab == "teryt":
            return Scope("teryt", teryt=self.teryt.get_resolved_teryt(),
                         precise=self.chk_precise.isChecked())
        if tab == "map":
            s = Scope("rect" if self.rb_rect.isChecked() else "extent")
            s.rect = self.drawn_rect if self.rb_rect.isChecked() else None
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
                             self.chk_style.isChecked())

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
        self.result = hint_label("taskResult")
        self.result.setVisible(False)
        lay.addWidget(self.result)
        self.running = True
        if task is not None:
            task.progressChanged.connect(lambda p: self.bar.setValue(int(p)))

    def attach(self, task) -> None:
        """Podmienia zadanie (np. po pobraniu granicy startuje właściwe pobieranie)."""
        self.task = task
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        task.progressChanged.connect(lambda p: self.bar.setValue(int(p)))

    def _cancel(self):
        if self.task is not None:
            self.task.cancel()
        self.finish(tr("Anulowano."), ok=False)

    def finish(self, text: str, ok: bool = True) -> None:
        if not self.running:
            return
        self.running = False
        self.bar.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.result.setText(("✓ " if ok else "⚠ ") + text)
        set_state(self.result, "ok" if ok else "warn")
        self.result.setVisible(True)


class TasksPanel(QWidget):
    open_log = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        lbl = QLabel(tr("Zadania"))
        lbl.setObjectName("stepTitle")
        head.addWidget(lbl)
        head.addStretch(1)
        btn_log = QToolButton()
        btn_log.setObjectName("link")
        btn_log.setText(tr("Dziennik"))
        btn_log.setToolTip(tr("Szczegóły w dzienniku komunikatów, zakładka PD_GUGiK"))
        btn_log.clicked.connect(self.open_log.emit)
        head.addWidget(btn_log)
        btn_clear = QToolButton()
        btn_clear.setObjectName("link")
        btn_clear.setText(tr("Wyczyść"))
        btn_clear.clicked.connect(self.clear_finished)
        head.addWidget(btn_clear)
        lay.addLayout(head)
        self.rows = QVBoxLayout()
        lay.addLayout(self.rows)
        self.empty = hint_label()
        self.empty.setText(tr("Brak zadań."))
        lay.addWidget(self.empty)

    def add(self, title: str, task=None) -> TaskRow:
        row = TaskRow(title, task)
        self.rows.insertWidget(0, row)
        self.empty.setVisible(False)
        return row

    def clear_finished(self) -> None:
        for i in reversed(range(self.rows.count())):
            w = self.rows.itemAt(i).widget()
            if w is not None and not w.running:
                w.setParent(None)
        self.empty.setVisible(self.rows.count() == 0)
