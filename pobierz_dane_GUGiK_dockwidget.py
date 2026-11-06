# -*- coding: utf-8 -*-

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtWidgets import QMessageBox, QCompleter, QListWidgetItem, QFileDialog
from qgis.core import (
    QgsProject, QgsGeometry, QgsCoordinateTransform,
    QgsCoordinateReferenceSystem, QgsApplication, QgsMessageLog, Qgis,
    QgsRasterLayer, QgsPointCloudLayer, QgsRectangle
)
from qgis.PyQt.QtGui import QColor
from .qt_compat import GEOM_POLYGON, LAYER_VECTOR
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsMapLayerComboBox

import os
import json
import re

from .core import catalog, filters, gml, layers, log, tasks
from .prg_client import PRGClient

from .wfs_index_dialog import WfsIndexResultsDialog
from .wfs_index_client import WFSIndexClient
from .skorowidz_services import SKOROWIDZ_SERVICES, SKOROWIDZ_BY_LABEL

from .eziudp_download_dialog import EziudpWidget


FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'pobierz_dane_GUGiK_dockwidget_base.ui'))

class RectangleMapTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        self.rubberBand = QgsRubberBand(self.canvas, GEOM_POLYGON)
        # self.rubberBand.setColor(Qt.red)
        self.rubberBand.setFillColor(QColor(255, 255, 0, 80))   # półprzezroczyste wypełnienie
        self.rubberBand.setColor(QColor(255, 200, 0, 200))      # obwódka
        self.rubberBand.setWidth(2)
        self.startPoint = None
        self.isEmittingPoint = False
       

    def canvasPressEvent(self, e):
        self.startPoint = self.toMapCoordinates(e.pos())
        self.rubberBand.reset(GEOM_POLYGON)
        self.isEmittingPoint = True

    def canvasMoveEvent(self, e):
        if not self.isEmittingPoint:
            return

        currentPoint = self.toMapCoordinates(e.pos())
        self.rubberBand.reset(GEOM_POLYGON)

        # Create normalized rectangle (min x,y to max x,y)
        xmin = min(self.startPoint.x(), currentPoint.x())
        ymin = min(self.startPoint.y(), currentPoint.y())
        xmax = max(self.startPoint.x(), currentPoint.x())
        ymax = max(self.startPoint.y(), currentPoint.y())

        rect = QgsRectangle(xmin, ymin, xmax, ymax)
        self.rubberBand.addGeometry(QgsGeometry.fromRect(rect), None)

    def canvasReleaseEvent(self, e):
        self.isEmittingPoint = False
        endPoint = self.toMapCoordinates(e.pos())

        # Create normalized rectangle (min x,y to max x,y)
        xmin = min(self.startPoint.x(), endPoint.x())
        ymin = min(self.startPoint.y(), endPoint.y())
        xmax = max(self.startPoint.x(), endPoint.x())
        ymax = max(self.startPoint.y(), endPoint.y())

        rect = QgsRectangle(xmin, ymin, xmax, ymax)
        self.callback(rect)
        self.rubberBand.reset(GEOM_POLYGON)


