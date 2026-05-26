from __future__ import annotations

import os
import sys
import socket
import threading
import time
import subprocess
import atexit
from pathlib import Path

# IMPORTANT: Override storage directory if running inside a PyInstaller frozen bundle.
# This MUST be set before importing any app modules, so db.py and main.py resolve to the persistent path.
if getattr(sys, 'frozen', False):
    if sys.platform == 'win32':
        app_data = Path(os.getenv('APPDATA') or (Path.home() / 'AppData' / 'Roaming'))
        storage_dir = app_data / 'ReqPilotX' / 'storage'
    elif sys.platform == 'darwin':
        storage_dir = Path.home() / 'Library' / 'Application Support' / 'ReqPilotX' / 'storage'
    else:
        storage_dir = Path.home() / '.local' / 'share' / 'ReqPilotX' / 'storage'
    
    storage_dir.mkdir(parents=True, exist_ok=True)
    os.environ["REQPILOT_STORAGE_DIR"] = str(storage_dir)
else:
    # Development mode uses the local prototype/storage directory
    prototype_dir = Path(__file__).resolve().parent
    storage_dir = prototype_dir / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

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
if getattr(sys, 'frozen', False):
    # In packaged mode, discard terminal output and write only to log file
    sys.stdout = DesktopLogger(log_file_path, None)
    sys.stderr = DesktopLogger(log_file_path, None)
else:
    # In dev mode, write to both terminal and log file
    sys.stdout = DesktopLogger(log_file_path, sys.stdout)
    sys.stderr = DesktopLogger(log_file_path, sys.stderr)

print(f"\n--- ReqPilotX Desktop Start: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
if getattr(sys, 'frozen', False):
    print(f"[ReqPilotX] Running in packaged mode. Storage redirected to: {storage_dir}")
else:
    print(f"[ReqPilotX] Running in development mode. Storage: {storage_dir}")

import datetime as dt
class SafeDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        t = dt.datetime.now(tz)
        return cls(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond, t.tzinfo)
    def __sub__(self, other):
        if isinstance(other, dt.datetime):
            self_aware = (self.tzinfo is not None)
            other_aware = (other.tzinfo is not None)
            if self_aware != other_aware:
                if self_aware:
                    other = other.replace(tzinfo=self.tzinfo)
                else:
                    self_dt = dt.datetime(self.year, self.month, self.day, self.hour, self.minute, self.second, self.microsecond, tzinfo=other.tzinfo)
                    return self_dt - other
        return super().__sub__(other)

import app.services.progress_tracker
app.services.progress_tracker.datetime = SafeDatetime

# Obfuscated configuration loading
def deobfuscate_secret(cipher_text: str) -> str:
    try:
        import base64
        decoded = base64.b64decode(cipher_text.encode('utf-8'))
        return bytes([b ^ 42 for b in decoded]).decode('utf-8')
    except Exception:
        return ""

# Put obfuscated key here. If empty, fallback to system environment variables.
OB_DASHSCOPE_API_KEY = ""

# Load API key: environment first, then config.json, then obfuscated key fallback.
has_key = False
if os.environ.get("REQPILOT_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("REQPILOT_ALIYUN_API_KEY"):
    has_key = True

if not has_key:
    config_file = storage_dir / "config.json"
    if config_file.exists():
        try:
            import json
            config_data = json.loads(config_file.read_text(encoding="utf-8"))
            api_key = config_data.get("REQPILOT_DASHSCOPE_API_KEY") or config_data.get("DASHSCOPE_API_KEY")
            if api_key:
                os.environ["REQPILOT_DASHSCOPE_API_KEY"] = api_key
                has_key = True
                print(f"[ReqPilotX] DashScope API key loaded from {config_file}")
        except Exception as e:
            print(f"[ReqPilotX] Warning: Failed to load config.json: {e}")

if not has_key and OB_DASHSCOPE_API_KEY:
    api_key = deobfuscate_secret(OB_DASHSCOPE_API_KEY)
    if api_key:
        os.environ["REQPILOT_DASHSCOPE_API_KEY"] = api_key
        has_key = True
        print("[ReqPilotX] Obfuscated DashScope API key loaded successfully.")

# If we have a key (from any source), default to enabling local Qdrant retrieval for Aliyun POC
if has_key:
    os.environ["REQPILOT_ALIYUN_RETRIEVAL_BACKEND"] = "qdrant"
    if not os.environ.get("REQPILOT_DASHSCOPE_BASE_URL"):
        os.environ["REQPILOT_DASHSCOPE_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"

import uvicorn
import webview
from app.main import app

# Qdrant process reference for cleanup
qdrant_process = None

def find_free_port(start_port: int = 8543) -> int:
    port = start_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                port += 1

def start_backend(port: int) -> None:
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect(('127.0.0.1', port))
            return True
        except OSError:
            return False

def check_qdrant_service() -> bool:
    import urllib.request
    import json
    try:
        req = urllib.request.Request("http://127.0.0.1:6333/")
        with urllib.request.urlopen(req, timeout=1.0) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                return data.get("title") == "qdrant"
    except Exception:
        pass
    return False

def is_server_ready(port: int, timeout: float = 5.0) -> bool:
    start_time = time.time()
    while time.time() - start_time < timeout:
        if is_port_in_use(port):
            return True
        time.sleep(0.1)
    return False

def start_qdrant_if_needed() -> None:
    global qdrant_process
    
    # Check if Qdrant is already running and healthy
    if check_qdrant_service():
        print("[ReqPilotX] Qdrant is already running and healthy on port 6333.")
        os.environ["REQPILOT_QDRANT_URL"] = "http://localhost:6333"
        return
        
    # Check if port 6333 is occupied by another service
    if is_port_in_use(6333):
        print("[ReqPilotX] Warning: Port 6333 is active but not responding as a healthy Qdrant service. Skipping background launch.")
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
            print(f"[ReqPilotX] Warning: Failed to set executable permissions on Qdrant: {e}")
            
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
            creationflags=creationflags
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
            print("[ReqPilotX] Qdrant service is ready and listening on port 6333.")
            os.environ["REQPILOT_QDRANT_URL"] = "http://localhost:6333"
        else:
            print("[ReqPilotX] Warning: Qdrant service did not become healthy within timeout.")
    except Exception as e:
        print(f"[ReqPilotX] Error launching Qdrant process: {e}")

def cleanup_processes() -> None:
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
            
# Register exit handler to clean up Qdrant process on normal exit
atexit.register(cleanup_processes)

def main() -> None:
    # 1. Start Qdrant backend if available
    start_qdrant_if_needed()
    
    # 2. Start FastAPI backend
    port = find_free_port()
    t = threading.Thread(target=start_backend, args=(port,), daemon=True)
    t.start()
    
    # Wait for backend server to be ready
    if not is_server_ready(port):
        print(f"[Error] Backend server failed to start on port {port} within timeout.")
        sys.exit(1)
        
    url = f"http://127.0.0.1:{port}"
    
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
    webview.start()

if __name__ == '__main__':
    main()
