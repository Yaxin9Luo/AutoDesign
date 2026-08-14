"""Compatibility imports for the package renamed to :mod:`autodesign`."""

from importlib import import_module

from ._compat import install


install()

__version__ = import_module("autodesign").__version__


def __getattr__(name: str):
    return getattr(import_module("autodesign"), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(import_module("autodesign"))))
