# Compatibility entry: the canonical spec owns every packaging decision.
from pathlib import Path
_canonical = Path(SPECPATH).resolve().parent / "apps/desktop/ReqPilotX.spec"
SPECPATH = str(_canonical.parent)
exec(compile(_canonical.read_text(encoding="utf-8"), str(_canonical), "exec"))
