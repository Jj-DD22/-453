#!/bin/zsh
set -euo pipefail
V17_LOCAL_ROOT="$(cd "$(dirname "$0")" && pwd)"
export PATH="/opt/homebrew/Caskroom/miniconda/base/bin:/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"
exec python3 "$V17_LOCAL_ROOT/app/local_launcher.py" "$@"
