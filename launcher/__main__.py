"""Allow ``python -m launcher`` to run the entry point."""
# Vendored from openfox-forge@58e7935 — see NOTICE for the licensing
# and the resynchronisation procedure.


import sys

from .main import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
