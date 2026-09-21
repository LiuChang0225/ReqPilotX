"""Compatibility launcher. Desktop implementation lives in apps/desktop."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "apps/desktop/app_desktop.py"), run_name="__main__")
