#!/usr/bin/env bash
set -euo pipefail

WORKTREE_ROOT="$(git rev-parse --show-toplevel)"
COMMON_DIR="$(git rev-parse --git-common-dir)"
if [[ "$COMMON_DIR" != /* ]]; then
  COMMON_DIR="$WORKTREE_ROOT/$COMMON_DIR"
fi
COMMON_DIR="$(cd "$COMMON_DIR" && pwd -P)"
HOOK_DIR="$COMMON_DIR/hooks"
HOOK_PATH="$HOOK_DIR/pre-commit"

mkdir -p "$HOOK_DIR"

cat >"$HOOK_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

WORKTREE_ROOT="$(git rev-parse --show-toplevel)"
COMMON_DIR="$(git rev-parse --git-common-dir)"

if [[ "$COMMON_DIR" != /* ]]; then
  COMMON_DIR="$WORKTREE_ROOT/$COMMON_DIR"
fi

COMMON_DIR="$(cd "$COMMON_DIR" && pwd -P)"
if [[ "$(basename "$COMMON_DIR")" == ".git" ]]; then
  COMMON_ROOT="$(cd "$COMMON_DIR/.." && pwd -P)"
else
  COMMON_ROOT="$COMMON_DIR"
fi

python3 "$COMMON_ROOT/scripts/check_context_sync.py" \
  --worktree-root "$WORKTREE_ROOT" \
  --repo-root "$COMMON_ROOT"
EOF

chmod +x "$HOOK_PATH"
echo "Installed pre-commit hook at $HOOK_PATH"
