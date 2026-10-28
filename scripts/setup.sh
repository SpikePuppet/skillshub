#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/bootstrap.sh"
exec .venv/bin/python -m skillhub setup "$@"
