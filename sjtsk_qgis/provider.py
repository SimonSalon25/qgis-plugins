# SPDX-License-Identifier: GPL-2.0-or-later
from pathlib import Path

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms import InstallGrid, TransformLayer


class Provider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(TransformLayer())
        self.addAlgorithm(InstallGrid())

    def id(self):
        return "sjtsk"

    def name(self):
        return "S-JTSK (official ÚGKK)"

    def icon(self):
        return QIcon(str(Path(__file__).parent / "icon.svg"))
