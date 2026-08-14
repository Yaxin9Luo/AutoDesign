from __future__ import annotations

from importlib import import_module, util
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
from types import ModuleType


_LEGACY_PREFIX = "design_anything."
_CANONICAL_PREFIX = "autodesign."
_COMPAT_ROOT = Path(__file__).resolve().parent
_WRAPPER_PATHS = {
    "design_anything.cli": _COMPAT_ROOT / "cli.py",
    "design_anything.smoke": _COMPAT_ROOT / "smoke.py",
    "design_anything.evaluator.tools": _COMPAT_ROOT / "evaluator" / "tools.py",
}


class _CanonicalAliasLoader(Loader):
    def __init__(self, legacy_name: str, canonical_name: str) -> None:
        self.legacy_name = legacy_name
        self.canonical_name = canonical_name
        self.canonical_attrs: dict[str, object] = {}

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = import_module(self.canonical_name)
        self.canonical_attrs = {
            name: getattr(module, name)
            for name in ("__name__", "__loader__", "__package__", "__spec__", "__file__", "__cached__")
            if hasattr(module, name)
        }
        return module

    def exec_module(self, module: ModuleType) -> None:
        for name, value in self.canonical_attrs.items():
            setattr(module, name, value)
        sys.modules[self.legacy_name] = module


class _LegacyModuleFinder(MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        wrapper_path = _WRAPPER_PATHS.get(fullname)
        if wrapper_path is not None:
            return util.spec_from_file_location(fullname, wrapper_path)
        if not fullname.startswith(_LEGACY_PREFIX) or fullname == "design_anything._compat":
            return None

        canonical_name = _CANONICAL_PREFIX + fullname.removeprefix(_LEGACY_PREFIX)
        try:
            canonical_module = import_module(canonical_name)
        except ModuleNotFoundError as exc:
            if exc.name == canonical_name:
                return None
            raise
        loader = _CanonicalAliasLoader(fullname, canonical_name)
        return ModuleSpec(fullname, loader, is_package=hasattr(canonical_module, "__path__"))


def install() -> None:
    if not any(isinstance(finder, _LegacyModuleFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _LegacyModuleFinder())
