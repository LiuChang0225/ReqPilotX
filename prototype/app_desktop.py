from __future__ import annotations

import os
import sys
import socket
import threading
import time
import subprocess
import atexit
import json
import re
import uuid
from pathlib import Path

def _prefer_bundled_runtime_packages() -> None:
    base_dir = Path(__file__).resolve().parent
    runtime_python = base_dir / "runtime" / "python"
    bundled_site_packages = runtime_python / "Lib" / "site-packages"
    if not bundled_site_packages.exists():
        return

    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("PYTHONHOME", None)

    priority_paths = [
        base_dir,
        runtime_python,
        runtime_python / "DLLs",
        runtime_python / "Lib",
        bundled_site_packages,
        bundled_site_packages / "win32",
        bundled_site_packages / "win32" / "lib",
        bundled_site_packages / "Pythonwin",
    ]
    existing_priority_paths = [str(path) for path in priority_paths if path.exists()]
    normalized_priority = {str(Path(path).resolve()).lower() for path in existing_priority_paths}
    runtime_root = runtime_python.resolve()

    filtered_paths: list[str] = []
    for item in sys.path:
        if not item:
            continue
        try:
            resolved = Path(item).resolve()
        except Exception:
            filtered_paths.append(item)
            continue

        normalized = str(resolved).lower()
        if normalized in normalized_priority:
            continue
        if "site-packages" in normalized:
            try:
                resolved.relative_to(runtime_root)
            except ValueError:
                continue
        filtered_paths.append(item)

    sys.path[:] = existing_priority_paths + filtered_paths

    try:
        import pywin32_bootstrap  # type: ignore[import-not-found]
    except Exception:
        pass


_prefer_bundled_runtime_packages()
os.environ["REQPILOT_DESKTOP_EMBEDDED"] = "1"
if getattr(sys, "frozen", False):
    os.environ.setdefault(
        "REQPILOT_FRONTEND_DIR",
        str(Path(sys._MEIPASS) / "frontend" / "dist"),
    )

from reqpilotx_server.infrastructure.local_runtime import (
    LOOPBACK_HOST,
    QDRANT_HTTP_PORT,
    loopback_url,
    probe_local_runtime_readiness,
    prepare_artifact_smoke_storage,
    qdrant_loopback_environment,
    shutdown_runtime_in_order,
    uvicorn_loopback_kwargs,
    wait_for_local_readiness,
)
from reqpilotx_server.runtime_paths import resolve_storage_dir

ARTIFACT_SMOKE_SERVER_FLAG = "--artifact-smoke-server"
ARTIFACT_SMOKE_HANDSHAKE_FLAG = "--artifact-smoke-handshake"
ARTIFACT_SMOKE_NONCE_FLAG = "--artifact-smoke-nonce"
_artifact_smoke_server = ARTIFACT_SMOKE_SERVER_FLAG in sys.argv


def _default_user_storage_dir() -> Path:
    if sys.platform == 'win32':
        app_data = Path(os.getenv('LOCALAPPDATA') or os.getenv('APPDATA') or (Path.home() / 'AppData' / 'Local'))
        return app_data / 'ReqPilotX' / 'storage'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'ReqPilotX' / 'storage'
    return Path.home() / '.local' / 'share' / 'ReqPilotX' / 'storage'


