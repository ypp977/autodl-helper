#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
DIST_DIR="${PROJECT_ROOT}/dist/nuitka-macos"
OUTPUT_NAME="autodl-helper"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must be run on macOS." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python not found: ${PYTHON_BIN}" >&2
  echo "Create a venv first, or run with PYTHON=/path/to/python ${0}" >&2
  exit 1
fi

cd "${PROJECT_ROOT}"
mkdir -p "${DIST_DIR}"

"${PYTHON_BIN}" -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --output-filename="${OUTPUT_NAME}" \
  --output-dir="${DIST_DIR}" \
  --include-package=autodl_helper \
  --include-data-files="config.example.yaml=config.example.yaml" \
  --include-data-files=".env.template=.env.template" \
  --remove-output \
  main.py

cat <<MSG
Built: ${DIST_DIR}/${OUTPUT_NAME}

Notes:
- This builds a console executable only; it does not create a DMG, installer, GUI app, or code signature.
- Playwright browser binaries are not bundled. Install Chromium in the runtime environment if login/browser flows need it.
MSG
