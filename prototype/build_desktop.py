from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

def main() -> None:
    root_dir = Path(__file__).resolve().parent
    os.chdir(root_dir)
    
    print("[ReqPilotX Build] Starting desktop app packaging...")
    
    # 1. Choose separator for --add-data based on platform
    # Windows uses ';' whereas macOS and Linux use ':'
    if sys.platform == 'win32':
        data_sep = ';'
    else:
        data_sep = ':'
        
    add_data_static = f"app/static{data_sep}app/static"
    add_data_bin = f"bin{data_sep}bin"
    
    # 2. Formulate PyInstaller command arguments
    # We package in directory mode (--onedir) as it is highly robust and launches faster.
    # --noconsole hides the default cmd/terminal window on GUI launch.
    # We explicitly exclude large unused ML packages to dramatically reduce bundle size.
    cmd = [
        "pyinstaller",
        "--name", "ReqPilotX",
        "--onedir",
        "--add-data", add_data_static,
        "--add-data", add_data_bin,
        "--noconsole",
        "--noconfirm", # Overwrite output directory without asking
        "--exclude-module", "torch",
        "--exclude-module", "sentence_transformers",
        "--exclude-module", "transformers",
        "--exclude-module", "tokenizers",
        "--exclude-module", "safetensors",
        "--exclude-module", "scipy",
        "--exclude-module", "sklearn",
        "app_desktop.py"
    ]
    
    # 3. Locate the pyinstaller executable in the local virtual environment
    if sys.platform == 'win32':
        venv_pyinstaller = root_dir / ".venv" / "Scripts" / "pyinstaller.exe"
    else:
        venv_pyinstaller = root_dir / ".venv" / "bin" / "pyinstaller"
        
    if venv_pyinstaller.exists():
        cmd[0] = str(venv_pyinstaller)
    else:
        print("[ReqPilotX Build] Warning: Local .venv pyinstaller not found, using global fallback.")
        cmd[0] = "pyinstaller"
        
    print(f"[ReqPilotX Build] Executing command: {' '.join(cmd)}")
    
    try:
        subprocess.run(cmd, check=True)
        print("\n[ReqPilotX Build] SUCCESS! Packaging completed successfully.")
        print(f"[ReqPilotX Build] Packaged app is located in: {root_dir / 'dist' / 'ReqPilotX'}")
    except subprocess.CalledProcessError as e:
        print(f"\n[ReqPilotX Build] ERROR: PyInstaller failed with exit code {e.returncode}")
        sys.exit(e.returncode)

if __name__ == '__main__':
    main()
