from importlib import import_module
import sys


_canonical = import_module("autodesign.smoke")
main = _canonical.main

if __name__ == "__main__":
    raise SystemExit(main())
if __name__.startswith("design_anything."):
    sys.modules[__name__] = _canonical
