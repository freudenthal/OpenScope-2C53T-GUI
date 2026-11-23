"""sys.path set-up for the suites, so `python tests/test_x.py` works with no install.

Puts this repository first (openscope_gui). If the `openscope` library is not
installed, it adds the library from the OPENSCOPE_HOST environment variable,
or from the firmware checkout next to this repository
(../OpenScope-2C53T/tools/openscope_host).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import openscope  # noqa: F401
except ImportError:
    host = os.environ.get("OPENSCOPE_HOST") or os.path.join(
        os.path.dirname(ROOT), "OpenScope-2C53T", "tools", "openscope_host")
    if not os.path.isdir(os.path.join(host, "openscope")):
        raise SystemExit(f"openscope library not found at {host}: "
                         "pip install -e <OpenScope-2C53T>/tools/openscope_host, or set OPENSCOPE_HOST")
    sys.path.insert(1, host)
