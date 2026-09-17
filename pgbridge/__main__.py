#!/usr/bin/env python3
"""Entry point: `pgbridge`, `python -m pgbridge`, or `python run.py`."""

import sys


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("Tkinter is missing. On Debian/Ubuntu: sudo apt install python3-tk\n"
              "On Windows and macOS it ships with the python.org installer.",
              file=sys.stderr)
        return 1

    missing = []
    for module, package in (("psycopg2", "psycopg2-binary"), ("pyodbc", "pyodbc")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        print("Missing database drivers: " + ", ".join(missing)
              + "\n  pip install " + " ".join(missing), file=sys.stderr)
        return 1

    # The ODBC driver is an OS package, not a Python one, and its absence
    # otherwise surfaces as "Data source name not found" at connect time.
    import pyodbc
    if not any("SQL Server" in d for d in pyodbc.drivers()):
        print("Warning: no SQL Server ODBC driver is registered.\n"
              "  Ubuntu:  install msodbcsql18 and unixodbc from Microsoft's apt repo\n"
              "  Windows: install 'ODBC Driver 18 for SQL Server' (MSI)",
              file=sys.stderr)

    from pgbridge.app import main as run_app
    run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
