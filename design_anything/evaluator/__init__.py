from importlib import import_module
import sys


_canonical = import_module("autodesign.evaluator")
sys.modules[__name__] = _canonical
