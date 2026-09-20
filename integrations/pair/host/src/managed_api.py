"""Bind an OS-assigned loopback socket for the Pair-owned API; no worker or model dispatch."""
import json
import os
from pathlib import Path
import socket
import sys

root, descriptor = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(root))
import uvicorn  # noqa: E402

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    descriptor.write_text(json.dumps({"instanceId": os.environ["REQPILOT_MANAGED_INSTANCE"], "pid": os.getpid(), "origin": f"http://127.0.0.1:{listener.getsockname()[1]}"}) + "\n")
    descriptor.chmod(0o600)
    uvicorn.Server(uvicorn.Config("app.main:app", log_level="warning")).run(sockets=[listener])
