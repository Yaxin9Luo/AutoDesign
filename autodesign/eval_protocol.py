"""Evaluation protocol identity and automatic provenance fingerprints.

This module intentionally depends only on the Python standard library so report
builders can inspect benchmark provenance without importing evaluator runtimes.
"""

from __future__ import annotations

import ast
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


EVAL_PROTOCOL = "posterbench-final"


def fingerprint_files(paths: Iterable[Path], *, namespace: str) -> str:
    """Return a location-independent SHA-256 fingerprint for file contents."""

    resolved = sorted((Path(path).resolve() for path in paths), key=lambda path: path.as_posix())
    if not resolved:
        raise ValueError("at least one fingerprint input is required")
    common_root = Path(os.path.commonpath([str(path.parent) for path in resolved]))
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\0")
    for path in resolved:
        label = _relative_label(path, common_root)
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def fingerprint_installed_distributions(
    distributions: Iterable[str],
    *,
    namespace: str,
) -> str:
    """Fingerprint installed dependency versions without importing the packages."""
    versions: dict[str, str] = {}
    for name in sorted(set(distributions)):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "missing"
    return structured_fingerprint(versions, namespace=namespace)


def fingerprint_python_symbols(
    path: Path,
    symbols: Sequence[str],
    *,
    namespace: str,
) -> str:
    """Fingerprint selected top-level symbols and their same-file dependencies."""

    source_path = Path(path).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    definitions = _top_level_definitions(tree)
    missing = sorted(set(symbols) - definitions.keys())
    if missing:
        raise ValueError(f"missing fingerprint symbols in {source_path}: {', '.join(missing)}")

    selected: set[str] = set()
    pending = list(symbols)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        node = definitions[name]
        dependencies = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and child.id in definitions
        }
        pending.extend(sorted(dependencies - selected))

    payload = [
        (name, ast.dump(definitions[name], annotate_fields=True, include_attributes=False))
        for name in sorted(selected)
    ]
    return structured_fingerprint(payload, namespace=namespace)


def fingerprint_local_python_closure(
    entry_paths: Iterable[Path],
    *,
    package_root: Path,
    namespace: str,
) -> str:
    """Fingerprint entry modules plus all statically imported local modules."""

    root = Path(package_root).resolve()
    pending = [Path(path).resolve() for path in entry_paths]
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        if not path.is_relative_to(root):
            raise ValueError(f"fingerprint entry is outside package root: {path}")
        visited.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module_parts in _local_import_modules(tree, path=path, package_root=root):
            dependency = _resolve_local_module(root, module_parts)
            if dependency is not None and dependency not in visited:
                pending.append(dependency)
    return fingerprint_files(visited, namespace=namespace)


def fingerprint_local_python_symbol_closure(
    entries: Mapping[Path, Sequence[str]],
    *,
    package_root: Path,
    namespace: str,
) -> str:
    """Fingerprint selected symbols and the local symbols they actually import."""

    root = Path(package_root).resolve()
    pending = [
        (Path(path).resolve(), str(symbol))
        for path, symbols in entries.items()
        for symbol in symbols
    ]
    requested: dict[Path, set[str]] = {}
    trees: dict[Path, ast.Module] = {}
    definitions: dict[Path, dict[str, ast.AST]] = {}

    while pending:
        path, symbol = pending.pop()
        if not path.is_relative_to(root):
            raise ValueError(f"fingerprint entry is outside package root: {path}")
        if symbol in requested.setdefault(path, set()):
            continue
        tree = trees.setdefault(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        file_definitions = definitions.setdefault(path, _top_level_definitions(tree))
        import_bindings = _local_import_bindings(tree, path=path, package_root=root)
        if symbol not in file_definitions:
            binding = import_bindings.get(symbol)
            if binding is None or binding[1] is None:
                raise ValueError(f"missing fingerprint symbol in {path}: {symbol}")
            requested[path].add(symbol)
            pending.append(binding)
            continue

        selected = _same_file_symbol_dependencies(file_definitions, [symbol])
        new_symbols = selected - requested[path]
        requested[path].update(new_symbols)
        selected_nodes = [file_definitions[name] for name in new_symbols]
        used_names = {
            child.id
            for node in selected_nodes
            for child in ast.walk(node)
            if isinstance(child, ast.Name)
        }
        for name in used_names:
            binding = import_bindings.get(name)
            if binding is not None and binding[1] is not None:
                pending.append(binding)
        for node in selected_nodes:
            for child in ast.walk(node):
                if not isinstance(child, ast.ImportFrom):
                    continue
                dependency = _resolve_import_from(child, path=path, package_root=root)
                if dependency is None:
                    continue
                for alias in child.names:
                    if alias.name != "*":
                        pending.append((dependency, alias.name))

    payload: list[tuple[str, str, str]] = []
    for path in sorted(requested, key=lambda item: item.as_posix()):
        file_definitions = definitions[path]
        for symbol in sorted(requested[path]):
            node = file_definitions.get(symbol)
            if node is None:
                continue
            payload.append((
                path.relative_to(root).as_posix(),
                symbol,
                ast.dump(node, annotate_fields=True, include_attributes=False),
            ))
    return structured_fingerprint(payload, namespace=namespace)


def combine_fingerprints(*fingerprints: str, namespace: str) -> str:
    return structured_fingerprint(list(fingerprints), namespace=namespace)


def structured_fingerprint(value: Any, *, namespace: str) -> str:
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )
    return f"sha256:{digest.hexdigest()}"


