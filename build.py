"""Build the standalone Windows executable. Never includes .env or runtime/."""
import argparse
import os
import shutil
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, default=root / "dist")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Сборка Windows .exe выполняется на Windows.")
    build = root / "build"
    build.mkdir(exist_ok=True)
    # Render the existing SVG mark into an ICO; no external asset downloads.
    from PyQt6.QtSvg import QSvgRenderer
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtCore import Qt
    from PIL import Image
    canvas = QImage(256, 256, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    QSvgRenderer(str(root / "frontend/mark.svg")).render(painter)
    painter.end()
    png, icon = build / "icon.png", build / "srez.ico"
    canvas.save(str(png))
    with Image.open(png) as image:
        image.save(icon, sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
               "--name", "Srez", "--icon", str(icon), "--distpath", str(args.dist_dir.resolve()),
               "--workpath", str(build / "pyinstaller"), "--specpath", str(build),
               "--paths", str(root), "--noupx"]
    resources = [
        ("frontend", "frontend"),
        (".env.example", "."),
        ("examples/stage4/text-floradomspb.json", "examples/stage4"),
        ("examples/stage4/text-optflor.json", "examples/stage4"),
        ("examples/stage4/image-floradomspb.json", "examples/stage4"),
    ]
    for source, destination in resources:
        command += ["--add-data", str(root / source) + ";" + destination]
    for module in ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
                   "uvicorn.lifespan.on"]:
        command += ["--hidden-import", module]
    command += [str(root / "desktop/launcher.py")]
    environment = os.environ.copy()
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    # An inherited toolchain PATH can contribute an incompatible icuuc.dll.
    # Qt uses Windows' ICU; resolve native dependencies against Python and Windows only.
    environment["PATH"] = os.pathsep.join(map(str, [
        Path(sys.executable).parent, Path(sys.base_prefix), windows / "System32", windows,
    ]))
    environment["PYINSTALLER_CONFIG_DIR"] = str(build / "pyinstaller-cache")
    subprocess.run(command, cwd=root, env=environment, check=True)
    shutil.copyfile(root / "desktop/Start-Srez.cmd", args.dist_dir.resolve() / "Start-Srez.cmd")
    print("Executable: " + str(args.dist_dir.resolve() / "Srez.exe"))


if __name__ == "__main__":
    main()
