"""Deterministic edit metadata for authored Landing and Slides HTML."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup, Tag


EditableArtifactType = Literal["landing", "deck", "video"]

_SEMANTIC_TEXT_SELECTOR = ",".join(
    (
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "li",
        "figcaption",
        "blockquote",
        "td",
        "th",
    )
)


@dataclass(frozen=True)
class EditableHtmlContractResult:
    changed: bool
    text_layer_count: int
    image_layer_count: int


def ensure_editable_html_contract(
    html_path: Path,
    artifact_type: EditableArtifactType,
) -> EditableHtmlContractResult:
    """Add stable edit IDs without changing the authored visual layout."""

    html_path = Path(html_path)
    doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    root = _artifact_root(doc, artifact_type)
    if root is None:
        return EditableHtmlContractResult(False, 0, 0)

    changed = _set_attr(root, "data-autodesign-artifact-root", artifact_type)
    text_count = 0
    image_count = 0
    used_layer_ids: set[str] = set()
    semantic_nodes = [
        node
        for node in root.select(_SEMANTIC_TEXT_SELECTOR)
        if isinstance(node, Tag)
        and not _excluded(node)
        and node.find(True, recursive=False) is None
    ]
    semantic_node_ids = {id(node) for node in semantic_nodes}
    leaf_authored_nodes = [
        node
        for node in root.select('[contenteditable="true"]')
        if isinstance(node, Tag)
        and not _excluded(node)
        and not node.select_one(_SEMANTIC_TEXT_SELECTOR)
        and node.find(True, recursive=False) is None
    ]
    leaf_text_nodes = [
        node
        for node in root.find_all(True)
        if isinstance(node, Tag)
        and not _excluded(node)
        and node.find(True, recursive=False) is None
        and node.get_text(" ", strip=True)
        and not any(id(parent) in semantic_node_ids for parent in node.parents)
    ]
    candidates = sorted(
        {
            id(node): node
            for node in (*semantic_nodes, *leaf_authored_nodes, *leaf_text_nodes)
        }.values(),
        key=_document_order_key,
    )
    image_nodes = [
        node
        for node in root.select("img")
        if isinstance(node, Tag) and not _excluded(node)
    ]
    candidate_ids = {id(node) for node in (*candidates, *image_nodes)}
    for node in root.select("[data-layer-id]"):
        if not isinstance(node, Tag) or id(node) in candidate_ids:
            continue
        existing = str(node.get("data-layer-id") or "").strip()
        if existing:
            used_layer_ids.add(existing)

    for node in candidates:
        if not isinstance(node, Tag) or _excluded(node):
            continue
        if not node.get_text(" ", strip=True):
            continue
        text_count += 1
        changed |= _set_attr(node, "data-autodesign-editable", "true")
        changed |= _set_attr(node, "data-kind", "text")
        changed |= _claim_layer_id(
            node,
            prefix="html_text",
            sequence=text_count,
            used_layer_ids=used_layer_ids,
        )

    for node in image_nodes:
        image_count += 1
        changed |= _set_attr(node, "data-autodesign-editable", "true")
        changed |= _set_attr(node, "data-kind", "image")
        changed |= _claim_layer_id(
            node,
            prefix="html_image",
            sequence=image_count,
            used_layer_ids=used_layer_ids,
        )
        if not node.get("data-layer-name"):
            node["data-layer-name"] = str(
                node.get("alt") or node.get("data-source-id") or f"Image {image_count}"
            )
            changed = True

    if changed:
        html_path.write_text(str(doc), encoding="utf-8")
    return EditableHtmlContractResult(changed, text_count, image_count)


def _artifact_root(doc: BeautifulSoup, artifact_type: EditableArtifactType) -> Tag | None:
    if artifact_type == "deck":
        return find_deck_artifact_root(doc)
    elif artifact_type == "video":
        node = doc.select_one("[data-composition-id]") or doc.find("main") or doc.body
    else:
        node = doc.find("main") or doc.body
    return node if isinstance(node, Tag) else None


def find_deck_artifact_root(doc: BeautifulSoup) -> Tag | None:
    for selector in (
        "[data-autodesign-artifact-root]",
        "main#deck",
        "main[data-slide-count]",
    ):
        node = doc.select_one(selector)
        if isinstance(node, Tag) and node.name not in {"html", "body"}:
            return node
    return _narrow_deck_slide_ancestor(doc)


def _narrow_deck_slide_ancestor(doc: BeautifulSoup) -> Tag | None:
    slides = [
        node for node in doc.select(".deck-slide") if isinstance(node, Tag)
    ]
    if not slides:
        return None
    candidate = slides[0].parent
    while isinstance(candidate, Tag):
        if candidate.name in {"html", "body"}:
            return None
        if all(candidate in slide.parents for slide in slides):
            return candidate
        candidate = candidate.parent
    return None


def _excluded(node: Tag) -> bool:
    return any(
        isinstance(parent, Tag) and parent.name in {"script", "style", "svg", "noscript"}
        for parent in (node, *node.parents)
    )


def _document_order_key(node: Tag) -> int:
    root = node
    while isinstance(root.parent, Tag):
        root = root.parent
    for index, candidate in enumerate(root.descendants):
        if candidate is node:
            return index
    return 0


def _set_attr(node: Tag, name: str, value: str) -> bool:
    if str(node.get(name) or "") == value:
        return False
    node[name] = value
    return True


def _claim_layer_id(
    node: Tag,
    *,
    prefix: str,
    sequence: int,
    used_layer_ids: set[str],
) -> bool:
    existing = str(node.get("data-layer-id") or "").strip()
    if existing and existing not in used_layer_ids:
        used_layer_ids.add(existing)
        return False

    suffix = max(1, sequence)
    candidate = f"{prefix}_{suffix:04d}"
    while candidate in used_layer_ids:
        suffix += 1
        candidate = f"{prefix}_{suffix:04d}"
    used_layer_ids.add(candidate)
    if existing == candidate:
        return False
    node["data-layer-id"] = candidate
    return True
