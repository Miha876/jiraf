import sys

from PySide6 import QtCore, QtGui, QtWidgets

from jiraf_app.main_window import MainWindow

"""Простой модуль запуска графического интерфейса."""


class Splash(QtWidgets.QSplashScreen):
    def __init__(self):
        pixmap = QtGui.QPixmap(420, 220)
        pixmap.fill(QtGui.QColor("#0f172a"))
        super().__init__(pixmap)
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
        self._base = "Загрузка машинного зрения"
        self._dots = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.showMessage(
            self._base,
            QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter,
            QtGui.QColor("#e2e8f0"),
        )

    def _tick(self):
        self._dots = (self._dots + 1) % 4
        text = self._base + "." * self._dots
        self.showMessage(
            text,
            QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter,
            QtGui.QColor("#e2e8f0"),
        )


def main():
    """Создаем Qt-приложение, отображаем главное окно и стартуем цикл событий."""
    app = QtWidgets.QApplication(sys.argv)
    splash = Splash()
    splash.show()
    app.processEvents()
    window = MainWindow()
    window.show()
    splash.finish(window)
    return app.exec()
