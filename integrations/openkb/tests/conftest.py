from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[3]
sys.dont_write_bytecode = True
sys.path[:0] = [str(REPO / "packages/engine/src"), str(REPO / "integrations/openkb/src"),
    str(REPO / "plugins/reqpilot/scripts"), str(REPO / "plugins/reqpilot/tests")]
