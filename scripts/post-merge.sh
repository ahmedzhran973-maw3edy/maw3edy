#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-.pythonlibs}" \
  uv sync --frozen --no-progress

python -m py_compile main.py