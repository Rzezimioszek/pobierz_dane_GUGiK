# -*- coding: utf-8 -*-
"""
Jednostki administracyjne z data/jednostki.json.gz: nazwy i kody TERYT
(województwa, powiaty, gminy, obręby). Wczytywane raz na sesję QGIS.

Słownik odświeża się przed wydaniem (tools/odswiez_slowniki.py). Obręby
zmieniają się częściej, więc kod zgodny z formatem, a nieobecny
w słowniku, nie blokuje pobierania – sprawdza go granica z PRG.
"""
import gzip
import json
import os
import re
from typing import Optional

from ..core import log
from ..core.i18n import tr

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

_CACHE = None


DATA_FILE = os.path.join(DATA_DIR, "jednostki.json.gz")

# Formaty kodów: województwo, powiat, gmina (z typem po „_”), obręb
TERYT_PATTERNS = {
    "województwo": re.compile(r"^\d{2}$"),
    "powiat": re.compile(r"^\d{4}$"),
    "gmina": re.compile(r"^\d{6}_\d$"),
    "obręb": re.compile(r"^\d{6}_\d\.\d{4}$"),
}


def normalize_teryt(text: str) -> str:
    """'1465011' → '146501_1' (gmina wpisana bez podkreślenia); reszta bez zmian."""
    text = (text or "").strip()
    if len(text) == 7 and text.isdigit():
        return f"{text[:6]}_{text[6]}"
    return text


def teryt_format(teryt: str) -> str:
    """Poziom wynikający z samego formatu kodu ('' gdy format nieprawidłowy)."""
    for level, pattern in TERYT_PATTERNS.items():
        if pattern.match(teryt or ""):
            return level
    return ""


