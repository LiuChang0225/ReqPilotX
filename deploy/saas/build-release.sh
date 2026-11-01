#!/usr/bin/env bash
# Off-server only. Produce an offline, checksummed native release without secrets.
set -euo pipefail
if [[ $# != 2 ]]; then
  echo 'Usage: deploy/saas/build-release.sh <candidate-revision> <output-directory>' >&2
  exit 2
fi
revision=$1
[[ "$revision" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] || { echo 'Invalid revision label' >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
output=$(realpath -m "$2")
[[ ! -e "$output/$revision" ]] || { echo 'Release directory already exists' >&2; exit 2; }
command -v uv >/dev/null || { echo 'Off-server build requires uv from its official distribution' >&2; exit 2; }
command -v npm >/dev/null || { echo 'Off-server frontend build requires Node >=22.12' >&2; exit 2; }
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/reqpilotx-release-uv-cache}"
mkdir -p "$output"
build=$(mktemp -d "$output/.build-$revision.XXXXXX")
trap 'rm -rf "$build"' EXIT
cd "$root"
# Locks are reviewed release inputs; never re-resolve against a mutable index.
python3 - deploy/saas/requirements-runtime.in deploy/saas/requirements-runtime.lock <<'PYLOCK'
import re, sys
from pathlib import Path
locked = dict(re.findall(r'^([a-zA-Z0-9_.-]+)==([^\s\\]+)', Path(sys.argv[2]).read_text(), re.M))
for line in Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    match = re.fullmatch(r'([a-zA-Z0-9_.-]+)(?:\[[^]]+\])?==([^\s]+)', line)
    if not match or locked.get(match[1].lower().replace('_', '-')) != match[2]:
        raise SystemExit('Input/lock mismatch: review the lock before releasing')
PYLOCK
npm ci --no-audit --no-fund
npm run build
uv venv "$build/build-venv" >/dev/null
uv pip install --python "$build/build-venv/bin/python" --require-hashes \
  -r deploy/saas/requirements-build.lock >/dev/null
# The official engine builder requires committed, clean release inputs and
# writes a manifest-backed version. Never replace it with a generic engine wheel.
"$build/build-venv/bin/python" plugins/reqpilot/scripts/build_engine.py \
  --repo "$root" --output "$build/formal-engine"
mkdir -p "$build/wheels"
cp "$build/formal-engine"/*.whl "$build/wheels/"
uv build --wheel --no-build-isolation --python "$build/build-venv/bin/python" \
  --out-dir "$build/wheels" apps/server
"$build/build-venv/bin/python" plugins/reqpilot/scripts/build_bundle.py \
  --repo "$root" --engine-wheel "$(find "$build/formal-engine" -name '*.whl')" \
  --output "$build/capability"
# pip is needed only on the build machine, not a newly downloaded script.
platforms=()
for level in $(seq 28 -1 5); do platforms+=(--platform "manylinux_2_${level}_x86_64"); done
platforms+=(--platform manylinux2014_x86_64 --platform manylinux2010_x86_64 --platform manylinux1_x86_64)
python3 -m pip download --require-hashes --only-binary=:all: \
  --python-version 3.11 --implementation cp --abi cp311 --abi abi3 --abi none \
  "${platforms[@]}" -r deploy/saas/requirements-runtime.lock -d "$build/wheelhouse"
python3 deploy/saas/pack-release.py --revision "$revision" --root "$root" \
  --wheels "$build/wheels" --wheelhouse "$build/wheelhouse" --capability-bundle "$build/capability" --output "$output/$revision"
