#!/bin/sh
# Refuge launcher (Linux/macOS). First run may need: chmod +x run.sh
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Refuge needs Python 3 (with tkinter). Install it, e.g.:"
    echo "  Debian/Ubuntu: sudo apt install python3 python3-tk"
    echo "  Fedora:        sudo dnf install python3 python3-tkinter"
    exit 1
fi

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "Python is installed but tkinter is missing. Install it, e.g.:"
    echo "  Debian/Ubuntu: sudo apt install python3-tk"
    echo "  Fedora:        sudo dnf install python3-tkinter"
    exit 1
fi

exec "$PY" run.py
