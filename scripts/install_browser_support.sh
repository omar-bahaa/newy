#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
BROWSER_ENGINE="${BROWSER_ENGINE:-chromium}"
INSTALL_PROJECT="${INSTALL_PROJECT:-1}"
INSTALL_BROWSER="${INSTALL_BROWSER:-1}"

printf '==> Newy browser support setup\n'
printf 'Root: %s\n' "$ROOT_DIR"
printf 'Python bootstrap: %s\n' "$PYTHON_BIN"
printf 'Virtualenv: %s\n' "$VENV_DIR"
printf 'Browser engine: %s\n' "$BROWSER_ENGINE"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  printf '\n==> Creating virtual environment\n'
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$ROOT_DIR/$VENV_DIR/bin/python"
VENV_PIP="$ROOT_DIR/$VENV_DIR/bin/pip"

printf '\n==> Upgrading pip in virtual environment\n'
"$VENV_PYTHON" -m pip install --upgrade pip

if [[ "$INSTALL_PROJECT" == "1" ]]; then
  printf '\n==> Installing Newy with browser extras into %s\n' "$VENV_DIR"
  "$VENV_PIP" install -e ".[browser]"
fi

if [[ "$INSTALL_BROWSER" == "1" ]]; then
  printf '\n==> Installing Playwright browser binary\n'
  "$VENV_PYTHON" -m playwright install "$BROWSER_ENGINE"
fi

printf '\n==> Verifying browser support\n'
"$VENV_PYTHON" - <<'PY'
import importlib
mod = importlib.import_module('playwright.sync_api')
print('playwright import ok:', mod.__name__)
PY

printf '\nDone. Activate with: source %s/bin/activate\n' "$VENV_DIR"
