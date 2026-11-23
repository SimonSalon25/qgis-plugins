# SPDX-License-Identifier: GPL-2.0-or-later
"""QGIS plugin: convert Esri tile caches (.tpk Compact V1/V2, .tpkx, cache folders, .vtpk) to GeoPackage / MBTiles."""


def classFactory(iface):  # noqa: N802 — QGIS API
    from .plugin import Plugin
    return Plugin(iface)
