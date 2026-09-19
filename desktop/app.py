"""PyQt6 window containing the complete local Srez interface."""
import json
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QLockFile, QTimer, QUrl, Qt
from PyQt6.QtGui import QAction, QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QToolBar, QVBoxLayout,
)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6 import QtSvg  # SVG icon support in frozen builds.

from backend.config import PROJECT_ROOT
from .runtime import LocalServer, load_settings
from .settings import save_connection


class LocalPage(QWebEnginePage):
    def __init__(self, profile, base_url, parent=None):
        super().__init__(profile, parent)
        self.origin = QUrl(base_url)

    def is_local(self, url):
        return (url.scheme(), url.host(), url.port()) == (
            self.origin.scheme(), self.origin.host(), self.origin.port())

    def acceptNavigationRequest(self, url, nav_type, is_main):
        if self.is_local(url) or url.toString() == "about:blank":
            return True
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(url)
        return False


class ConnectionDialog(QDialog):
    def __init__(self, data_dir, settings, parent):
        super().__init__(parent)
        self.setWindowTitle("Настройки ИИ")
        self.setMinimumWidth(540)
        self.data_dir = data_dir
        layout = QVBoxLayout(self)
        note = QLabel("Ключ хранится в .env в папке данных.\nСохранение настроек не отправляет запрос к модели.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItems(["openai", "proxyapi"])
        self.provider.setCurrentText(settings.ai_provider)
        form.addRow("Провайдер", self.provider)
        self.fields = {}
        for key, label, value in [
            ("OPENAI_API_KEY", "Ключ OpenAI", settings.openai_api_key.get_secret_value()),
            ("PROXY_API_KEY", "Ключ ProxyAPI", settings.proxy_api_key.get_secret_value()),
            ("TEXT_MODEL", "Модель для текста", settings.text_model),
            ("IMAGE_MODEL", "Модель для изображений", settings.image_model),
        ]:
            widget = QLineEdit(value)
            widget.setMaxLength(1024)
            if "KEY" in key:
                widget.setEchoMode(QLineEdit.EchoMode.Password)
            else:
                widget.setPlaceholderText("Пусто — модель по умолчанию")
            form.addRow(label, widget)
            self.fields[key] = widget
        layout.addLayout(form)
        path_label = QLabel("Папка данных: " + str(data_dir))
        path_label.setWordWrap(True)
        path_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(path_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        values = {key: field.text().strip() for key, field in self.fields.items()}
        values["AI_PROVIDER"] = self.provider.currentText()
        try:
            save_connection(self.data_dir, values)
        except Exception:
            QMessageBox.warning(self, "Не удалось сохранить", "Проверьте поля и доступ к папке данных.")
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, data_dir, smoke_report=None):
        super().__init__()
        self.data_dir = data_dir
        self.smoke_report = smoke_report
        self.smoke_done = False
        self.smoke_success = False
        self.setWindowTitle("Срез · оптовые цветы · Санкт-Петербург")
        self.setWindowIcon(QIcon(str(PROJECT_ROOT / "frontend/mark.svg")))
        self.resize(1360, 900)
        self.setMinimumSize(900, 640)
        self.profile = QWebEngineProfile(self)  # In-memory cookies/cache; records stay in SQLite.
        self.profile.downloadRequested.connect(self.download)
        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)
        self.toolbar = QToolBar("Приложение", self)
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)
        self.actions = []
        for label, callback in [
            ("Обновить", self.refresh), ("Настройки ИИ", self.configure),
            ("Папка данных", self.open_data), ("Открыть в браузере", self.open_browser),
            ("О программе", self.about),
        ]:
            action = QAction(label, self)
            action.triggered.connect(callback)
            self.toolbar.addAction(action)
            self.actions.append(action)
        self.toolbar.setStyleSheet("QToolBar { background: #f6f7f0; padding: 5px; spacing: 10px; border: 0; } QToolButton { padding: 7px 10px; color: #244337; }")
        self.server = None
        self.page_loaded = False
        self.start_server()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(120)
        if smoke_report:
            QTimer.singleShot(45000, lambda: self.finish_smoke({"ok": False, "reason": "ui_timeout"}))

    def start_server(self):
        self.settings = load_settings(self.data_dir)
        self.server = LocalServer(self.settings)
        self.started_at = time.monotonic()
        self.page_loaded = False
        self.page = LocalPage(self.profile, self.server.url, self.view)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.page.newWindowRequested.connect(self.open_link)
        self.page.renderProcessTerminated.connect(self.renderer_failed)
        self.view.setPage(self.page)
        if not hasattr(self, "_connected"):
            self.view.loadFinished.connect(self.loaded)
            self._connected = True
        self.statusBar().showMessage("Запуск локального сервера…")
        self.server.start()

    def poll(self):
        if self.server.ready and not self.page_loaded:
            self.page_loaded = True
            self.view.setUrl(QUrl(self.server.url))
        elif not self.server.thread.is_alive() or (not self.server.ready and time.monotonic() - self.started_at > 25):
            self.timer.stop()
            if self.smoke_report:
                self.finish_smoke({"ok": False, "reason": "server_start_failed"})
            else:
                QMessageBox.critical(self, "Не удалось запустить сервер", "Закройте приложение и проверьте доступ к папке данных. Запуск: start-desktop.ps1.")
            return
        ready = self.server.ready and not self.server.busy
        for action in self.actions[:2]:
            action.setEnabled(ready)
        self.statusBar().showMessage(("Выполняется запрос · " if self.server.busy else "Данные: ") + str(self.data_dir))

    def loaded(self, ok):
        if not ok:
            self.statusBar().showMessage("Не удалось загрузить интерфейс. Нажмите «Обновить».")
        if self.smoke_report and ok:
            QTimer.singleShot(1200, self.check_smoke)

    def check_smoke(self):
        # Read-only check of this application's rendered interface.
        self.page.runJavaScript("""JSON.stringify({
            title: document.title,
            cards: document.querySelectorAll('.supplier-card').length,
            heading: document.querySelector('h1')?.textContent,
            body: document.body.innerText.includes('Учебный пример'),
            width: innerWidth
        })""", self.receive_smoke)

    def receive_smoke(self, value):
        try:
            result = json.loads(value or "{}")
        except (ValueError, TypeError):
            result = {}
        result["ok"] = result.get("cards") == 5 and result.get("heading") == "Обзор поставщиков"
        if result["ok"]:
            self.smoke_report.parent.mkdir(parents=True, exist_ok=True)
            screenshot = self.smoke_report.with_suffix(".png")
            result["screenshot_saved"] = self.grab().save(str(screenshot))
        self.finish_smoke(result)

    def finish_smoke(self, result):
        if self.smoke_done or not self.smoke_report:
            return
        self.smoke_done = True
        self.smoke_success = bool(result.get("ok"))
        result.update(frozen=bool(getattr(sys, "frozen", False)), test="desktop_ui", external_requests=0)
        self.smoke_report.parent.mkdir(parents=True, exist_ok=True)
        self.smoke_report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.close()

    def renderer_failed(self, status, code):
        if self.smoke_report:
            self.finish_smoke({"ok": False, "reason": "renderer_terminated", "exit_code": code})
        else:
            QMessageBox.warning(self, "Интерфейс остановился", "Не удалось отобразить встроенный интерфейс. Можно нажать «Открыть в браузере» и продолжить работу, оставив это окно открытым. Сохранённые материалы остаются в папке данных.")

    def ensure_idle(self):
        if self.server and self.server.busy:
            QMessageBox.information(self, "Запрос ещё выполняется", "Дождитесь завершения сбора или анализа.")
            return False
        return True

    def refresh(self):
        if self.ensure_idle():
            self.view.reload()

    def configure(self):
        if not self.ensure_idle():
            return
        if ConnectionDialog(self.data_dir, self.settings, self).exec() == QDialog.DialogCode.Accepted:
            self.timer.stop()
            if not self.server.stop():
                QMessageBox.warning(self, "Сервер ещё останавливается", "Настройки сохранены. Закройте и снова откройте приложение.")
                self.timer.start()
                return
            self.start_server()
            self.timer.start()

    def open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))

    def open_browser(self):
        if self.server.ready:
            QDesktopServices.openUrl(QUrl(self.server.url))

    def open_link(self, request):
        url = request.requestedUrl()
        if url.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(url)

    def download(self, item):
        url = item.url()
        if not (self.page.is_local(url) or url.toString().startswith("blob:" + self.server.url + "/")):
            item.cancel()
            return
        name, _ = QFileDialog.getSaveFileName(self, "Сохранить файл", str(Path.home() / "Downloads" / Path(item.suggestedFileName()).name))
        if not name:
            item.cancel()
            return
        destination = Path(name)
        item.setDownloadDirectory(str(destination.parent))
        item.setDownloadFileName(destination.name)
        item.accept()

    def about(self):
        QMessageBox.information(self, "Срез 0.7", "Оптовые цветы · Санкт-Петербург\nPyQt6 + Qt WebEngine + FastAPI\n\nУчебный пример работает без ключа. Для нового анализа нужен ключ провайдера; для сбора сайтов — Google Chrome.\n\nДокументация: docs/DESKTOP.md в исходниках.")

    def closeEvent(self, event):
        if not self.ensure_idle():
            event.ignore()
            return
        self.timer.stop()
        if self.server and not self.server.stop():
            event.ignore()
            self.timer.start()
            return
        self.view.stop()
        # Release the page before its off-the-record profile.
        self.view.setPage(QWebEnginePage(self.view))
        event.accept()


def run_desktop(data_dir: Path, smoke_report: Path | None = None):
    # Qt receives only its own arguments, not the application's --data-dir flags.
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Срез")
    app.setOrganizationName("Srez")
    data_dir = data_dir.resolve()
    try:
        (data_dir / "runtime").mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(data_dir / "runtime/desktop.lock"))
        if not lock.tryLock(0):
            QMessageBox.information(None, "Срез уже открыт", "Для этой папки данных уже запущено настольное приложение.")
            return 2
        window = MainWindow(data_dir, smoke_report)
    except Exception as exc:
        if smoke_report:
            smoke_report.parent.mkdir(parents=True, exist_ok=True)
            smoke_report.write_text(json.dumps({"ok": False, "reason": type(exc).__name__}), encoding="utf-8")
        else:
            QMessageBox.critical(None, "Не удалось открыть Срез", "Проверьте доступ к папке данных и настройки .env. Ключи и история должны храниться вне .exe.")
        return 1
    window.show()
    code = app.exec()
    lock.unlock()
    if smoke_report:
        return 0 if window.smoke_success else 1
    return code
