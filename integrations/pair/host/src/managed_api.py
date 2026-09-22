"""Pair lifecycle adapter for a non-editable installed engine/server environment."""
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys

def installation():
    if importlib.util.find_spec("app") is not None:
        raise RuntimeError("Legacy app namespace must not be visible in the installed runtime")
    result = {}
    for distribution, module in (("reqpilotx-engine", "reqpilotx"), ("reqpilotx-server", "reqpilotx_server")):
        dist = importlib.metadata.distribution(distribution)
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            raise RuntimeError(f"{distribution} must be a non-editable installation")
        spec = importlib.util.find_spec(module)
        root = Path(dist.locate_file("")).resolve()
        if spec is None or not spec.origin or not Path(spec.origin).resolve().is_relative_to(root):
            raise RuntimeError(f"{module} does not resolve to its installed distribution")
        result[distribution] = {"version": dist.version, "module_origin": spec.origin}
    from reqpilotx_server.resource_paths import postgres_migration_dir
    result["migration_count"] = len(list(postgres_migration_dir().glob("*.sql")))
    return result


def configure_environment():
    allowed = {"REQPILOT_DATABASE_URL", "REQPILOT_STORAGE_DIR", "REQPILOT_FRONTEND_DIR",
               "REQPILOT_OBJECT_ROOT", "REQPILOT_SCRATCH_ROOT", "REQPILOT_MANAGED_INSTANCE"}
    for key in list(os.environ):
        if key.startswith(("REQPILOT_", "REQPILOTX_", "OPENAI_", "AZURE_", "DASHSCOPE_", "ALIYUN_")) and key not in allowed:
            del os.environ[key]
    os.environ.update(REQPILOT_ENV="local", REQPILOT_PROCESS_ROLE="api",
                      REQPILOT_AGENT_RUNTIME="none", REQPILOT_OBJECT_STORE="local",
                      REQPILOT_ALLOWED_HOSTS="127.0.0.1,localhost")


def main():
    command = sys.argv[1]
    identity = installation()
    if command == "probe":
        print(json.dumps(identity))
        return
    configure_environment()
    if command == "migrate":
        from reqpilotx_server.infrastructure.runtime_container import build_runtime_container
        from reqpilotx_server.infrastructure.runtime_settings import RuntimeSettings
        container = build_runtime_container(RuntimeSettings.from_environment(), apply_migrations=True)
        try:
            container.database.healthcheck()
        finally:
            container.close()
        print(json.dumps({"migrations": "applied"}))
        return
    if command != "serve":
        raise ValueError("Expected probe, migrate or serve")
    import uvicorn
    descriptor = Path(sys.argv[2]).resolve()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        with descriptor.open("x", encoding="utf-8") as stream:
            os.chmod(descriptor, 0o600)
            json.dump({"instanceId": os.environ["REQPILOT_MANAGED_INSTANCE"], "pid": os.getpid(),
                       "origin": f"http://127.0.0.1:{listener.getsockname()[1]}"}, stream)
        uvicorn.Server(uvicorn.Config("reqpilotx_server.main:app", log_level="warning")).run(sockets=[listener])


if __name__ == "__main__":
    main()
