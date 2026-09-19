"""Source and frozen executable entry point."""
import argparse
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.runtime import default_data_dir


def main():
    parser = argparse.ArgumentParser(description="Срез — настольное приложение")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--self-test", type=Path, metavar="REPORT_JSON")
    parser.add_argument("--ui-smoke", type=Path, metavar="REPORT_JSON")
    args = parser.parse_args()
    if args.self_test:
        from desktop.qa import self_test
        return self_test(args.self_test.resolve())
    from desktop.app import run_desktop
    return run_desktop(args.data_dir or default_data_dir(),
                       args.ui_smoke.resolve() if args.ui_smoke else None)


if __name__ == "__main__":
    raise SystemExit(main())
