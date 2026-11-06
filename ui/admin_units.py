# -*- coding: utf-8 -*-
"""
Jednostki administracyjne z data/*.geojson: nazwy i kody TERYT
(województwa, powiaty, gminy, obręby). Wczytywane raz na sesję QGIS.
"""
import json
import os
import re
from typing import Optional

from ..core import log

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

_CACHE = None


def _props(feat: dict, *keys) -> Optional[str]:
    props = {k.lower(): v for k, v in (feat.get("properties") or {}).items()}
    for k in keys:
        if props.get(k) not in (None, ""):
            return str(props[k])
    return None


def _read(name: str) -> list:
    path = os.path.join(DATA_DIR, f"{name}.geojson")
    if not os.path.exists(path):
        log.warning(f"[Jednostki] Brak pliku {path}")
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("features", [])


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
        for f in _read("wojewodztwa"):
            name, t = _props(f, "nazwa", "name"), _props(f, "teryt", "id", "kod")
            if name and t:
                self.wojewodztwa[t] = {"nazwa": name, "geom": None}
        for f in _read("powiaty"):
            name, t = _props(f, "nazwa", "name"), _props(f, "teryt", "id", "kod")
            if name and t:
                self.powiaty[t] = {"nazwa": name, "parent": t[:2], "geom": None}
        for f in _read("gminy"):
            name, t = _props(f, "nazwa", "name"), _props(f, "teryt", "id", "kod")
            if name and t:
                self.gminy[t] = {"nazwa": name, "parent": t[:4], "geom": None}
        for f in _read("obreby"):
            name, t = _props(f, "nazwa", "name"), _props(f, "teryt", "id", "kod")
            if not (name and t):
                continue
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
                 f"{len(self.gminy)} gmin, {len(self.obreby_teryt_to_info)} obrębów")

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

    def gmina_name(self, teryt: str) -> Optional[str]:
        for key in (teryt[:8], teryt[:6]):
            if key in self.gminy:
                return self.gminy[key]["nazwa"]
        return None

    def powiat_name(self, teryt: str) -> Optional[str]:
        return self.powiaty.get(teryt[:4], {}).get("nazwa")

    def find_obreby(self, name: str) -> list:
        """TERYT-y obrębów o tej nazwie (bez względu na wielkość liter)."""
        name = (name or "").strip()
        if name in self.obreby_by_name:
            return self.obreby_by_name[name]
        low = name.lower()
        for n, ts in self.obreby_by_name.items():
            if n.lower() == low:
                return ts
        return []
