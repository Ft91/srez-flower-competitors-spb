# Desktop host

- launcher.py — точка входа Python / PyInstaller.
- app.py — окно PyQt6, Qt WebEngine, настройки, сохранение файлов.
- runtime.py — локальный сервер и внешняя папка данных.
- settings.py — атомарная запись параметров подключения.
- qa.py — автономная проверка сборки без запросов к поставщикам или моделям.
- requirements.txt — зависимости для desktop и сборщика.

Сборка: `python build.py` из корня проекта. Инструкция: [docs/DESKTOP.md](../docs/DESKTOP.md).