class PD_GUGiKDockWidget(QtWidgets.QDockWidget, FORM_CLASS):

    closingPlugin = pyqtSignal()

    def __init__(self, parent=None):
        """Constructor."""
        super(PD_GUGiKDockWidget, self).__init__(parent)
        self.setupUi(self)

        # Data Cache
        self.wojewodztwa = {}
        self.powiaty = {}
        self.gminy = {}
        self.obreby = {}

        # Init
        self.prg_client = PRGClient()
        self.load_data()
        self.connect_signals()
        self.download_task = None
        self.running_tasks = []

        # Add checkbox for precise spatial filtering to admin tab
        if hasattr(self, 'tab_admin'):
            self.chk_precise_spatial = QtWidgets.QCheckBox("Dokładne filtrowanie przestrzenne")
            self.chk_precise_spatial.setToolTip("Jeśli zaznaczone, używa dokładnej geometrii zamiast prostokąta otaczającego (wolniejsze, ale bardziej precyzyjne).")
            
            # Add button for PRG geometry download
            self.btn_download_prg_geom = QtWidgets.QPushButton("Pobierz granicę z PRG")
            self.btn_download_prg_geom.setToolTip("Pobierz tylko geometrię obszaru administracyjnego z PRG (nie pobiera działek).")
            self.btn_download_prg_geom.clicked.connect(self.run_prg_geometry_download)

            # Insert before download button
            layout = self.tab_admin.layout()
            # Assuming 'Pobierz' button is at the end or near the end.
            # chk_precise_spatial was inserted at count-2.
            # Let's insert this button there too (pushing download button further down?)
            # Or insert it before precise check?
            # Layout order usually: Inputs, Spacer, Buttons.
            # Let's add it before chk_precise_spatial or after.
            
            # Current structure seems to be VBox.
            # We want: [Precise Check], [Download PRG Boundary], [Download Parcels]
            
            layout.insertWidget(layout.count() - 2, self.chk_precise_spatial)
            layout.insertWidget(layout.count() - 1, self.btn_download_prg_geom)

            # self._patch_init_eziudp_button()


        # Add new tab for precise search (obreb + dzialka nr)
        if hasattr(self, 'tabWidget'):
            from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFormLayout

            self.btn_search_obreb_nr.clicked.connect(self.run_precise_search)

        # Add layer selection and download button to map tab
        if hasattr(self, 'tab_map'):
            from qgis.PyQt.QtWidgets import QFormLayout, QLabel
            map_layout = self.tab_map.layout()

            # Create form layout for layer selection
            form_layout = QFormLayout()

            self.lbl_layer = QLabel("Warstwa poligonowa:")
            self.cmb_layer = QgsMapLayerComboBox()

            form_layout.addRow(self.lbl_layer, self.cmb_layer)

            # Create download button
            self.btn_download_layer = QtWidgets.QPushButton("Pobierz przez warstwę poligonową")
            self.btn_download_layer.setToolTip("Pobierz działki dla wszystkich geometrii w wybranej warstwie.")

            # Insert widgets before spacer
            form_widget = QtWidgets.QWidget()
            form_widget.setLayout(form_layout)
            map_layout.insertWidget(1, form_widget)
            map_layout.insertWidget(2, self.btn_download_layer)

            # Connect signal
            self.btn_download_layer.clicked.connect(self.run_layer_download)

        self.map_tool = None

        # Wypełnij cmbObjType ze statycznych wpisów EGiB/RCN + dynamicznie z rejestru usług skorowidzowych
        self._populate_obj_type_combo()
        self._init_eziudp_tab()
        self._init_category_filter()

        # Dynamiczny combobox roku – widoczny tylko dla usług skorowidzowych z year_range
        # (cmb_orto_rok / cmb_nmt_rok z .ui są zastępowane jedną parą sterowaną z kodu)
        self._init_year_combos()

        from qgis.utils import iface
        self.iface = iface
        self.canvas = self.iface.mapCanvas()


        # Initialize Settings and Attributes
        self.init_settings_ui()
        self.setup_completer()

    def _patch_init_eziudp_button(self):
        """
        Fragment do wklejenia w __init__(), np. na końcu bloku tab_admin.
    
        Tworzy przycisk "Pobierz dane powiatowe (eziudp)" i wstawia go
        do layoutu zakładki administracyjnej.
        Jeśli wolisz zdefiniować go w Qt Designerze – po prostu nazwij go
        'btn_eziudp' i pomiń ten blok.
        """
        if hasattr(self, "tab_admin"):
            self.btn_eziudp = QtWidgets.QPushButton(
                "Pobierz dane powiatowe (EZiUDP) …"
            )
            self.btn_eziudp.setToolTip(
                "Otwiera okno pobierania danych z powiatowych usług WFS\n"
                "przez serwis integracja.gugik.gov.pl (BDSOG, EGIB, RCN)."
            )
            layout = self.tab_admin.layout()
            # Wstaw przed ostatnim widgetem (zwykle spacer lub btn_download_admin)
            layout.insertWidget(layout.count() - 1, self.btn_eziudp)
            # Podpięcie sygnału – connect_signals() uruchomił się wcześniej,
            # więc podpinamy tutaj bezpośrednio

    def _populate_obj_type_combo(self):
        """
        Wypełnia cmbObjType: najpierw stałe typy EGiB/RCN,
        potem wszystkie usługi skorowidzowe z rejestru.
        Dzięki temu dodanie nowej usługi do SKOROWIDZ_SERVICES
        automatycznie pojawia się w UI.
        """
        # Zachowaj istniejące wpisy EGiB/RCN jeśli combo już ma elementy
        # (setupUi mógł wypełnić je z .ui), w przeciwnym razie dodaj ręcznie
        if self.cmbObjType.count() == 0:
            for label in ["dzialki (EGIB)", "budynki (EGIB)",
                          "dzialki (RCN)", "budynki (RCN)", "lokale (RCN)", "A07_Punkty_adresowe (PRG)", "A08_Ulice (PRG)"]:
                self.cmbObjType.addItem(label)

        # Usuń stare wpisy skorowidzowe (gdyby były w .ui) – zachowaj tylko EGiB/RCN
        egib_rcn_count = self.cmbObjType.count()

        # Dodaj wszystkie usługi z rejestru
        for svc in SKOROWIDZ_SERVICES:
            self.cmbObjType.addItem(svc.label)

        self.cmbObjType.addItem("Usługi powiatowe (EZiUDP)")

    def _init_year_combos(self):
        """
        Tworzy dynamiczną parę comboboxów roku (od/do) sterowaną przez rejestr.
        Zastępuje hardkodowane cmb_orto_rok / cmb_nmt_rok jeśli istnieją w .ui,
        lub tworzy nowe widgety programowo gdy ich nie ma.
        """


        self.cmb_skorowidz_rok.setVisible(False)
        self.cmb_skorowidz_rok_do.setVisible(False)
        self.lbl_skorowidz_date.setVisible(False)

        # Podpięcie sygnału rok_do – robimy to tu, po tym jak widget na pewno istnieje,
        # żeby uniknąć TypeError przy disconnect w connect_signals.
        # self.cmb_skorowidz_rok_do.currentIndexChanged.connect(self.update_ui_from_type)

    def get_gmina_name(self, teryt):
        """Pobierz nazwę gminy z kodu TERYT."""
        if teryt[:8] in self.gminy:
            return self.gminy[teryt[:8]]['nazwa']
        elif teryt[:6] in self.gminy:
            return self.gminy[teryt[:6]]['nazwa']
        return None

    def get_powiat_name(self, teryt):
        """Pobierz nazwę powiatu z kodu TERYT."""
        if teryt[:4] in self.powiaty:
            return self.powiaty[teryt[:4]]['nazwa']
        return None

    def init_settings_ui(self):
        """Initialize settings tab and load attributes list."""
        self.attributes_all = [
            'id_dzialki', 'numer_dzialki', 'numer_obrebu', 'numer_jednostki', 'nazwa_obrebu', 'nazwa_gminy',
            'pole_powierzchni', 'grupa_rejestrowa', 'data', 'klasouzytki_egib'
        ]
        for attr in self.attributes_all:
            item = QListWidgetItem(attr)
            item.setCheckState(Qt.CheckState.Checked)
            self.list_attributes.addItem(item)
            
    def setup_completer(self):
        """Setup QCompleter for TERYT search by name."""
        self.name_to_teryt = {}
        # Collect all names from loaded data
        for t, data in self.wojewodztwa.items():
            # self.name_to_teryt[data['nazwa']] = t
            self.name_to_teryt[f"{data['nazwa']} (województwo {t})"] = t
        for t, data in self.powiaty.items():
            # self.name_to_teryt[f"{data['nazwa']} (powiat)"] = t
            self.name_to_teryt[f"{data['nazwa']} (powiat {t})"] = t
        for t, data in self.gminy.items():
            # self.name_to_teryt[f"{data['nazwa']} (gmina)"] = t
            self.name_to_teryt[f"{data['nazwa']} (gmina {t})"] = t
        if hasattr(self, 'obreby_teryt_to_info'):
            for t, data in self.obreby_teryt_to_info.items():
                self.name_to_teryt[f"{data['nazwa']} (obręb {t})"] = t

        self.completer = QCompleter(self.name_to_teryt.keys(), self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.txt_teryt_manual.setCompleter(self.completer)
        
        self.completer.activated.connect(self.on_completer_activated)

    def on_completer_activated(self, text):
        
        # Try to find TERYT in the selected text (e.g. from "Brzuchania (obręb 120805_5.0002)")
        # match = re.search(r'\(obręb (.*?)\)', text)
        match = re.search(r'^.+\.([\d:4]+)', text)
        if match:
            teryt = match.group()
        elif text in self.name_to_teryt:
            teryt = self.name_to_teryt[text]
        else:
            teryt = text

        
            
        self.txt_teryt_manual.setText(teryt)
        self.sync_combos_from_teryt(teryt)

        self.lbl_teryt_info.setText(teryt)

    def sync_combos_from_teryt(self, teryt):
        """Update ComboBoxes based on given TERYT code."""
        if len(teryt) >= 2:
            idx = self.cmb_woj.findData(teryt[:2])
            if idx >= 0: self.cmb_woj.setCurrentIndex(idx)
        if len(teryt) >= 4:
            idx = self.cmb_pow.findData(teryt[:4])
            if idx >= 0: self.cmb_pow.setCurrentIndex(idx)
        if len(teryt) >= 6:
            # Try to find exactly or prefix
            t6 = teryt[:7] if len(teryt) >= 7 else teryt
            idx = self.cmb_gmina.findData(t6)
            if idx < 0 and len(teryt) >= 8:
                idx = self.cmb_gmina.findData(teryt[:8])
            if idx >= 0: self.cmb_gmina.setCurrentIndex(idx)
        if "." in teryt:
            idx = self.cmb_obreb.findData(teryt)
            if idx >= 0: self.cmb_obreb.setCurrentIndex(idx)

    def validate_teryt(self):
        text = self.txt_teryt_manual.text().strip()
        text = text.split(" ")[-1].replace(")","") if " " in text else text
        if not text:
            self.lbl_teryt_info.setText("")
            return

        is_valid = False
        if text in self.wojewodztwa or text in self.powiaty or text in self.gminy:
            is_valid = True
        elif hasattr(self, 'obreby_teryt_to_info') and text in self.obreby_teryt_to_info:
            is_valid = True
        elif '*' in text or '?' in text:
            is_valid = True
        elif len(text) in [2, 4, 7, 8]:
            is_valid = True
        elif "." in text:
            is_valid = True

        if not is_valid:
            self.lbl_teryt_info.setText("Teryt może być niewłaściwy!")
        else:
            self.lbl_teryt_info.setText("")
            # Avoid recursion if just selected from completer
            if not self.completer.popup().isVisible():
                 # Don't sync on every keystroke to avoid perf issues, maybe only on certain length
                 if len(text) in [2, 4, 7, 8] or "." in text:
                     self.sync_combos_from_teryt(text)

    def load_data(self):
        """Load JSON/GeoJSON files into memory."""
        plugin_dir = os.path.dirname(__file__)
        data_dir = os.path.join(plugin_dir, 'data')

        from qgis.core import QgsMessageLog, Qgis

        def get_val(props, keys):
            # Case-insensitive lookup
            props_lower = {k.lower(): v for k, v in props.items()}
            for k in keys:
                if k.lower() in props_lower:
                    return props_lower[k.lower()]
            return None

        try:
            # Ładowanie województw
            path = os.path.join(data_dir, 'wojewodztwa.geojson')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        name = get_val(props, ['nazwa', 'name', 'voivodeship'])
                        teryt = get_val(props, ['teryt', 'id', 'kod'])
                        geom_json = feat.get('geometry')

                        if name and teryt:
                            teryt_str = str(teryt)
                            geom = None
                            if geom_json:
                                geom = QgsGeometry.fromJson(json.dumps(geom_json))

                            self.wojewodztwa[teryt_str] = {
                                'nazwa': name,
                                'geom': geom
                            }
                            count += 1

                    self.cmb_woj.addItem("- Wybierz -", None)
                    self.cmb_woj.addItem("Cała Polska (0)", "")
                    for teryt_str, w in sorted(self.wojewodztwa.items(), key=lambda x: x[1]['nazwa']):
                        self.cmb_woj.addItem(f"{w['nazwa']} ({teryt_str})", teryt_str)
                    QgsMessageLog.logMessage(f"Załadowano {count} województw.", "PD_GUGiK", Qgis.MessageLevel.Info)

            # Ładowanie powiatów
            path = os.path.join(data_dir, 'powiaty.geojson')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        name = get_val(props, ['nazwa', 'name', 'powiat'])
                        teryt = get_val(props, ['teryt', 'id', 'kod'])
                        geom_json = feat.get('geometry')

                        if name and teryt:
                            teryt_str = str(teryt)
                            geom = None
                            if geom_json:
                                geom = QgsGeometry.fromJson(json.dumps(geom_json))

                            # Parent is first 2 chars of TERYT (Województwo)
                            self.powiaty[teryt_str] = {
                                'nazwa': name,
                                'parent': teryt_str[:2],
                                'geom': geom
                            }
                            count += 1
                    QgsMessageLog.logMessage(f"Załadowano {count} powiatów.", "PD_GUGiK", Qgis.MessageLevel.Info)

            # Ładowanie gmin
            path = os.path.join(data_dir, 'gminy.geojson')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        name = get_val(props, ['nazwa', 'name', 'gmina'])
                        teryt = get_val(props, ['teryt', 'id', 'kod'])
                        geom_json = feat.get('geometry')

                        if name and teryt:
                            teryt_str = str(teryt)
                            geom = None
                            if geom_json:
                                geom = QgsGeometry.fromJson(json.dumps(geom_json))

                            # Parent is first 4 chars of TERYT (Powiat)
                            self.gminy[teryt_str] = {
                                'nazwa': name,
                                'parent': teryt_str[:4],
                                'geom': geom
                            }
                            count += 1
                    QgsMessageLog.logMessage(f"Załadowano {count} gmin.", "PD_GUGiK", Qgis.MessageLevel.Info)
            
            # Ładowanie obrębów - zapisz jako słownik według kodu TERYT (pełnego)
            obreb_path = os.path.join(data_dir, 'obreby.geojson')
            if os.path.exists(obreb_path):
                with open(obreb_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.obreby_by_gmina = {}
                    self.obreby_by_name = {}
                    self.obreby_teryt_to_info = {}
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        teryt = get_val(props, ['TERYT', 'teryt', 'id', 'kod'])
                        nazwa = get_val(props, ['Nazwa', 'nazwa', 'name'])

                        if teryt and nazwa:
                            teryt_str = str(teryt)
                            # Format TERYT obrębu: WWPPGG_R.OOOO (np. 141201_1.0001)
                            # Kod gminy to część przed kropką (WWPPGG_R)
                            if '_' in teryt_str:
                                gmina_code = teryt_str.split('_')[0]
                               
                            else:
                                # Fallback: take first 8 chars if matches pattern
                                gmina_code = teryt_str[:6]

                            if gmina_code not in self.obreby_by_gmina:
                                self.obreby_by_gmina[gmina_code] = []
                            self.obreby_by_gmina[gmina_code].append((nazwa, teryt_str))

                            # Mapowanie nazwy obrębu do listy TERYT
                            if nazwa not in self.obreby_by_name:
                                self.obreby_by_name[nazwa] = []
                            self.obreby_by_name[nazwa].append(teryt_str)

                            # Mapowanie TERYT obrębu do info
                            self.obreby_teryt_to_info[teryt_str] = {
                                'nazwa': nazwa,
                                'gmina_code': gmina_code,
                                'powiat_code': teryt_str[:4]
                            }
                            count += 1
                    QgsMessageLog.logMessage(f"Załadowano {count} obrębów.", "PD_GUGiK", Qgis.MessageLevel.Info)
            else:
                self.obreby_by_gmina = {}
                self.obreby_by_name = {}
                self.obreby_teryt_to_info = {}

        except Exception as e:
            msg = f"Błąd ładowania danych: {e}"
            QgsMessageLog.logMessage(msg, "PD_GUGiK", Qgis.MessageLevel.Critical)
            self.show_error(msg)

    def connect_signals(self):
        self.cmb_woj.currentIndexChanged.connect(self.update_teryt_from_woj)
        self.cmb_pow.currentIndexChanged.connect(self.update_teryt_from_pow)
        self.cmb_gmina.currentIndexChanged.connect(self.update_teryt_from_gmina)
        self.cmb_obreb.currentIndexChanged.connect(self.update_teryt_from_obreb)
        self.cmbObjType.currentIndexChanged.connect(self.update_ui_from_type)
        # Sygnał cmb_skorowidz_rok_do podpinany w _init_year_combos po utworzeniu widgetu
        self.cmbCategory.currentIndexChanged.connect(self._apply_category_filter)

        self.btn_download_admin.clicked.connect(self.run_admin_download)
        self.btn_download_ids.clicked.connect(self.run_id_download)

        self.btn_select_rect.clicked.connect(self.toggle_map_tool)
        self.btn_download_extent.clicked.connect(self.run_extent_download)

        self.btn_cancel.clicked.connect(self.cancel_download)
        self.txt_teryt_manual.textChanged.connect(self.validate_teryt)


    def _current_skorowidz_service(self):
        """
        Zwraca SkorowidzService dla aktualnie wybranego typu obiektu,
        lub None jeśli to nie jest usługa skorowidzowa.
        """
        return SKOROWIDZ_BY_LABEL.get(self.cmbObjType.currentText())

    def _update_year_combo(self, svc):
        """
        Wypełnia cmb_skorowidz_rok / cmb_skorowidz_rok_do zakresem lat
        zdefiniowanym w SkorowidzService. Ukrywa combo gdy year_range=None.
        """
        has_years = svc is not None and svc.year_range is not None
        self.cmb_skorowidz_rok.setVisible(has_years)
        self.cmb_skorowidz_rok_do.setVisible(has_years)
        self.lbl_skorowidz_date.setVisible(has_years)

        if not has_years:
            return

        max_rok, min_rok = svc.year_range
        lata = ["Wszystkie"] + [str(r) for r in range(max_rok, min_rok - 1, -1)]
        lata_do = ["- Brak -"] + [str(r) for r in range(max_rok, min_rok - 1, -1)]

        self.cmb_skorowidz_rok.blockSignals(True)
        self.cmb_skorowidz_rok_do.blockSignals(True)
        self.cmb_skorowidz_rok.clear()
        self.cmb_skorowidz_rok_do.clear()
        self.cmb_skorowidz_rok.addItems(lata)
        self.cmb_skorowidz_rok_do.addItems(lata_do)
        self.cmb_skorowidz_rok.blockSignals(False)
        self.cmb_skorowidz_rok_do.blockSignals(False)

    def _get_years_from_combos(self, cmb_from, cmb_to) -> list:
        """
        Zwraca listę lat (jako stringi) wybranych w dwóch comboboxach rok_od/rok_do.
        """
        rok_od = cmb_from.currentText()
        rok_do = cmb_to.currentText()

        if rok_od == "Wszystkie":
            return [cmb_from.itemText(i) for i in range(1, cmb_from.count())]

        rok_od_int = int(rok_od)
        if rok_do and rok_do != "- Brak -":
            rok_do_int = int(rok_do)
            min_rok = min(rok_od_int, rok_do_int)
            max_rok = max(rok_od_int, rok_do_int)
            return [str(r) for r in range(max_rok, min_rok - 1, -1)]
        return [rok_od]

    def _run_skorowidz(self, geom, svc=None):
        """
        Uruchamia wyszukiwanie w skorowidzu dla podanej geometrii.
        Pobiera konfigurację usługi z rejestru SKOROWIDZ_SERVICES.

        :param geom: QgsGeometry obszaru wyszukiwania
        :param svc:  SkorowidzService – jeśli None, pobierany z aktualnego cmbObjType
        """
        if svc is None:
            svc = self._current_skorowidz_service()
        if svc is None:
            return

        if svc.year_range is None:
            # Usługa bez podziału rocznikowego – pobieramy całą warstwę
            self.search_and_show_wfs_index(geom, svc.url, svc.layer_name)
        else:
            lata = self._get_years_from_combos(
                self.cmb_skorowidz_rok, self.cmb_skorowidz_rok_do
            )
            if not lata:
                self.show_error("Brak poprawnych lat do wyszukania.")
                return
            if len(lata) == 1:
                self.search_and_show_wfs_index(
                    geom, svc.url, f"{svc.layer_name}{lata[0]}{svc.layer_suffix}"
                )
            else:
                self.search_and_show_multiple_wfs_indices(
                    geom, svc.url, lata, svc.layer_name, svc.layer_suffix
                )

    def update_teryt_from_woj(self):
        woj_id = self.cmb_woj.currentData()
        self.cmb_pow.clear()
        self.cmb_pow.setEnabled(True)
        self.cmb_pow.addItem("- Wybierz -", None)


        if woj_id:
            self.txt_teryt_manual.setText(str(woj_id))

            self.cmb_pow.addItem("Brak", None)
            for teryt, p in sorted(self.powiaty.items(), key=lambda x: x[1]['nazwa']):
                if p['parent'] == woj_id:
                    self.cmb_pow.addItem(f"{p['nazwa']} ({teryt})", teryt)
        elif woj_id == "":
            self.txt_teryt_manual.setText("0")
        else:
            self.txt_teryt_manual.clear()

    def update_teryt_from_pow(self):
        pow_id = self.cmb_pow.currentData()
        self.cmb_gmina.clear()
        if not pow_id:
            self.cmb_gmina.setEnabled(False)

            if self.cmb_pow.currentIndex() == 0:
                woj_id = self.cmb_woj.currentData()
                if woj_id:
                    self.txt_teryt_manual.setText(str(woj_id))
                else:
                    self.txt_teryt_manual.clear()
            elif self.cmb_pow.currentIndex() == 1:
                woj_id = self.cmb_woj.currentData()
                if woj_id:
                    self.txt_teryt_manual.setText(str(woj_id))
                else:
                    self.txt_teryt_manual.clear()
            return

        self.cmb_gmina.setEnabled(True)
        self.cmb_gmina.addItem("- Wybierz -", None)
        self.txt_teryt_manual.setText(str(pow_id))

        self.cmb_gmina.addItem("Brak", None)
        for teryt, g in sorted(self.gminy.items(), key=lambda x: x[1]['nazwa']):
            if g['parent'] == pow_id:
                self.cmb_gmina.addItem(f"{g['nazwa']} ({teryt})", teryt)

    def update_teryt_from_gmina(self):
        gmina_id = self.cmb_gmina.currentData()
        self.cmb_obreb.clear()
        if not gmina_id:
            self.cmb_obreb.setEnabled(False)

            if self.cmb_gmina.currentIndex() == 0:
                pow_id = self.cmb_pow.currentData()
                if pow_id:
                    self.txt_teryt_manual.setText(str(pow_id))
                else:
                    woj_id = self.cmb_woj.currentData()
                    if woj_id:
                        self.txt_teryt_manual.setText(str(woj_id))
                    else:
                        self.txt_teryt_manual.clear()
            elif self.cmb_gmina.currentIndex() == 1:
                pow_id = self.cmb_pow.currentData()
                if pow_id:
                    self.txt_teryt_manual.setText(str(pow_id))
                else:
                    woj_id = self.cmb_woj.currentData()
                    if woj_id:
                        self.txt_teryt_manual.setText(str(woj_id))
                    else:
                        self.txt_teryt_manual.clear()
            return

        self.cmb_obreb.setEnabled(True)
        self.cmb_obreb.addItem("- Wybierz -", None)
        self.txt_teryt_manual.setText(str(gmina_id))

        self.cmb_obreb.addItem("Brak", None)
        if str(gmina_id[:6]) in self.obreby_by_gmina:
            for nazwa, teryt in sorted(self.obreby_by_gmina[str(gmina_id[:6])], key=lambda x: x[0]):
                self.cmb_obreb.addItem(f"{nazwa} ({teryt})", teryt)

    def update_teryt_from_obreb(self):
        obreb_id = self.cmb_obreb.currentData()

        if obreb_id:
            self.txt_teryt_manual.setText(str(obreb_id))
        else:
            if self.cmb_obreb.currentIndex() == 0:
                gmina_id = self.cmb_gmina.currentData()
                if gmina_id:
                    self.txt_teryt_manual.setText(str(gmina_id))
                else:
                    pow_id = self.cmb_pow.currentData()
                    if pow_id:
                        self.txt_teryt_manual.setText(str(pow_id))
                    else:
                        woj_id = self.cmb_woj.currentData()
                        if woj_id:
                            self.txt_teryt_manual.setText(str(woj_id))
                        else:
                            self.txt_teryt_manual.clear()
            elif self.cmb_obreb.currentIndex() == 1:
                gmina_id = self.cmb_gmina.currentData()
                if gmina_id:
                    self.txt_teryt_manual.setText(str(gmina_id))
                else:
                    pow_id = self.cmb_pow.currentData()
                    if pow_id:
                        self.txt_teryt_manual.setText(str(pow_id))
                    else:
                        woj_id = self.cmb_woj.currentData()
                        if woj_id:
                            self.txt_teryt_manual.setText(str(woj_id))
                        else:
                            self.txt_teryt_manual.clear()
    def toggle_orto_rok_do(self):
        """Nieużywane – pozostawione dla kompatybilności wstecznej."""
        pass

    def update_ui_from_type(self):
        wybrany_tekst = self.cmbObjType.currentText()

        # Zakładki Identyfikator i Obręb+nr tylko dla działek EGIB
        for tab, title in [(self.tab_id, "Identyfikator / Plik"),
                           (self.tab_precise, "Obręb + nr działki")]:
            idx = self.tabWidget.indexOf(tab)
            if wybrany_tekst == "dzialki (EGIB)":
                if idx == -1:
                    self.tabWidget.insertTab(1, tab, title)
            else:
                if idx != -1:
                    self.tabWidget.removeTab(idx)

        # Combobox roku – aktualizuj na podstawie rejestru
        svc = self._current_skorowidz_service()
        self._update_year_combo(svc)
        self._update_tabs_for_type()

    def toggle_map_tool(self):
        if self.btn_select_rect.isChecked():
            self.map_tool = RectangleMapTool(self.canvas, self.on_rect_selected)
            self.canvas.setMapTool(self.map_tool)
        else:
            self.canvas.unsetMapTool(self.map_tool)
            self.map_tool = None

    def on_rect_selected(self, rect):
        self.btn_select_rect.setChecked(False)
        self.canvas.unsetMapTool(self.map_tool)
        self.run_spatial_download(rect, "Prostokąt", log_extent=False)

    def get_geometry_from_data(self, level, teryt_id):
        """
        Get geometry from loaded reference data.

        :param level: 'wojewodztwo', 'powiat', or 'gmina'
        :param teryt_id: TERYT identifier
        :returns: QgsGeometry or None
        """
        t_id = teryt_id
        t_id_match= re.search(r"'([\d_\.]+)'", t_id)
        teryt_id = t_id_match.group()
        
        if level == 'wojewodztwo' and teryt_id in self.wojewodztwa:
            if isinstance(self.wojewodztwa[teryt_id], dict):
                return self.wojewodztwa[teryt_id].get('geom')
        elif level == 'powiat' and teryt_id in self.powiaty:
            if isinstance(self.powiaty[teryt_id], dict):
                return self.powiaty[teryt_id].get('geom')
        elif level == 'gmina' and teryt_id in self.gminy:
            if isinstance(self.gminy[teryt_id], dict):
                return self.gminy[teryt_id].get('geom')

        QgsMessageLog.logMessage(
            f"[UI] Brak geometrii dla {level}={teryt_id}",
            "PD_GUGiK", Qgis.MessageLevel.Warning
        )
        return None

    # ------------------------------------------------------------------ wspólne

    def _current_source(self):
        """Źródło WFS z katalogu dla pozycji wybranej na liście (None dla skorowidzów/EZiUDP)."""
        return catalog.source_for_label(self.cmbObjType.currentText())

    def _selected_attributes(self):
        attrs = [self.list_attributes.item(i).text() for i in range(self.list_attributes.count())
                 if self.list_attributes.item(i).checkState() == Qt.CheckState.Checked]
        return None if len(attrs) == self.list_attributes.count() else attrs

    def _load_style(self):
        return self.chk_load_style.isChecked()

    def _teryt_from_input(self):
        text = self.txt_teryt_manual.text().strip()
        return text.split(" ")[-1].replace(")", "") if "(" in text else text

    def _track(self, task):
        """Trzyma referencję do zadania do jego końca (inaczej Python je usunie)."""
        self.running_tasks.append(task)

        def forget(*_):
            if task in self.running_tasks:
                self.running_tasks.remove(task)
        task.taskCompleted.connect(forget)
        task.taskTerminated.connect(forget)

    def _begin_progress(self, task, lock_ui=True):
        self.download_task = task
        if lock_ui:
            self.toggle_ui(False)
        self.progressBar.setRange(0, 100)
        self.progressBar.setValue(0)
        self.progressBar.setVisible(True)
        self.btn_cancel.setVisible(True)
        task.progressChanged.connect(lambda p: self.progressBar.setValue(int(p)))

    def _confirm_start(self):
        reply = QMessageBox.question(
            self, "Potwierdzenie",
            "Rozpoczynamy pobieranie danych.\n\nPobieranie może zająć dużo czasu "
            "w zależności od ilości danych.\n\nMożesz przerwać w dowolnym momencie.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def _start_wfs_task(self, source, queries, expected=0, confirm=True):
        """Uruchamia pobieranie w tle; wynik trafia do _on_wfs_done."""
        if not queries:
            self.show_error("Brak zapytań do wykonania.")
            return
        if confirm and not self._confirm_start():
            return
        task = tasks.WfsDownloadTask(source, queries, self._selected_attributes(), expected)
        self._track(task)
        self._begin_progress(task)
        task.done.connect(lambda result, s=source: self._on_wfs_done(s, result))
        QgsApplication.taskManager().addTask(task)

    def _on_wfs_done(self, source, result):
        self.reset_ui()
        n = len(result.features)
        if n:
            layers.write_source(result.features, source, self._load_style())
        if result.canceled:
            self.show_info(f"Pobieranie przerwane. Zapisano {n} obiektów." if n
                           else "Pobieranie anulowane.")
            return
        if not n and result.errors:
            self.show_error(f"Błąd pobierania: {result.errors[0][1]}")
            return
        if not n:
            self.show_info("Brak obiektów w zadanym obszarze.")
            return
        msg = f"Pobrano {n} obiektów."
        if result.errors:
            msg += (f"\n\n{len(result.errors)} z zapytań nie powiodło się "
                    f"(szczegóły w dzienniku, zakładka PD_GUGiK).")
        self.show_info(msg)

    # ------------------------------------------------------------------ PRG

    def run_prg_geometry_download(self):
        """Pobierz i wyświetl geometrię z PRG (bez pobierania działek)."""
        teryt = self._teryt_from_input()
        if not teryt:
            self.show_error("Wpisz kod TERYT do wyszukania.")
            return
        log.info(f"[UI] Pobieranie geometrii PRG dla TERYT: {teryt}")
        geom = self.prg_client.get_boundary_geometry(teryt)
        if geom is None or geom.isEmpty():
            self.show_error(f"Nie udało się pobrać geometrii dla TERYT: {teryt}. "
                            f"Sprawdź poprawność kodu.")
            return
        vl = layers.boundary_layer(geom, teryt, self._load_style())
        if self.canvas:
            self.canvas.setExtent(vl.extent())
            self.canvas.refresh()

    # ------------------------------------------------------------------ po TERYT

    def run_admin_download(self):
        teryt = self._teryt_from_input()
        if not teryt:
            self.show_error("Wpisz kod TERYT do wyszukania.")
            return
        log.info(f"[UI] Pobieranie {self.cmbObjType.currentText()} dla TERYT: {teryt}")

        # Skorowidze: granica z PRG → wyszukiwanie w skorowidzu
        if self._current_skorowidz_service() is not None:
            geom = self.prg_client.get_boundary_geometry(teryt)
            if geom is None or geom.isEmpty():
                self.show_error(f"Nie udało się pobrać geometrii dla TERYT: {teryt}.")
                return
            self._run_skorowidz(geom)
            return

        source = self._current_source()
        if source is None:
            return

        # Kraj/województwo – tylko paczki; powiat – wybór metody
        if source.opendata and len(teryt) < 4:
            self.run_geoparquet_download(teryt, source)
            return
        if source.opendata and len(teryt) == 4:
            box = QMessageBox(self.iface.mainWindow())
            box.setWindowTitle("Wybór metody pobierania")
            box.setText(f"Wybrano powiat (TERYT: {teryt}).\nKtórą metodą chcesz pobrać dane?")
            parquet_btn = box.addButton("GeoPackage (zapis pliku na dysku)",
                                        QMessageBox.ButtonRole.ActionRole)
            box.addButton("WFS (warstwa tymczasowa)", QMessageBox.ButtonRole.ActionRole)
            cancel_btn = box.addButton("Anuluj", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() == parquet_btn:
                self.run_geoparquet_download(teryt, source)
                return
            if box.clickedButton() == cancel_btn:
                return

        geom = self.prg_client.get_boundary_geometry(teryt)
        if geom is None or geom.isEmpty():
            self.show_error(f"Nie udało się pobrać geometrii dla TERYT: {teryt}. "
                            f"Sprawdź poprawność kodu.")
            return

        if source.clip_locally:
            # PRG/PRNG: prostokąt w zapytaniu, dokładne przycięcie po pobraniu
            query = tasks.Query(filters.spatial(source.geom_field, geom.asWkt(), use_bbox=True),
                                clip_geom=geom, label=teryt)
        else:
            precise = hasattr(self, "chk_precise_spatial") and self.chk_precise_spatial.isChecked()
            query = tasks.Query(filters.combine([
                filters.spatial(source.geom_field, geom.asWkt(), use_bbox=not precise),
                filters.attribute(source.teryt_field, teryt, like=True),
            ]), label=teryt)
        self._start_wfs_task(source, [query])

    def run_geoparquet_download(self, teryt, source):
        package = catalog.opendata_package(source, teryt)
        if package is None:
            self.show_error("Dla tego źródła nie ma paczek do pobrania.")
            return
        url, name = package
        ext = name.split(".", 1)[1]
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Wybierz miejsce zapisu pliku", name, f"*.{ext}")
        if not path:
            return
        task = tasks.FileDownloadTask(url, path, f"Pobieranie {name}")
        self._track(task)
        self._begin_progress(task, lock_ui=False)
        task.done.connect(self.on_download_finished_gp)
        QgsApplication.taskManager().addTask(task)
        self.iface.messageBar().pushMessage("Zadanie uruchomione", "Pobieranie odbywa się w tle…",
                                            level=Qgis.MessageLevel.Info)

    def on_download_finished_gp(self, path):
        self.reset_ui()
        if not path:
            return
        try:
            added = layers.add_package(path, self._load_style())
        except Exception as exc:
            self.show_error(f"Nie udało się wczytać pobranego pliku: {exc}")
            return
        if not added:
            self.show_error(f"Nie udało się wczytać pliku: {path}")

    # ------------------------------------------------------------------ po ID

    def run_id_download(self):
        ids = [line.strip() for line in self.txt_ids.toPlainText().splitlines() if line.strip()]
        if not ids:
            self.show_error("Wpisz identyfikatory.")
            return
        source = self._current_source() or catalog.egib_dzialki()
        batch = 10
        queries = [tasks.Query(filters.ids(source.id_field, ids[i:i + batch]),
                               label=f"identyfikatory {i + 1}–{min(i + batch, len(ids))}")
                   for i in range(0, len(ids), batch)]
        log.info(f"[UI] Pobieranie {len(ids)} identyfikatorów w {len(queries)} paczkach")
        self._start_wfs_task(source, queries, confirm=False)

    # ------------------------------------------------------------------ po zasięgu

    def run_extent_download(self):
        self.run_spatial_download(self.canvas.extent(), "Widok mapy", log_extent=False)

    def run_spatial_download(self, rect, source_name, log_extent=True):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:2180")
        if canvas_crs != target_crs:
            rect = QgsCoordinateTransform(canvas_crs, target_crs,
                                          QgsProject.instance()).transformBoundingBox(rect)
        if log_extent:
            log.info(f"[UI] Zakres {source_name}: {rect.toString(2)}")
        geom = QgsGeometry.fromRect(rect)

        if self._current_skorowidz_service() is not None:
            self._run_skorowidz(geom)
            return
        source = self._current_source()
        if source is None:
            return
        query = tasks.Query(filters.spatial(source.geom_field, geom.asWkt()), label=source_name)
        self._start_wfs_task(source, [query])

    # ------------------------------------------------------------------ po warstwie

    def run_layer_download(self):
        """Pobieranie dla każdego obiektu wybranej warstwy poligonowej."""
        layer = self.cmb_layer.currentLayer() if hasattr(self, "cmb_layer") else None
        if not layer:
            self.show_error("Nie znaleziono warstwy poligonowej.")
            return
        if layer.type() != LAYER_VECTOR or layer.geometryType() != GEOM_POLYGON:
            self.show_error("Wybrana warstwa nie jest poligonowa.")
            return

        count = layer.featureCount()
        if count > 100:
            reply = QMessageBox.question(
                self, "Potwierdzenie",
                f"Warstwa zawiera {count} obiektów. Może to zająć dużo czasu. Czy kontynuować?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                return

        if self._current_skorowidz_service() is not None:
            dissolved = None
            for feat in layer.getFeatures():
                g = feat.geometry()
                dissolved = g if dissolved is None else dissolved.combine(g)
            if dissolved:
                self._run_skorowidz(dissolved)
            return

        source = self._current_source()
        if source is None:
            return

        target_crs = QgsCoordinateReferenceSystem("EPSG:2180")
        xform = None
        if layer.crs() != target_crs:
            xform = QgsCoordinateTransform(layer.crs(), target_crs, QgsProject.instance())

        queries = []
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                log.warning(f"[UI] Pominięto obiekt {feat.id()}: pusta geometria")
                continue
            if xform is not None:
                geom.transform(xform)
            label = f"obiekt {feat.id()} warstwy {layer.name()}"
            if geom.constGet().nCoordinates() <= 100:
                queries.append(tasks.Query(
                    filters.spatial(source.geom_field, geom.asWkt(), use_bbox=False), label=label))
                continue
            # Dużo wierzchołków: otoczka wypukła (albo BBOX) w zapytaniu + przycięcie lokalne
            hull = geom.convexHull()
            if hull.constGet().nCoordinates() <= 100:
                flt = filters.spatial(source.geom_field, hull.asWkt(), use_bbox=False)
            else:
                flt = filters.spatial(source.geom_field, geom.asWkt(), use_bbox=True)
            queries.append(tasks.Query(flt, clip_geom=geom, label=label))

        log.info(f"[UI] Pobieranie przez warstwę {layer.name()}: {len(queries)} zapytań")
        self._start_wfs_task(source, queries, confirm=False)

    # ------------------------------------------------------------------ liczba obiektów

    def start_check_hits(self, source, filter_xml):
        self.toggle_ui(False)
        self.progressBar.setVisible(True)
        self.progressBar.setRange(0, 0)
        task = tasks.HitsTask(source, filter_xml)
        self._track(task)
        task.done.connect(lambda hits: self.on_hits_checked(hits, source, filter_xml, task))
        QgsApplication.taskManager().addTask(task)

    def on_hits_checked(self, hits, source, filter_xml, task):
        self.reset_ui()
        if hits == -2:
            self.show_error(f"Błąd sprawdzania: {task.error}")
            return
        if hits == 0:
            self.show_info("Brak wyników w zadanym obszarze.")
            return
        if hits > 5000:
            reply = QMessageBox.question(
                self, "Potwierdzenie",
                f"Zapytanie może zwrócić około {hits} obiektów.\n\nCzy kontynuować?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                return
        self._start_wfs_task(source, [tasks.Query(filter_xml)], expected=max(hits, 0),
                             confirm=False)

    # ------------------------------------------------------------------ stan panelu

    def cancel_download(self):
        for task in list(self.running_tasks):
            task.cancel()

    def reset_ui(self):
        self.toggle_ui(True)
        self.progressBar.setRange(0, 100)
        self.progressBar.setVisible(False)
        self.btn_cancel.setVisible(False)

    def toggle_ui(self, enabled):
        self.tabWidget.setEnabled(enabled)

    def show_error(self, msg):
        QMessageBox.critical(self, "Błąd", str(msg))

    def show_info(self, msg):
        QMessageBox.information(self, "Info", str(msg))

    def run_precise_search(self):
        """Uruchom wyszukiwanie po nazwie obrębu i numerze działki."""
        obreb_name = self.txt_obreb_name.text().strip()
        dzialka_nr = self.txt_dzialka_nr.text().strip()
        self.search_by_obreb_and_nr(obreb_name, dzialka_nr)

    def search_by_obreb_and_nr(self, obreb_name, dzialka_nr):
        """Wyszukaj działki po nazwie obrębu i numerze działki."""
        if not obreb_name or not dzialka_nr:
            self.show_error("Wpisz nazwę obrębu i numer działki.")
            return

        obreb_teryt = None

        obreb_name_corrected = obreb_name[0].upper() + obreb_name[1:].lower() 

        # Wyszukaj obręby po nazwie
        if obreb_name in self.obreby_by_name:
            obreb_teryt_list = self.obreby_by_name[obreb_name]

            if len(obreb_teryt_list) == 1:
                obreb_teryt = obreb_teryt_list[0]
            else:
                # Wiele obrębów o tej nazwie - zapytaj użytkownika
                selected_obreb = self.select_obreb_from_duplicates(obreb_teryt_list)
                if not selected_obreb:
                    return
                obreb_teryt = selected_obreb

        elif obreb_name_corrected in self.obreby_by_name:
            obreb_teryt_list = self.obreby_by_name[obreb_name_corrected]

            if len(obreb_teryt_list) == 1:
                obreb_teryt = obreb_teryt_list[0]
            else:
                # Wiele obrębów o tej nazwie - zapytaj użytkownika
                selected_obreb = self.select_obreb_from_duplicates(obreb_teryt_list)
                if not selected_obreb:
                    return
                obreb_teryt = selected_obreb
        else:
            self.show_error(f"Nie znaleziono obrębu o nazwie '{obreb_name}'.")
            return

        # Buduj zapytanie: TERYT.obreb + numer dzialki
        dzialka_id = f"{obreb_teryt}.{dzialka_nr}"

        QgsMessageLog.logMessage(
            f"[UI] Wyszukiwanie: obręb '{obreb_name}', działka {dzialka_nr} -> ID: {dzialka_id}",
            "PD_GUGiK", Qgis.MessageLevel.Info
        )

        source = catalog.egib_dzialki()
        filter_xml = filters.ids(source.id_field, [dzialka_id])
        if filter_xml:
            self.start_check_hits(source, filter_xml)

    def select_obreb_from_duplicates(self, obreb_teryt_list):
        """Zapytaj użytkownika o wybór obrębu z listy duplikatów."""
        from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QListWidget, QPushButton, QLabel

        dialog = QDialog(self)
        dialog.setWindowTitle("Wybierz obręb")
        dialog.setMinimumWidth(400)

        layout = QVBoxLayout()

        label = QLabel("Znaleziono wiele obrębów o tej nazwie. Wybierz odpowiedni:")
        layout.addWidget(label)

        list_widget = QListWidget()
        for teryt in obreb_teryt_list:
            info = self.obreby_teryt_to_info.get(teryt, {})
            nazwa = info.get('nazwa', teryt)
            gmina = self.get_gmina_name(teryt)
            powiat = self.get_powiat_name(teryt)
            text = f"{nazwa} ({teryt}) - Gmina: {gmina if gmina else '?'}, Powiat: {powiat if powiat else '?'}"
            list_widget.addItem(text)
        layout.addWidget(list_widget)

        btn_ok = QPushButton("Wybierz")
        btn_cancel = QPushButton("Anuluj")

        button_layout = QVBoxLayout()
        button_layout.addWidget(btn_ok)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

        dialog.setLayout(layout)

        selected_teryt = None

        def on_ok():
            nonlocal selected_teryt
            current_row = list_widget.currentRow()
            if current_row >= 0:
                selected_teryt = obreb_teryt_list[current_row]
            dialog.accept()

        def on_cancel():
            dialog.reject()

        btn_ok.clicked.connect(on_ok)
        btn_cancel.clicked.connect(on_cancel)

        if dialog.exec() == QDialog.DialogCode.Accepted and selected_teryt:
            return selected_teryt
        return None

    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()

    #
    #
    # NEW LINES
    def parse_wfs_index_to_features(self, xml_content):
        """Obiekty skorowidza (geometria QgsGeometry + atrybuty) – odczyt przez GDAL w pamięci."""
        return gml.read_typed(xml_content)

    def search_and_show_wfs_index(self, geom, service_url, layer_name):
        """
        Funkcja, którą wywołujesz np. z run_spatial_download.
        Możesz w parametrach podawać różne url i warstwy (Orto, NMT).
        """
        QgsMessageLog.logMessage(f"[UI] Szukam w skorowidzu: {layer_name}", "PD_GUGiK", Qgis.MessageLevel.Info)
        
        # Przykładowe budowanie filtra (zaimplementowane w Twoim kodzie)
        client = WFSIndexClient(url=service_url)
        filter_xml = client.build_spatial_filter(geom.asWkt())
        
        try:
            # POBIERANIE XML-a GML Z WFS (Tu użyj swojej wbudowanej funkcji)
            gml_content = client.download(filter_xml, layer_name=layer_name) 
            
            # Parsowanie uniwersalne (z geometriami)
            features_data = self.parse_wfs_index_to_features(gml_content)
            
            if not features_data:
                self.show_info("Brak wyników w zaznaczonym obszarze dla wybranej warstwy.")
                return
                
            # Pokaż okienko
            dialog = WfsIndexResultsDialog(features_data, self)
            if dialog.exec():
                urls_to_load = dialog.selected_urls
                mode = dialog.load_mode
                
                if urls_to_load:
                    self.process_selected_rasters(urls_to_load, mode)
                    
        except Exception as e:
            self.show_error(f"Błąd podczas szukania w skorowidzu: {e}")

    # Rozszerzenia traktowane jako chmury punktów
    _POINT_CLOUD_EXTENSIONS = frozenset({".laz", ".las", ".copc.laz", ".copc"})

    def _load_file_as_layer(self, path_or_url: str, layer_name: str) -> bool:
        """
        Ładuje plik jako warstwę QGIS dobierając typ na podstawie rozszerzenia.

        Obsługuje:
          - chmury punktów (.laz, .las, .copc.laz, .copc) → QgsPointCloudLayer
            * strumień /vsicurl/ wymaga formatu COPC; zwykłe LAZ nie obsługuje
              strumieniowania – zostanie zalogowane ostrzeżenie
          - wszystko inne → QgsRasterLayer

        Zwraca True jeśli warstwa została dodana do projektu.
        """
        lower = path_or_url.lower()
        # Sprawdź po najdłuższym możliwym sufiksie (copc.laz przed .laz)
        ext = next(
            (e for e in sorted(self._POINT_CLOUD_EXTENSIONS, key=len, reverse=True)
             if lower.endswith(e)),
            None,
        )

        if ext is not None:
            # Chmura punktów
            is_stream = path_or_url.startswith("/vsicurl/")
            if is_stream and ".copc" not in lower:
                QgsMessageLog.logMessage(
                    f"[LAZ] Plik {layer_name} nie jest w formacie COPC – "                    f"strumieniowanie /vsicurl/ może nie działać. "                    f"Zalecane pobieranie na dysk.",
                    "PD_GUGiK", Qgis.MessageLevel.Warning,
                )
            layer = QgsPointCloudLayer(path_or_url, layer_name, "pdal")
        else:
            # Raster (ortofotomapa, NMT, NMPT …)
            layer = QgsRasterLayer(path_or_url, layer_name)

        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return True

        QgsMessageLog.logMessage(
            f"Nie udało się wczytać: {path_or_url}",
            "PD_GUGiK", Qgis.MessageLevel.Warning,
        )
        return False

    def process_selected_rasters(self, urls, mode):
        """Wczytuje zaznaczone pliki na mapę: strumieniowo albo po pobraniu w tle."""
        if mode == "vsicurl":
            added = 0
            for name, url in urls:
                if self._load_file_as_layer(f"/vsicurl/{url}", url.split('/')[-1]):
                    added += 1
            self.show_info(f"Dodano {added} z {len(urls)} warstw strumieniowych.")
            return
        if mode != "download":
            return
        folder = QFileDialog.getExistingDirectory(self, "Wybierz folder do zapisu pobranych plików")
        if not folder:
            return
        self._raster_queue = [(url, os.path.join(folder, url.split('/')[-1])) for _, url in urls]
        self._raster_total = len(self._raster_queue)
        self._raster_saved = 0
        self._next_raster()

    def _next_raster(self):
        """Pobiera pliki skorowidza po jednym, w tle; po każdym wczytuje warstwę."""
        if not self._raster_queue:
            self.reset_ui()
            self.show_info(f"Zakończono pobieranie: {self._raster_saved} z {self._raster_total} plików.")
            return
        url, path = self._raster_queue.pop(0)
        done = self._raster_total - len(self._raster_queue)
        task = tasks.FileDownloadTask(url, path, f"Pobieranie pliku {done} z {self._raster_total}")
        self._track(task)
        self._begin_progress(task)
        task.done.connect(self._on_raster_downloaded)
        QgsApplication.taskManager().addTask(task)

    def _on_raster_downloaded(self, path):
        if path:
            self._raster_saved += 1
            self._load_file_as_layer(path, os.path.basename(path))
        elif self.download_task is not None and self.download_task.isCanceled():
            self._raster_queue = []  # anulowanie zatrzymuje całą kolejkę
        self._next_raster()

    def search_and_show_multiple_wfs_indices(self, geom, service_url, years,
                                              layer_prefix: str = "gugik:SkorowidzOrtofomapy",
                                              layer_suffix: str = ""):
        """
        Przeszukuje wszystkie warstwy skorowidza dla podanej listy lat.

        :param geom:          QgsGeometry obszaru wyszukiwania
        :param service_url:   URL endpointu WFS
        :param years:         Lista lat jako stringi, np. ['2024', '2023']
        :param layer_prefix:  Prefix nazwy warstwy WFS, np. 'gugik:SkorowidzOrtofomapy'
        :param layer_suffix:  Opcjonalny sufiks po roku, np. '' lub '_KRON86'
        """
        QgsMessageLog.logMessage(
            f"[UI] Szukam w skorowidzach ({layer_prefix}) dla {len(years)} lat...",
            "PD_GUGiK", Qgis.MessageLevel.Info,
        )

        client = WFSIndexClient(url=service_url)
        filter_xml = client.build_spatial_filter(geom.asWkt())
        all_features = []

        self.progressBar.setVisible(True)
        self.progressBar.setRange(0, len(years))
        self.iface.mainWindow().statusBar().showMessage(
            "Przeszukiwanie warstw WFS dla wszystkich roczników..."
        )

        for i, rok in enumerate(years):
            wfs_layer_name = f"{layer_prefix}{rok}{layer_suffix}"
            try:
                gml_content = client.download(filter_xml, layer_name=wfs_layer_name)
                features = self.parse_wfs_index_to_features(gml_content)
                if features:
                    all_features.extend(features)
            except Exception as e:
                # Niektóre starsze roczniki mogą nie istnieć – logujemy i pomijamy
                QgsMessageLog.logMessage(
                    f"[UI] Błąd dla warstwy {wfs_layer_name}: {e}",
                    "PD_GUGiK", Qgis.MessageLevel.Warning,
                )

            self.progressBar.setValue(i + 1)
            QgsApplication.processEvents()

        self.progressBar.setVisible(False)
        self.iface.mainWindow().statusBar().clearMessage()

        if not all_features:
            self.show_info("Brak wyników w zaznaczonym obszarze dla wszystkich sprawdzonych lat.")
            return

        dialog = WfsIndexResultsDialog(all_features, self)
        if dialog.exec():
            urls_to_load = dialog.selected_urls
            mode = dialog.load_mode
            if urls_to_load:
                self.process_selected_rasters(urls_to_load, mode)

    def _init_eziudp_tab(self):
        """
        Tworzy zakładkę 'Usługi powiatowe' z EziudpWidget i dodaje ją
        do tabWidget. Zakładka jest początkowo ukryta – pojawia się tylko
        gdy cmbObjType = "Usługi powiatowe (eziudp)".
    
        Wywołaj tę metodę po self.setup_completer() w __init__,
        żeby name_to_teryt był już dostępny.
        """
        from .eziudp_download_dialog import EziudpWidget
        self._eziudp_widget = EziudpWidget(parent=self)
    
        # *** ZMIANA: inject_scope_data zamiast setup_completer ***
        # Przekazujemy pełne słowniki, nie tylko name_to_teryt
        self._eziudp_widget.inject_scope_data(
            self.wojewodztwa,
            self.powiaty,
            self.gminy,
            self.name_to_teryt if hasattr(self, "name_to_teryt") else {},
        )

        self._std_tabs = []
        for i in range(self.tabWidget.count()):
            self._std_tabs.append(
                (self.tabWidget.widget(i), self.tabWidget.tabText(i))
            )
        self._eziudp_tab_visible = False
    
    
    def _update_tabs_for_type(self):
        """
        Chowa standardowe zakładki i pokazuje zakładkę eziudp
        gdy wybrane jest "Usługi powiatowe (eziudp)", i odwrotnie.
    
        Wywołaj tę metodę NA KOŃCU istniejącej metody update_ui_from_type().
        """
        is_eziudp = (self.cmbObjType.currentText() == "Usługi powiatowe (EZiUDP)")
    
        if is_eziudp and not self._eziudp_tab_visible:
            # Usuń standardowe zakładki
            while self.tabWidget.count() > 0:
                self.tabWidget.removeTab(0)
            # Dodaj zakładkę eziudp
            self.tabWidget.addTab(self._eziudp_widget, "Usługi powiatowe")
            self._eziudp_tab_visible = True
    
        elif not is_eziudp and self._eziudp_tab_visible:
            # Usuń zakładkę eziudp
            while self.tabWidget.count() > 0:
                self.tabWidget.removeTab(0)
            # Przywróć standardowe zakładki
            for widget, title in self._std_tabs:
                self.tabWidget.addTab(widget, title)
            self._eziudp_tab_visible = False

    def _init_category_filter(self):
        """
        Tworzy cmbCategory i wstawia go nad cmbObjType programowo
        (nie wymaga edycji .ui).
    
        Kategorie wykrywane automatycznie z tekstu w nawiasach w cmbObjType.
        Specjalne kategorie bez nawiasów trafiają do "Skorowidze".
        """
        from qgis.PyQt.QtWidgets import QComboBox, QLabel, QHBoxLayout, QWidget
    
        # Utwórz widget jeśli nie zdefiniowany w .ui
        if not hasattr(self, "cmbCategory"):
            self.cmbCategory = QComboBox()
            self.cmbCategory.setToolTip(
                "Filtruj listę typów danych według kategorii.\n"
                "Zmiana kategorii ogranicza dostępne opcje w polu poniżej."
            )
    
            # Wstaw cmbCategory nad cmbObjType w tym samym layoucie
            # Szukamy cmbObjType i wstawiamy przed nim
            parent_widget = self.cmbObjType.parentWidget()
            layout = parent_widget.layout() if parent_widget else None
            if layout:
                idx = None
                for i in range(layout.count()):
                    item = layout.itemAt(i)
                    if item and item.widget() is self.cmbObjType:
                        idx = i
                        break
                if idx is not None:
                    row = QHBoxLayout()
                    row.addWidget(QLabel("Kategoria:"))
                    row.addWidget(self.cmbCategory, 1)
                    container = QWidget()
                    container.setLayout(row)
                    layout.insertWidget(idx, container)
    
        # Zbierz unikalne kategorie z cmbObjType
        self._rebuild_category_combo()
    
    
    def _rebuild_category_combo(self):
        """
        Wypełnia cmbCategory unikalnymi kategoriami z cmbObjType.
    
        Kategoria = tekst w nawiasie, np. "(EGIB)" → "EGIB".
        Pozycje bez nawiasów → "Skorowidze".
        Pierwsza pozycja zawsze "Wszystkie".
        """
        import re as _re
    
        # cats = ["Wszystkie"]
        cats = []
        seen = set()
    
        for i in range(self.cmbObjType.count()):
            text = self.cmbObjType.itemText(i)
            m = _re.search(r'\(([^)]+)\)', text)
            if m:
                cat = m.group(1)
            else:
                cat = "Skorowidze"
            if cat not in seen:
                cats.append(cat)
                seen.add(cat)
    
        self.cmbCategory.blockSignals(True)
        prev = self.cmbCategory.currentText()
        self.cmbCategory.clear()
        self.cmbCategory.addItems(cats)
        # Przywróć poprzednią kategorię jeśli jeszcze istnieje
        idx = self.cmbCategory.findText(prev)
        if idx >= 0:
            self.cmbCategory.setCurrentIndex(idx)
        self.cmbCategory.blockSignals(False)
    
        # Zastosuj filtr od razu
        self._apply_category_filter()
    
    
    def _apply_category_filter(self):
        """
        Chowa/pokazuje pozycje w cmbObjType pasujące do wybranej kategorii.
    
        Implementacja: Qt nie obsługuje natywnego ukrywania pozycji w QComboBox,
        więc przebudowujemy cmbObjType zostawiając tylko pasujące pozycje
        i zachowując referencję do pełnej listy w self._all_obj_type_items.
        """
        import re as _re
    
        selected_cat = self.cmbCategory.currentText()
    
        # Zachowaj pełną listę przy pierwszym wywołaniu
        if not hasattr(self, "_all_obj_type_items"):
            self._all_obj_type_items = [
                self.cmbObjType.itemText(i)
                for i in range(self.cmbObjType.count())
            ]
    
        current_text = self.cmbObjType.currentText()
    
        self.cmbObjType.blockSignals(True)
        self.cmbObjType.clear()
    
        for text in self._all_obj_type_items:
            if selected_cat == "Wszystkie":
                self.cmbObjType.addItem(text)
            else:
                m = _re.search(r'\(([^)]+)\)', text)
                cat = m.group(1) if m else "Skorowidze"
                if cat == selected_cat:
                    self.cmbObjType.addItem(text)
    
        # Przywróć poprzednią wartość jeśli jest w filtrze
        idx = self.cmbObjType.findText(current_text)
        if idx >= 0:
            self.cmbObjType.setCurrentIndex(idx)
        else:
            self.cmbObjType.setCurrentIndex(0)
    
        self.cmbObjType.blockSignals(False)
    
        # Wyzwól update UI dla nowego wyboru
        self.update_ui_from_type()