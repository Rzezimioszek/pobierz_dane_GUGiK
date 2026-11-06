# -*- coding: utf-8 -*-
"""Jedno miejsce logowania wtyczki (zakładka „PD_GUGiK” w dzienniku QGIS)."""
from qgis.core import Qgis, QgsMessageLog

LOG_TAG = "PD_GUGiK"


def info(msg: str) -> None:
    QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.MessageLevel.Info)


def warning(msg: str) -> None:
    QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.MessageLevel.Warning)


def error(msg: str) -> None:
    QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.MessageLevel.Critical)


def success(msg: str) -> None:
    QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.MessageLevel.Success)