def _relative_label(path: Path, common_root: Path) -> str:
    try:
        return path.relative_to(common_root).as_posix()
    except ValueError:
        return path.name


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    definitions: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _assigned_names(target):
                    definitions[name] = node
    return definitions


def _same_file_symbol_dependencies(
    definitions: Mapping[str, ast.AST],
    symbols: Sequence[str],
) -> set[str]:
    selected: set[str] = set()
    pending = list(symbols)
    while pending:
        name = pending.pop()
        if name in selected or name not in definitions:
            continue
        selected.add(name)
        dependencies = {
            child.id
            for child in ast.walk(definitions[name])
            if isinstance(child, ast.Name) and child.id in definitions
        }
        pending.extend(sorted(dependencies - selected))
    return selected


def _assigned_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_assigned_names(item) for item in node.elts))
    return set()


def _local_import_modules(
    tree: ast.Module,
    *,
    path: Path,
    package_root: Path,
) -> set[tuple[str, ...]]:
    package_name = package_root.name
    relative_file = path.relative_to(package_root)
    current_package = list(relative_file.parent.parts)
    modules: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts and parts[0] == package_name:
                    modules.add(tuple(parts[1:]))
        elif isinstance(node, ast.ImportFrom):
            module_parts = node.module.split(".") if node.module else []
            if node.level:
                trim = node.level - 1
                if trim > len(current_package):
                    continue
                base = current_package[: len(current_package) - trim]
                modules.add(tuple([*base, *module_parts]))
            elif module_parts and module_parts[0] == package_name:
                modules.add(tuple(module_parts[1:]))
    return {parts for parts in modules if parts}


def _local_import_bindings(
    tree: ast.Module,
    *,
    path: Path,
    package_root: Path,
) -> dict[str, tuple[Path, str | None]]:
    bindings: dict[str, tuple[Path, str | None]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            dependency = _resolve_import_from(node, path=path, package_root=package_root)
            if dependency is None:
                continue
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = (dependency, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if not parts or parts[0] != package_root.name:
                    continue
                dependency = _resolve_local_module(package_root, tuple(parts[1:]))
                if dependency is not None:
                    bindings[alias.asname or parts[-1]] = (dependency, None)
    return bindings


def _resolve_import_from(
    node: ast.ImportFrom,
    *,
    path: Path,
    package_root: Path,
) -> Path | None:
    module_parts = node.module.split(".") if node.module else []
    if node.level:
        current_package = list(path.relative_to(package_root).parent.parts)
        trim = node.level - 1
        if trim > len(current_package):
            return None
        parts = tuple([*current_package[: len(current_package) - trim], *module_parts])
    else:
        if not module_parts or module_parts[0] != package_root.name:
            return None
        parts = tuple(module_parts[1:])
    return _resolve_local_module(package_root, parts) if parts else None


def _resolve_local_module(package_root: Path, module_parts: tuple[str, ...]) -> Path | None:
    module_path = package_root.joinpath(*module_parts)
    file_path = module_path.with_suffix(".py")
    if file_path.is_file():
        return file_path.resolve()
    init_path = module_path / "__init__.py"
    if init_path.is_file():
        return init_path.resolve()
    return None


_PACKAGE_ROOT = Path(__file__).resolve().parent
_EVALUATOR_DIR = _PACKAGE_ROOT / "evaluator"
_GOLD_SPEC_PATH = _EVALUATOR_DIR / "assets" / "poster_gold_reference_specs.json"
_EVALUATOR_RUNTIME_DISTRIBUTIONS = (
    "Pillow",
    "PyMuPDF",
    "beautifulsoup4",
    "numpy",
    "onnxruntime",
    "opencv-python",
    "rapidocr-onnxruntime",
)

_EVALUATOR_CODE_FINGERPRINT = fingerprint_local_python_symbol_closure(
    {
        _EVALUATOR_DIR / "quality_rubric.py": [
            "compute_deterministic_report",
            "aggregate_final",
        ],
    },
    package_root=_PACKAGE_ROOT,
    namespace=f"{EVAL_PROTOCOL}:evaluator-code",
)
EVALUATOR_FINGERPRINT = combine_fingerprints(
    _EVALUATOR_CODE_FINGERPRINT,
    fingerprint_files([_GOLD_SPEC_PATH], namespace=f"{EVAL_PROTOCOL}:gold-spec"),
    fingerprint_installed_distributions(
        _EVALUATOR_RUNTIME_DISTRIBUTIONS,
        namespace=f"{EVAL_PROTOCOL}:evaluator-runtime",
    ),
    namespace=f"{EVAL_PROTOCOL}:evaluator",
)

VLM_PROMPT_FINGERPRINT = combine_fingerprints(
    fingerprint_python_symbols(
        _EVALUATOR_DIR / "tools.py",
        ["tool_vlm_judge"],
        namespace=f"{EVAL_PROTOCOL}:vlm-tool",
    ),
    fingerprint_python_symbols(
        _EVALUATOR_DIR / "poster_rubric.py",
        ["DIMENSIONS"],
        namespace=f"{EVAL_PROTOCOL}:vlm-rubric",
    ),
    namespace=f"{EVAL_PROTOCOL}:vlm-prompt",
)
