# SPDX-License-Identifier: GPL-2.0-or-later
from pathlib import Path

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms import ConvertTileCache, ConvertVectorTilePackage


class Provider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(ConvertTileCache())
        self.addAlgorithm(ConvertVectorTilePackage())

    def id(self):
        return "esritilecache"

    def name(self):
        return "Esri tile cache"

    def icon(self):
        return QIcon(str(Path(__file__).parent / "icon.svg"))
