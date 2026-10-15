#!/bin/bash
# Installs Python deps at the start of Claude Code cloud sessions.
# Runs inside the repo checkout (unlike the environment setup script).
set -euo pipefail
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "$CLAUDE_PROJECT_DIR"
command -v uv >/dev/null || pip install --quiet uv
uv sync --quiet
