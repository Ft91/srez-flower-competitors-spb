"""Collect public competitor pages with an isolated, headless Chrome."""
import base64
import json
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.selenium_manager import SeleniumManager
from selenium.webdriver.support.ui import WebDriverWait

from .config import COMPETITORS
from .history import utc_now
from .service import AnalysisError

BROWSER_LOCK = threading.Lock()
WIDTH, HEIGHT, MAX_HEIGHT = 1440, 900, 12000


class ParsingService:
    def __init__(self, settings, driver_factory=None):
        self.settings = settings
        self.driver_factory = driver_factory

    def _driver(self, profile):
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "eager"
        for arg in ["--headless=new", f"--window-size={WIDTH},{HEIGHT}", "--lang=ru-RU",
                    "--disable-notifications", "--no-first-run", "--disable-background-networking",
                    "--disable-sync", "--disable-gpu", "--remote-debugging-pipe", f"--user-data-dir={profile}"]:
            options.add_argument(arg)
        options.add_experimental_option("prefs", {
            "download_restrictions": 3, "profile.default_content_setting_values.notifications": 2,
            "credentials_enable_service": False, "profile.password_manager_enabled": False,
        })
        configured = self.settings.chrome_binary
        candidates = [Path(configured)] if configured else [
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        binary = next((p for p in candidates if p.is_file()), None)
        if not binary:
            raise AnalysisError(503, "browser_missing", "Установите Google Chrome или задайте CHROME_BINARY в .env.")
        options.binary_location = str(binary)
        driver_path = self.settings.chrome_driver
        if not driver_path:
            cache = self.settings.runtime_dir / "selenium-cache"
            cache.mkdir(parents=True, exist_ok=True)
            paths = SeleniumManager().binary_paths([
                "--browser", "chrome", "--browser-path", str(binary),
                "--cache-path", str(cache), "--avoid-stats", "--timeout", "45",
            ])
            driver_path = paths["driver_path"]
        # Hide the driver console on Windows. Chrome itself is headless.
        service = Service(executable_path=driver_path, log_output=os.devnull,
                          popen_kw={"creation_flags": 0x08000000} if os.name == "nt" else {})
        return webdriver.Chrome(service=service, options=options)

    def collect(self, competitor_id):
        if competitor_id not in COMPETITORS:
            raise AnalysisError(422, "unknown_competitor", "Выберите поставщика из /competitors.")
        if not BROWSER_LOCK.acquire(blocking=False):
            raise AnalysisError(409, "browser_busy", "Уже идёт сбор страницы. Дождитесь окончания и повторите.")
        driver = None
        try:
            root = self.settings.runtime_dir / "browser-profiles"
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="capture-", dir=root) as profile:
                try:
                    driver = self.driver_factory(profile) if self.driver_factory else self._driver(profile)
                    driver.set_page_load_timeout(self.settings.page_timeout_seconds)
                    driver.set_script_timeout(20)
                    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                        "width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1, "mobile": False,
                    })
                    driver.get(COMPETITORS[competitor_id]["url"])
                    WebDriverWait(driver, 10).until(lambda d: d.find_element(By.TAG_NAME, "body"))
                    warnings = [
                        "Собрана одна общедоступная страница; каталог, вкладки и ссылки автоматически не открывались.",
                        "Баннеры cookies и выбор города не закрывались; условия для Санкт-Петербурга нужно сверить.",
                    ]
                    # Bounded scrolling loads some lazy content, without clicking/submitting anything.
                    driver.execute_async_script("""
                        const done = arguments[arguments.length - 1];
                        (async () => {
                            for (let n = 0; n < 16; n++) {
                                window.scrollBy(0, 800);
                                await new Promise(resolve => setTimeout(resolve, 180));
                                if (window.scrollY + innerHeight >= document.documentElement.scrollHeight) break;
                            }
                            window.scrollTo(0, 0);
                            await new Promise(resolve => setTimeout(resolve, 700));
                            done(true);
                        })();
                    """)
                    final_url = driver.current_url
                    allowed_hosts = {urlsplit(item["url"]).hostname.removeprefix("www.") for item in COMPETITORS.values()}
                    if (urlsplit(final_url).hostname or "").removeprefix("www.") not in allowed_hosts:
                        raise AnalysisError(502, "unexpected_redirect", "Сайт перенаправил на другой домен. Автоматический анализ остановлен.")
                    text = driver.execute_script("return document.body.innerText").strip()
                    if len(text) < 10:
                        raise AnalysisError(502, "empty_page", "Страница не содержит достаточного текста для анализа.")
                    if len(text) > 500000:
                        raise AnalysisError(413, "page_too_large", "Страница превышает лимит сбора 500 000 символов.")
                    title = driver.title
                    viewport = driver.get_screenshot_as_png()
                    height = int(driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
                    clipped_height = min(height, MAX_HEIGHT)
                    full = base64.b64decode(driver.execute_cdp_cmd("Page.captureScreenshot", {
                        "format": "png", "captureBeyondViewport": True,
                        "clip": {"x": 0, "y": 0, "width": WIDTH, "height": clipped_height, "scale": 1},
                    })["data"])
                    if height > MAX_HEIGHT:
                        warnings.append(f"Полный скриншот ограничен первыми {MAX_HEIGHT} пикселями; текст собран отдельно.")
                    if len(text) < 300:
                        warnings.append("На странице мало текста; часть условий может быть в каталоге или требовать ручной проверки.")
                    blocked = any(marker in (title + "\n" + text[:1500]).lower() for marker in [
                        "verify you are human", "checking your browser", "access denied",
                        "подтвердите, что вы не робот", "доступ ограничен", "just a moment",
                    ])
                    if blocked:
                        warnings.append("Обнаружены признаки страницы защиты. Материал сохранён, анализ ИИ отключён.")
                    meta = {
                        "competitor_id": competitor_id, "name": COMPETITORS[competitor_id]["name"],
                        "requested_url": COMPETITORS[competitor_id]["url"], "url": final_url,
                        "title": title, "captured_at": utc_now(), "text_characters": len(text),
                        "viewport": {"width": WIDTH, "height": HEIGHT},
                        "full_page": {"width": WIDTH, "height": clipped_height, "original_height": height},
                        "browser_version": driver.capabilities.get("browserVersion"),
                        "warnings": warnings, "usable_for_analysis": not blocked,
                    }
                    return meta, {"page.txt": (text.encode("utf-8"), "text/plain"),
                                  "page.json": (json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"),
                                  "viewport.png": (viewport, "image/png"), "full-page.png": (full, "image/png")}
                finally:
                    if driver:
                        try:
                            driver.quit()
                        except WebDriverException:
                            pass
        except AnalysisError:
            raise
        except TimeoutException:
            raise AnalysisError(504, "page_timeout", "Сайт не загрузился за отведённое время. Попробуйте позднее.") from None
        except (WebDriverException, OSError, ValueError, KeyError):
            raise AnalysisError(503, "browser_error", "Не удалось собрать страницу. Проверьте Chrome, драйвер и доступ к сайту.") from None
        finally:
            BROWSER_LOCK.release()
