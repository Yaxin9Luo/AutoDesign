"""Compatibility import for the lightweight evaluation protocol module."""

from ..eval_protocol import (  # noqa: F401
    EVAL_PROTOCOL,
    EVALUATOR_FINGERPRINT,
    VLM_PROMPT_FINGERPRINT,
    combine_fingerprints,
    fingerprint_files,
    fingerprint_local_python_closure,
    fingerprint_local_python_symbol_closure,
    fingerprint_python_symbols,
    structured_fingerprint,
)

__all__ = [
    "EVAL_PROTOCOL",
    "EVALUATOR_FINGERPRINT",
    "VLM_PROMPT_FINGERPRINT",
    "combine_fingerprints",
    "fingerprint_files",
    "fingerprint_local_python_closure",
    "fingerprint_local_python_symbol_closure",
    "fingerprint_python_symbols",
    "structured_fingerprint",
]
