from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path


RUNTIME_SKIP_DIRS = {
    "__pycache__",
    "site-packages",
    "test",
    "tests",
    "idlelib",
    "ensurepip",
    "venv",
}

DEPENDENCY_SKIP_DIRS = {
    "__pycache__",
    "PyInstaller",
    "_pyinstaller_hooks_contrib",
    "pyinstaller-6.20.0.dist-info",
    "pyinstaller_hooks_contrib-2026.5.dist-info",
}

DEPENDENCY_SKIP_SUFFIXES = {".pyc", ".pyo"}


def _remove(path: Path) -> None:
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _ignore_runtime(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in RUNTIME_SKIP_DIRS:
            ignored.add(name)
        elif name.endswith((".pyc", ".pyo")):
            ignored.add(name)
    return ignored


def _ignore_dependencies(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in DEPENDENCY_SKIP_DIRS:
            ignored.add(name)
        elif name.endswith(tuple(DEPENDENCY_SKIP_SUFFIXES)):
            ignored.add(name)
    return ignored


def _copytree(src: Path, dst: Path, *, ignore=None) -> None:
    _remove(dst)
    shutil.copytree(src, dst, ignore=ignore)


def _copy_python_runtime(root_dir: Path, portable_dir: Path) -> None:
    base_python = Path(sys.base_prefix).resolve()
    runtime_dir = portable_dir / "runtime" / "python"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    for name in (
        "python.exe",
        "pythonw.exe",
        "python3.dll",
        f"python{sys.version_info.major}{sys.version_info.minor}.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "LICENSE.txt",
    ):
        src = base_python / name
        if src.exists():
            shutil.copy2(src, runtime_dir / name)

    for name in ("DLLs", "Lib"):
        src = base_python / name
        if src.exists():
            _copytree(src, runtime_dir / name, ignore=_ignore_runtime)

    source_site_packages = Path(sysconfig.get_paths()["purelib"]).resolve()
    target_site_packages = runtime_dir / "Lib" / "site-packages"
    _copytree(source_site_packages, target_site_packages, ignore=_ignore_dependencies)
    _write_python_isolation_files(runtime_dir)


def _write_python_isolation_files(runtime_dir: Path) -> None:
    python_path_entries = "\n".join(
        [
            "../..",
            ".",
            "DLLs",
            "Lib",
            "Lib/site-packages",
            "Lib/site-packages/win32",
            "Lib/site-packages/win32/lib",
            "Lib/site-packages/Pythonwin",
            "",
        ]
    )
    for filename in ("python._pth", f"python{sys.version_info.major}{sys.version_info.minor}._pth"):
        (runtime_dir / filename).write_text(python_path_entries, encoding="utf-8")


def _write_launchers(portable_dir: Path) -> None:
    launcher = portable_dir / "Start ReqPilotX.cmd"
    launcher.write_text(
        "\r\n".join(
            [
                "@echo off",
                "setlocal",
                "cd /d %~dp0",
                "set PYTHONHOME=",
                "set PYTHONPATH=",
                "set PYTHONNOUSERSITE=1",
                "set REQPILOT_PORTABLE_MODE=1",
                "set REQPILOT_STORAGE_DIR=%LOCALAPPDATA%\\ReqPilotX\\storage",
                "if not exist \"%REQPILOT_STORAGE_DIR%\" mkdir \"%REQPILOT_STORAGE_DIR%\"",
                "start \"ReqPilotX\" \"%~dp0runtime\\python\\pythonw.exe\" \"%~dp0app_desktop.py\"",
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )

    console_launcher = portable_dir / "Start ReqPilotX (console).cmd"
    console_launcher.write_text(
        "\r\n".join(
            [
                "@echo off",
                "setlocal",
                "cd /d %~dp0",
                "set PYTHONHOME=",
                "set PYTHONPATH=",
                "set PYTHONNOUSERSITE=1",
                "set REQPILOT_PORTABLE_MODE=1",
                "set REQPILOT_STORAGE_DIR=%LOCALAPPDATA%\\ReqPilotX\\storage",
                "if not exist \"%REQPILOT_STORAGE_DIR%\" mkdir \"%REQPILOT_STORAGE_DIR%\"",
                "\"%~dp0runtime\\python\\python.exe\" \"%~dp0app_desktop.py\"",
                "pause",
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )


def _write_readme(portable_dir: Path) -> None:
    (portable_dir / "README-PORTABLE.txt").write_text(
        "\r\n".join(
            [
                "ReqPilotX 便携测试版",
                "",
                "使用方式：",
                "1. 将整个 ReqPilotX-Portable 文件夹复制或同步到目标电脑本地。",
                "2. 建议放在本地目录，例如 C:\\ReqPilotX-Portable 或桌面上的普通文件夹。",
                "3. 双击 Start ReqPilotX.cmd 启动。",
                "4. 如果窗口没有打开，请运行 Start ReqPilotX (console).cmd，并把控制台输出发给维护人员。",
                "",
                "关于环境：",
                "- 本包已经包含 Python 运行时，路径为 runtime/python，不需要单独安装 Python。",
                "- 便携 Python 已隔离用户级 Python 包，避免同事电脑上的旧依赖覆盖本包依赖。",
                "- 本包已经包含本地 Qdrant，路径为 bin/win/qdrant.exe，不需要安装 Docker。",
                "- 每台电脑的运行数据保存在 %LOCALAPPDATA%\\ReqPilotX\\storage，不会写入共享包目录。",
                "- 导出的 Compliance Matrix 文件保存在 %LOCALAPPDATA%\\ReqPilotX\\storage\\exports，应用内可点击“打开导出目录”直接定位。",
                "- API Key 不随包分发，需要每台测试电脑在应用界面中自行配置。",
                "",
                "关于 OneDrive 分发：",
                "- 可以把 ZIP 或整个 ReqPilotX-Portable 文件夹放在 OneDrive 中共享。",
                "- 同事同步后，请确认文件已完整下载到本地，必要时右键选择“始终保留在此设备上”。",
                "- 更稳妥的方式是：从 OneDrive 复制一份到本机普通目录后再运行。",
                "- 不建议多个人直接在同一个 OneDrive 同步目录里长期运行同一份程序文件夹。",
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )


def main() -> None:
    root_dir = Path(__file__).resolve().parent
    os.chdir(root_dir)

    portable_dir = root_dir / "dist" / "ReqPilotX-Portable"
    _remove(portable_dir)
    portable_dir.mkdir(parents=True, exist_ok=True)

    print(f"[ReqPilotX Portable] Creating {portable_dir}")
    _copytree(root_dir / "app", portable_dir / "app")
    _copytree(root_dir / "bin", portable_dir / "bin")
    shutil.copy2(root_dir / "app_desktop.py", portable_dir / "app_desktop.py")
    shutil.copy2(root_dir / "requirements.txt", portable_dir / "requirements.txt")

    _copy_python_runtime(root_dir, portable_dir)
    _write_launchers(portable_dir)
    _write_readme(portable_dir)

    size_mb = sum(p.stat().st_size for p in portable_dir.rglob("*") if p.is_file()) / 1024 / 1024
    print(f"[ReqPilotX Portable] DONE: {portable_dir}")
    print(f"[ReqPilotX Portable] Size: {size_mb:.2f} MB")


if __name__ == "__main__":
    main()