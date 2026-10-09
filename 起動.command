#!/bin/bash
cd "$(dirname "$0")" || exit 1

for candidate in \
  "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" \
  /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    exec "$candidate" launch.py "$@"
  fi
done

echo 'Python 3.11以上が見つかりません。対応するPythonを用意してから再度開いてください。'
read -r -p 'Enterで閉じます。'
exit 1
