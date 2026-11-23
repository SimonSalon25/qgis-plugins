# SPDX-License-Identifier: GPL-2.0-or-later
"""QGIS plugin: Slovak S-JTSK (JTSK, JTSK03) ↔ ETRS89 / WGS 84 transformations the official ÚGKK way."""


def classFactory(iface):  # noqa: N802 — QGIS API
    from .plugin import Plugin
    return Plugin(iface)
