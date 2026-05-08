"""Application entry point."""

from pathlib import Path
import sys


APP_DIR = Path(__file__).resolve().parent
if APP_DIR.exists():
    sys.path.insert(0, str(APP_DIR))

from app.gui import main


if __name__ == "__main__":
    raise SystemExit(main())