# IMPORTANT: Choose storage before importing any app modules, so db.py and
# main.py resolve to the same persistent path. Packaged and portable builds use
# per-user local storage to avoid file-lock conflicts in synced folders.
configured_storage_dir = os.environ.get("REQPILOT_STORAGE_DIR")
if _artifact_smoke_server:
    raw_storage_dir = str(configured_storage_dir or "").strip()
    try:
        storage_dir = prepare_artifact_smoke_storage(
            Path(raw_storage_dir),
            frozen=bool(getattr(sys, "frozen", False)),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
elif configured_storage_dir:
    storage_dir = Path(configured_storage_dir).expanduser()
elif getattr(sys, 'frozen', False) or os.environ.get("REQPILOT_PORTABLE_MODE") == "1":
    storage_dir = _default_user_storage_dir()
else:
    # Development mode uses the local prototype/storage directory
    prototype_dir = Path(__file__).resolve().parent
    storage_dir = resolve_storage_dir(prototype_dir)

storage_dir.mkdir(parents=True, exist_ok=True)
os.environ["REQPILOT_STORAGE_DIR"] = str(storage_dir)

# Set up logging to app_desktop.log
class DesktopLogger:
    def __init__(self, filename: Path, stream):
        self.log_file = open(filename, "a", encoding="utf-8", buffering=1)
        self.stream = stream

    def write(self, message: str) -> None:
        self.log_file.write(message)
        if self.stream:
            try:
                self.stream.write(message)
            except Exception:
                pass

    def flush(self) -> None:
        self.log_file.flush()
        if self.stream:
            try:
                self.stream.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        if self.stream and hasattr(self.stream, name):
            return getattr(self.stream, name)
        return getattr(self.log_file, name)

log_file_path = storage_dir / "app_desktop.log"
if getattr(sys, 'frozen', False) and not _artifact_smoke_server:
    # In packaged mode, discard terminal output and write only to log file
    sys.stdout = DesktopLogger(log_file_path, None)
    sys.stderr = DesktopLogger(log_file_path, None)
else:
    # In dev mode, write to both terminal and log file
    sys.stdout = DesktopLogger(log_file_path, sys.stdout)
    sys.stderr = DesktopLogger(log_file_path, sys.stderr)

print(f"\n--- ReqPilotX Desktop Start: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
if os.environ.get("REQPILOT_PORTABLE_MODE") == "1":
    print(f"[ReqPilotX] Running in portable mode. Storage redirected to: {storage_dir}")
elif getattr(sys, 'frozen', False):
    print(f"[ReqPilotX] Running in packaged mode. Storage redirected to: {storage_dir}")
else:
    print(f"[ReqPilotX] Running in development mode. Storage: {storage_dir}")

# Load API settings from the environment or the user-owned runtime config.
has_key = False
if os.environ.get("REQPILOT_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("REQPILOT_ALIYUN_API_KEY"):
    has_key = True

config_file = storage_dir / "config.json"
if config_file.exists():
    try:
        import json
        config_data = json.loads(config_file.read_text(encoding="utf-8"))
        for key, val in config_data.items():
            if val:
                if key not in os.environ:
                    os.environ[key] = str(val)
        if os.environ.get("REQPILOT_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("REQPILOT_ALIYUN_API_KEY"):
            has_key = True
            print("[ReqPilotX] Runtime configuration loaded.")
    except Exception as e:
        print(
            "[ReqPilotX] Warning: Failed to load runtime configuration. "
            f"error_type={type(e).__name__}"
        )

# If we have a key (from any source), default to enabling local Qdrant retrieval for Aliyun POC
if has_key:
    os.environ["REQPILOT_ALIYUN_RETRIEVAL_BACKEND"] = "qdrant"
    if not os.environ.get("REQPILOT_DASHSCOPE_BASE_URL"):
        os.environ["REQPILOT_DASHSCOPE_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"

import uvicorn
import webview
from reqpilotx_server.application.runtime import (
    acquire_runtime_storage_lease,
    manage_runtime_storage_lease_from_launcher,
    register_artifact_smoke_shutdown,
    release_runtime_storage_lease,
)
from reqpilotx_server.infrastructure.storage_lock import StorageLockError
from reqpilotx_server.main import app

# Qdrant process reference for cleanup
qdrant_process = None
backend_server = None
backend_thread = None


def bind_loopback_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LOOPBACK_HOST, 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        return listener
    except Exception:
        listener.close()
        raise


def request_backend_shutdown() -> None:
    if backend_server is not None:
        backend_server.should_exit = True


def start_backend(listener: socket.socket) -> bool:
    global backend_server
    host, port = listener.getsockname()[:2]
    if host != LOOPBACK_HOST:
        listener.close()
        raise RuntimeError("ReqPilotX backend listener is not loopback-bound.")
    config = uvicorn.Config(app, **uvicorn_loopback_kwargs(port))
    backend_server = uvicorn.Server(config)
    register_artifact_smoke_shutdown(request_backend_shutdown)
    try:
        backend_server.run(sockets=[listener])
        return bool(backend_server.started)
    finally:
        register_artifact_smoke_shutdown(None)
        listener.close()


def stop_backend(thread: threading.Thread | None = None) -> None:
    request_backend_shutdown()
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
        if thread.is_alive():
            raise RuntimeError("ReqPilotX backend did not stop within 5 seconds.")

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect((LOOPBACK_HOST, port))
            return True
        except OSError:
            return False

def check_qdrant_service() -> bool:
    import urllib.request
    import json
    try:
        req = urllib.request.Request(loopback_url(QDRANT_HTTP_PORT, "/"))
        with urllib.request.urlopen(req, timeout=1.0) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                return "qdrant" in str(data.get("title", "")).lower()
    except Exception:
        pass
    return False

def is_server_ready(port: int, timeout: float = 10.0) -> bool:
    return wait_for_local_readiness(
        port,
        timeout_seconds=timeout,
        probe=probe_local_runtime_readiness,
    )

def start_qdrant_if_needed() -> None:
    global qdrant_process
    
    # Check if Qdrant is already running and healthy
    if check_qdrant_service():
        print(f"[ReqPilotX] Qdrant is already running and healthy on port {QDRANT_HTTP_PORT}.")
        os.environ["REQPILOT_QDRANT_URL"] = loopback_url(QDRANT_HTTP_PORT)
        return
        
    # Check if the local Qdrant port is occupied by another service.
    if is_port_in_use(QDRANT_HTTP_PORT):
        print(
            f"[ReqPilotX] Warning: Port {QDRANT_HTTP_PORT} is active but not "
            "responding as a healthy Qdrant service. Skipping background launch."
        )
        return
        
    # Resolve Qdrant binary path based on runtime context
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys._MEIPASS)
    else:
        base_dir = Path(__file__).resolve().parent
        
    if sys.platform == 'win32':
        qdrant_binary = base_dir / "bin" / "win" / "qdrant.exe"
    elif sys.platform == 'darwin':
        qdrant_binary = base_dir / "bin" / "mac" / "qdrant"
    else:
        # Unsupported platforms for built-in qdrant, skip
        print("[ReqPilotX] Standalone Qdrant binary not available for this platform.")
        return
        
    if not qdrant_binary.exists():
        print(f"[ReqPilotX] Warning: Standalone Qdrant binary not found at {qdrant_binary}. Skipping background launch.")
        return
        
    # Create Qdrant database directory inside the persistent storage path
    qdrant_db_dir = storage_dir / "qdrant_db"
    qdrant_db_dir.mkdir(parents=True, exist_ok=True)
    
    # On macOS/Linux, ensure execution permissions
    if sys.platform != 'win32':
        try:
            os.chmod(qdrant_binary, 0o755)
        except Exception as e:
            print(
                "[ReqPilotX] Warning: Failed to set executable permissions on Qdrant. "
                f"error_type={type(e).__name__}"
            )
            
    print(f"[ReqPilotX] Starting background Qdrant process at {qdrant_binary}...")
    
    creationflags = 0
    if sys.platform == 'win32':
        # subprocess.CREATE_NO_WINDOW = 0x08000000
        creationflags = 0x08000000
        
    try:
        qdrant_process = subprocess.Popen(
            [str(qdrant_binary)],
            cwd=str(qdrant_db_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            env=qdrant_loopback_environment(),
        )
        print("[ReqPilotX] Qdrant process spawned. Waiting for it to become ready...")
        
        # Wait up to 10 seconds for Qdrant to be healthy
        ready = False
        start_time = time.time()
        while time.time() - start_time < 10.0:
            if check_qdrant_service():
                ready = True
                break
            time.sleep(0.2)
            
        if ready:
            print(
                f"[ReqPilotX] Qdrant service is ready and listening on "
                f"port {QDRANT_HTTP_PORT}."
            )
            os.environ["REQPILOT_QDRANT_URL"] = loopback_url(QDRANT_HTTP_PORT)
        else:
            print("[ReqPilotX] Warning: Qdrant service did not become healthy within timeout.")
    except Exception as e:
        print(
            "[ReqPilotX] Error launching Qdrant process. "
            f"error_type={type(e).__name__}"
        )

def stop_qdrant() -> None:
    global qdrant_process
    if qdrant_process and qdrant_process.poll() is None:
        print("[ReqPilotX] Terminating background Qdrant database...")
        qdrant_process.terminate()
        try:
            qdrant_process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            print("[ReqPilotX] Force killing Qdrant database...")
            qdrant_process.kill()
            qdrant_process.wait()


def shutdown_owned_runtime(thread: threading.Thread | None) -> None:
    shutdown_runtime_in_order(
        stop_qdrant=stop_qdrant,
        stop_backend=lambda: stop_backend(thread),
        release_storage=release_runtime_storage_lease,
    )


def cleanup_processes() -> None:
    try:
        shutdown_owned_runtime(backend_thread)
    except Exception as exc:
        print(
            "[ReqPilotX] Runtime cleanup did not complete; storage lease remains held. "
            f"error_type={type(exc).__name__}"
        )
            
# Register exit handler to clean up Qdrant process on normal exit
atexit.register(cleanup_processes)

def _required_argument(flag: str) -> str:
    try:
        return str(sys.argv[sys.argv.index(flag) + 1])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"{flag} requires a value.") from exc


def _write_artifact_smoke_handshake(
    path: Path,
    *,
    nonce: str,
    host: str,
    port: int,
) -> None:
    if not path.is_absolute() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("Artifact smoke handshake path must use an existing absolute directory.")
    if os.path.lexists(path):
        raise ValueError("Artifact smoke handshake path already exists.")
    payload = {
        "pid": os.getpid(),
        "port": int(port),
        "nonce": nonce,
        "host": host,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    global backend_thread
    if _artifact_smoke_server:
        try:
            nonce = _required_argument(ARTIFACT_SMOKE_NONCE_FLAG)
            handshake_path = Path(
                _required_argument(ARTIFACT_SMOKE_HANDSHAKE_FLAG)
            )
            if not re.fullmatch(r"[0-9a-f]{64}", nonce):
                raise ValueError("Artifact smoke nonce must be 64 lowercase hex characters.")
            listener = bind_loopback_socket()
            host, port = listener.getsockname()[:2]
            os.environ["REQPILOT_ARTIFACT_SMOKE_NONCE"] = nonce
            os.environ["REQPILOT_ARTIFACT_SMOKE_HOST"] = str(host)
            os.environ["REQPILOT_ARTIFACT_SMOKE_PORT"] = str(port)
            _write_artifact_smoke_handshake(
                handshake_path,
                nonce=nonce,
                host=str(host),
                port=int(port),
            )
            manage_runtime_storage_lease_from_launcher(True)
            started = start_backend(listener)
        except ValueError as exc:
            print(f"[ReqPilotX] Artifact smoke server rejected: {exc}")
            return 2
        if not started:
            return 1
        release_runtime_storage_lease()
        return 0

    manage_runtime_storage_lease_from_launcher(True)
    try:
        acquire_runtime_storage_lease()
    except StorageLockError as exc:
        print(f"[ReqPilotX] Startup blocked: {exc}")
        return 2

    # 1. Start Qdrant backend if available
    start_qdrant_if_needed()
    
    # 2. Start FastAPI backend
    listener = bind_loopback_socket()
    _host, port = listener.getsockname()[:2]
    t = threading.Thread(target=start_backend, args=(listener,), daemon=True)
    backend_thread = t
    t.start()
    
    # Wait for backend server to be ready
    if not is_server_ready(port):
        print(f"[Error] Backend server failed to start on port {port} within timeout.")
        try:
            shutdown_owned_runtime(t)
        except RuntimeError:
            return 1
        return 1
        
    url = loopback_url(port)
    
    # Create the pywebview native window
    window = webview.create_window(
        title='ReqPilotX - SOR AI Copilot',
        url=url,
        width=1280,
        height=800,
        min_size=(1024, 768),
        text_select=True
    )
    
    def remove_target_blank() -> None:
        # Inject JavaScript to patch target="_blank" for local URLs
        # and override window.open to keep navigation inside the native window.
        js_code = """
        function patchLinks() {
            document.querySelectorAll('a[target="_blank"]').forEach(link => {
                const href = link.getAttribute('href');
                if (href && (href.startsWith('/') || href.includes(window.location.host))) {
                    link.removeAttribute('target');
                }
            });
        }
        
        // Patch links initially
        patchLinks();
        
        // Monitor DOM to patch dynamically loaded links
        const observer = new MutationObserver(patchLinks);
        observer.observe(document.body, { childList: true, subtree: true });
        
        // Override window.open for local navigation
        const originalOpen = window.open;
        window.open = function(url, target, features) {
            if (!url) return originalOpen(url, target, features);
            if (url.startsWith('/') || url.includes(window.location.host)) {
                window.location.href = url;
                return window;
            }
            return originalOpen(url, target, features);
        };
        """
        window.evaluate_js(js_code)
        
    window.events.loaded += remove_target_blank
    
    # Start the native window event loop (this blocks until window is closed)
    try:
        webview.start()
    finally:
        shutdown_owned_runtime(t)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
