"""Validate the curated academic identity allowlist.

This is intentionally stdlib-only. It checks the resolver-critical invariants
that JSON Schema alone cannot enforce without adding a runtime dependency.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "assets" / "academic_identity" / "allowlist.json"
ROLE_VALUES = {"venue", "institution", "lab", "company", "society", "publisher", "project", "source"}
URL_FIELDS = ("homepages", "preferred_page_urls", "preferred_asset_urls")


def main() -> int:
    errors: list[str] = []
    data = _load_json(ALLOWLIST_PATH, errors)
    if not isinstance(data, dict):
        errors.append("allowlist root must be an object")
        return _finish(errors)

    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("allowlist.rules must be a non-empty array")
        return _finish(errors)

    seen_ids: set[str] = set()
    seen_aliases: dict[str, str] = {}
    for index, rule in enumerate(rules):
        path = f"rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{path} must be an object")
            continue
        _validate_rule(path, rule, seen_ids, seen_aliases, errors)

    return _finish(errors, rule_count=len(rules))


def _load_json(path: Path, errors: list[str]) -> object:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"failed to read {path}: {type(exc).__name__}: {exc}")
        return None


def _validate_rule(
    path: str,
    rule: dict[str, object],
    seen_ids: set[str],
    seen_aliases: dict[str, str],
    errors: list[str],
) -> None:
    rule_id = str(rule.get("id") or "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", rule_id):
        errors.append(f"{path}.id must be lowercase kebab-case")
    elif rule_id in seen_ids:
        errors.append(f"{path}.id duplicates {rule_id}")
    seen_ids.add(rule_id)

    entity_name = str(rule.get("entity_name") or "").strip()
    if not entity_name:
        errors.append(f"{path}.entity_name is required")

    role = str(rule.get("role") or "")
    if role not in ROLE_VALUES:
        errors.append(f"{path}.role must be one of {sorted(ROLE_VALUES)}")

    aliases = _string_list(rule.get("aliases"))
    if not aliases:
        errors.append(f"{path}.aliases must be a non-empty string array")
    for alias in [entity_name, *aliases]:
        alias_key = _alias_key(alias)
        if not alias_key:
            continue
        prior = seen_aliases.get(alias_key)
        if prior and prior != rule_id and role != "company":
            errors.append(f"{path}.aliases contains '{alias}', already used by {prior}")
        seen_aliases.setdefault(alias_key, rule_id)

    official_domains = _string_list(rule.get("official_domains"))
    if not official_domains:
        errors.append(f"{path}.official_domains must be a non-empty string array")
    for domain in official_domains:
        if "://" in domain or "/" in domain or domain != domain.lower() or not re.fullmatch(r"[a-z0-9.-]+", domain):
            errors.append(f"{path}.official_domains has invalid domain '{domain}'")

    allowed_asset_domains = _string_list(rule.get("allowed_asset_domains"))
    for domain in allowed_asset_domains:
        if "://" in domain or "/" in domain or domain != domain.lower() or not re.fullmatch(r"[a-z0-9.-]+", domain):
            errors.append(f"{path}.allowed_asset_domains has invalid domain '{domain}'")

    try:
        confidence = float(rule.get("confidence", -1))
    except Exception:
        confidence = -1
    if not 0 <= confidence <= 1:
        errors.append(f"{path}.confidence must be between 0 and 1")

    for field in URL_FIELDS:
        urls = _string_list(rule.get(field))
        for url in urls:
            _validate_url(path, field, url, official_domains, allowed_asset_domains, errors)


def _validate_url(
    path: str,
    field: str,
    url: str,
    official_domains: list[str],
    allowed_asset_domains: list[str],
    errors: list[str],
) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        errors.append(f"{path}.{field} must be https URL: {url}")
        return
    host = parsed.hostname.lower()
    allowed_domains = official_domains
    if field == "preferred_asset_urls":
        allowed_domains = [*official_domains, *allowed_asset_domains]
    if not any(_host_matches(host, domain) for domain in allowed_domains):
        errors.append(f"{path}.{field} host '{host}' is outside official/asset domains: {url}")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _host_matches(hostname: str, domain: str) -> bool:
    host = hostname.lower().strip(".")
    dom = domain.lower().strip(".")
    return bool(dom) and (host == dom or host.endswith("." + dom))


def _alias_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _finish(errors: list[str], *, rule_count: int = 0) -> int:
    if errors:
        for error in errors:
            print(f"academic_identity_library: {error}", file=sys.stderr)
        return 1
    print(f"academic_identity_library: ok ({rule_count} rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
