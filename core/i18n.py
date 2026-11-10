# -*- coding: utf-8 -*-
"""
Tłumaczenia: napisy w kodzie są po polsku, plik i18n/PD_GUGiK_en.qm podaje
angielskie. Jeden kontekst dla całej wtyczki – tools/tlumaczenia.py zbiera
wszystkie wywołania tr("…") i mark("…").
"""
from qgis.PyQt.QtCore import QCoreApplication

CONTEXT = "PobierzDaneGUGiK"


def tr(text: str) -> str:
    """Napis w języku interfejsu QGIS."""
    return QCoreApplication.translate(CONTEXT, text)


def mark(text: str) -> str:
    """Oznacza napis do tłumaczenia bez tłumaczenia go teraz (stałe, tabele) – potem tr(zmienna)."""
    return text


def server_error(detail: str) -> str:
    """Wspólny początek komunikatu o błędzie zwróconym przez usługę (rozpoznaje go net.friendly)."""
    return tr("Serwer zwrócił błąd: ") + detail
