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
        for t, name in data.get("obreby", {}).items():
            gmina = t.split(".")[0] if "." in t else t[:8]
            self.obreby_by_name.setdefault(name, []).append(t)
            self.obreby_teryt_to_info[t] = {"nazwa": name, "gmina_code": gmina,
                                            "powiat_code": t[:4]}

        for t, d in self.wojewodztwa.items():
            self.name_to_teryt[f"{d['nazwa']} (województwo {t})"] = t
        for t, d in self.powiaty.items():
            self.name_to_teryt[f"{d['nazwa']} (powiat {t})"] = t
        for t, d in self.gminy.items():
            self.name_to_teryt[f"{d['nazwa']} (gmina {t})"] = t
        for t, d in self.obreby_teryt_to_info.items():
            self.name_to_teryt[f"{d['nazwa']} (obręb {t})"] = t
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
            return f"województwo {self.wojewodztwa[teryt]['nazwa']}"
        if lvl == "powiat":
            woj = self.wojewodztwa.get(teryt[:2], {}).get("nazwa", "?")
            return f"{self.powiaty[teryt]['nazwa']}, woj. {woj}"
        if lvl == "gmina":
            pow_ = self.powiaty.get(teryt[:4], {}).get("nazwa", "?")
            return f"gmina {self.gminy[teryt]['nazwa']}, {pow_}"
        if lvl == "obręb":
            info = self.obreby_teryt_to_info[teryt]
            gm = self.gminy.get(info["gmina_code"], {}).get("nazwa", "?")
            return f"obręb {info['nazwa']}, gmina {gm}"
        return ""

    def date_text(self) -> str:
        return f"z dnia {self.date}" if self.date else "z wydania wtyczki"

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
            return "", ("Nieprawidłowy kod TERYT – oczekiwane: województwo 2 cyfry, powiat 4, "
                        "gmina np. 146501_1, obręb np. 146501_1.0001.")
        return fmt, (f"Tego kodu nie ma w słowniku jednostek ({self.date_text()}) – może to nowa "
                     f"jednostka; zostanie sprawdzony w PRG przy pobieraniu.")

    def gmina_name(self, teryt: str) -> Optional[str]:
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
