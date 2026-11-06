# -*- coding: utf-8 -*-
"""Interfejs wtyczki: panel w trzech krokach (co pobrać → skąd → jak zapisać)."""
from qgis.PyQt.QtCore import QCoreApplication


def tr(text: str) -> str:
    """Napis do tłumaczenia (kontekst wspólny dla całego interfejsu)."""
    return QCoreApplication.translate("PobierzDaneGUGiK", text)
