#!/usr/bin/env bash
set -euo pipefail
NOTIFIER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The helper handles one request then exits. Its exclusive lock prevents an
# in-flight request from being replaced; never kill another active reminder.
NOTIFIER_PYTHON="$NOTIFIER_ROOT/.toolchains/cc8/dev-venv/bin/python"
if [[ ! -x "$NOTIFIER_PYTHON" ]]; then
  NOTIFIER_PYTHON="$NOTIFIER_ROOT/.toolchains/cc8/voice-venv/bin/python"
fi
if [[ ! -x "$NOTIFIER_PYTHON" ]]; then
  NOTIFIER_PYTHON="$(command -v python3.13 || true)"
fi
if [[ -z "$NOTIFIER_PYTHON" || ! -x "$NOTIFIER_PYTHON" ]]; then
  echo "Prepare Python 3.13 or the CC8 development environment first." >&2
  exit 2
fi
"$NOTIFIER_PYTHON" "$NOTIFIER_ROOT/cc8" notifications build
"$NOTIFIER_PYTHON" "$NOTIFIER_ROOT/cc8" notifications status --development