def _read() -> dict:
    if not os.path.exists(DATA_FILE):
        log.warning(f"[Jednostki] Brak pliku {DATA_FILE}")
        return {}
    try:
        with gzip.open(DATA_FILE, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        log.error(f"[Jednostki] Nie można odczytać {DATA_FILE}: {exc}")
        return {}


class AdminUnits:
    """
    Słowniki w formacie używanym też przez okno EZiUDP:
        wojewodztwa {teryt: {'nazwa'}}, powiaty / gminy {teryt: {'nazwa', 'parent'}}
        obreby_by_name {nazwa: [teryt…]}, obreby_teryt_to_info {teryt: {...}}
        name_to_teryt {"Nazwa (gmina 141201_1)": "141201_1", …} – podpowiedzi
    """

    def __init__(self):
        self.wojewodztwa, self.powiaty, self.gminy = {}, {}, {}
        self.obreby_by_name, self.obreby_teryt_to_info = {}, {}
        self.name_to_teryt = {}
        self.jednostki, self._obreby_by_jednostka = {}, {}
        self.date = ""
        self._load()

    @classmethod
    def instance(cls) -> "AdminUnits":
        global _CACHE
        if _CACHE is None:
            _CACHE = cls()
        return _CACHE

    def _load(self) -> None:
        data = _read()
        self.date = data.get("data") or ""        # stan słownika (RRRR-MM-DD), '' = nieznany
        for t, name in data.get("wojewodztwa", {}).items():
            self.wojewodztwa[t] = {"nazwa": name, "geom": None}
        for t, name in data.get("powiaty", {}).items():
            self.powiaty[t] = {"nazwa": name, "parent": t[:2], "geom": None}
        for t, name in data.get("gminy", {}).items():
            self.gminy[t] = {"nazwa": name, "parent": t[:4], "geom": None}
        # jednostki ewidencyjne (np. 020104_4 „miasto”, 126102_9 „Kraków-Krowodrza”)
        self.jednostki = dict(data.get("jednostki", {}))
        self._obreby_by_jednostka = {}
        for t, name in data.get("obreby", {}).items():
            jedn = t.split(".")[0]
            self._obreby_by_jednostka.setdefault(jedn, []).append(t)
            self.obreby_by_name.setdefault(name, []).append(t)
            self.obreby_teryt_to_info[t] = {"nazwa": name, "gmina_code": self.gmina_for(jedn),
                                            "powiat_code": t[:4], "jednostka": jedn}

        for t, d in self.wojewodztwa.items():
            self.name_to_teryt[f"{d['nazwa']} ({tr('województwo')} {t})"] = t
        for t, d in self.powiaty.items():
            self.name_to_teryt[f"{d['nazwa']} ({tr('powiat')} {t})"] = t
        for t, d in self.gminy.items():
            self.name_to_teryt[f"{d['nazwa']} ({tr('gmina')} {t})"] = t
        for t, d in self.obreby_teryt_to_info.items():
            self.name_to_teryt[f"{d['nazwa']} ({tr('obręb')} {t})"] = t
        log.info(f"[Jednostki] {len(self.wojewodztwa)} woj., {len(self.powiaty)} pow., "
                 f"{len(self.gminy)} gmin, {len(self.obreby_teryt_to_info)} obrębów "
                 f"(stan: {self.date or 'nieznany'})")

    # ------------------------------------------------------------------ zapytania

    @staticmethod
    def teryt_from_text(text: str) -> str:
        """'Kraków (gmina 126101_1)' → '126101_1'; sam kod zostaje bez zmian."""
        text = (text or "").strip()
        m = re.search(r"\((?:\S+ )?([0-9][0-9_.]*)\)\s*$", text)
        return m.group(1) if m else text

    def level(self, teryt: str) -> str:
        """kraj / województwo / powiat / gmina / obręb / '' (nieznany)."""
        if teryt in ("", "0", "PL"):
            return "kraj"
        if teryt in self.wojewodztwa:
            return "województwo"
        if teryt in self.powiaty:
            return "powiat"
        if teryt in self.gminy:
            return "gmina"
        if teryt in self.obreby_teryt_to_info:
            return "obręb"
        return ""

    def describe(self, teryt: str) -> str:
        """Czytelny opis jednostki do podpowiedzi pod polem; '' gdy kod nieznany."""
        lvl = self.level(teryt)
        if lvl == "województwo":
            return f"{tr('województwo')} {self.wojewodztwa[teryt]['nazwa']}"
        if lvl == "powiat":
            woj = self.wojewodztwa.get(teryt[:2], {}).get("nazwa", "?")
            return f"{self.powiaty[teryt]['nazwa']}, {tr('woj.')} {woj}"
        if lvl == "gmina":
            pow_ = self.powiaty.get(teryt[:4], {}).get("nazwa", "?")
            return f"{tr('gmina')} {self.gminy[teryt]['nazwa']}, {pow_}"
        if lvl == "obręb":
            info = self.obreby_teryt_to_info[teryt]
            gm = self.gminy.get(info["gmina_code"], {}).get("nazwa", "?")
            jedn = self.jednostki.get(info["jednostka"])
            return f"{tr('obręb')} {info['nazwa']}, {tr('gmina')} {gm}" + (f" ({jedn})" if jedn and jedn != gm else "")
        return ""

    def date_text(self) -> str:
        return tr("z dnia ") + self.date if self.date else tr("z wydania wtyczki")

    def check(self, teryt: str) -> tuple:
        """
        (poziom, uwaga) dla kodu wpisanego przez użytkownika:
        znany → (poziom, ''); poprawny format, ale nowy → (poziom, ostrzeżenie);
        zły format → ('', błąd).
        """
        teryt = normalize_teryt(teryt)
        known = self.level(teryt)
        if known:
            return known, ""
        fmt = teryt_format(teryt)
        if not fmt:
            return "", tr("Nieprawidłowy kod TERYT – oczekiwane: województwo 2 cyfry, powiat 4, "
                          "gmina np. 146501_1, obręb np. 146501_1.0001.")
        return fmt, tr("Tego kodu nie ma w słowniku jednostek ({0}) – może to nowa "
                       "jednostka; zostanie sprawdzony w PRG przy pobieraniu.").format(self.date_text())

    # ------------------------------------------------------------------ gmina ↔ jednostki ewidencyjne
    #
    # Obręby należą do jednostek ewidencyjnych, nie wprost do gmin:
    #   gmina miejska _1 / wiejska _2 → ta sama jednostka (_1 / _2)
    #   gmina miejsko-wiejska _3     → miasto _4 i obszar wiejski _5
    #   miasto z dzielnicami/delegaturami (Warszawa, Kraków, Łódź) → jednostki _8/_9 w tym powiecie

    def jednostki_for_gmina(self, gmina: str) -> list:
        """Kody jednostek ewidencyjnych, w których leżą obręby gminy."""
        gmina = normalize_teryt(gmina)
        if len(gmina) != 8 or gmina[6] != "_":
            return []
        base, kind = gmina[:6], gmina[7]
        if kind == "3":
            return [f"{base}_4", f"{base}_5"]
        if kind == "1":
            known = sorted({j for j in list(self._obreby_by_jednostka) + list(self.jednostki)
                            if j[:4] == gmina[:4] and j[7:] in ("8", "9")})
            if known and gmina not in self._obreby_by_jednostka:
                return known
        return [gmina]

    def gmina_for(self, jednostka: str) -> str:
        """Kod gminy dla jednostki ewidencyjnej (odwrotność jednostki_for_gmina)."""
        j = jednostka[:8]
        if len(j) == 8 and j[7] in ("4", "5"):
            return f"{j[:6]}_3"
        if len(j) == 8 and j[7] in ("8", "9"):
            city = [g for g in self.gminy if g[:4] == j[:4] and g.endswith("_1")]
            return city[0] if len(city) == 1 else j
        return j

    def obreby_for_gmina(self, gmina: str) -> list:
        """[(kod, nazwa)] obrębów gminy ze słownika, w kolejności kodów."""
        out = []
        for j in self.jednostki_for_gmina(gmina):
            out += [(t, self.obreby_teryt_to_info[t]["nazwa"]) for t in self._obreby_by_jednostka.get(j, [])]
        return sorted(out)

    def add_obreby(self, obreby: list) -> int:
        """Dopisuje do słownika (na tę sesję) obręby z ULDK; zwraca liczbę nowych."""
        new = 0
        for code, name in obreby:
            if code in self.obreby_teryt_to_info:
                continue
            jedn = code.split(".")[0]
            self.obreby_teryt_to_info[code] = {"nazwa": name, "gmina_code": self.gmina_for(jedn),
                                               "powiat_code": code[:4], "jednostka": jedn}
            self._obreby_by_jednostka.setdefault(jedn, []).append(code)
            self.obreby_by_name.setdefault(name, []).append(code)
            self.name_to_teryt[f"{name} ({tr('obręb')} {code})"] = code
            new += 1
        return new

    def gmina_name(self, teryt: str) -> Optional[str]:
        g = self.gmina_for(teryt.split(".")[0])
        if g in self.gminy:
            return self.gminy[g]["nazwa"]
        for key in (teryt[:8], teryt[:6]):
            if key in self.gminy:
                return self.gminy[key]["nazwa"]
        return None

    def powiat_name(self, teryt: str) -> Optional[str]:
        return self.powiaty.get(teryt[:4], {}).get("nazwa")

    def find_obreby(self, name: str) -> list:
        """
        TERYT-y obrębów o tej nazwie (bez względu na wielkość liter).
        Kod obrębu wpisany zamiast nazwy (np. 146501_1.0001) jest przyjmowany
        także spoza słownika – obręby zmieniają się częściej niż wydania wtyczki.
        """
        name = (name or "").strip()
        if teryt_format(name) == "obręb":
            return [name]
        if name in self.obreby_by_name:
            return self.obreby_by_name[name]
        low = name.lower()
        for n, ts in self.obreby_by_name.items():
            if n.lower() == low:
                return ts
        return []
