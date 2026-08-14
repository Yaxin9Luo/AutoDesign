"""Core editable-video project assembly and supervised HyperFrames rendering."""

from __future__ import annotations

import html
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, BinaryIO, Sequence
from urllib.parse import urlsplit

from .agents.external_author_process import (
    ExternalAuthorProcessRequest,
    run_external_author_process,
)
from .run_control import CancellationToken
from .run_file_access import RunFileAccessError, open_run_file


_COMPOSITION_ID = "editable-video-demo"
_WIDTH = 1920
_HEIGHT = 1080
_HYPERFRAMES_JSON = {
    "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
    "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
    "paths": {
        "blocks": "compositions",
        "components": "compositions/components",
        "assets": "assets",
    },
}


class EditableVideoJobError(RuntimeError):
    """Editable-video assembly or rendering failed with core diagnostics."""

    def __init__(self, code: str, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(message)


def write_editable_video_project(
    *,
    artifact: dict[str, Any],
    runs_dir: Path,
    editor_assets_dir: Path,
    source_run_dir: Path,
    run_id: str,
    run_dir: Path,
    cancellation_token: CancellationToken,
) -> dict[str, Any]:
    """Assemble an editable HyperFrames project using only approved local assets."""

    project_dir = run_dir / "hyperframes-editable-demo"
    token = cancellation_token
    runs_root = Path(runs_dir).absolute()
    source_root = Path(source_run_dir).absolute()
    if not source_root.is_dir() or source_root.parent != runs_root:
        raise EditableVideoJobError(
            "invalid_source_run",
            "editable-video source run is outside the configured runs directory",
        )
    _mkdir(project_dir / "assets", token, "editable_video.mkdir_assets")
    _mkdir(project_dir / "renders", token, "editable_video.mkdir_renders")
    manifest = _editable_video_manifest(
        artifact,
        project_dir=project_dir,
        runs_dir=Path(runs_dir),
        editor_assets_dir=Path(editor_assets_dir),
        source_run_dir=source_root,
        token=token,
    )
    _write_text(
        project_dir / "meta.json",
        json.dumps(
            {"id": _COMPOSITION_ID, "name": "Editable Video Demo", "run_id": run_id},
            indent=2,
            ensure_ascii=False,
        ),
        token,
        "editable_video.write_meta",
    )
    _write_text(
        project_dir / "hyperframes.json",
        json.dumps(_HYPERFRAMES_JSON, indent=2),
        token,
        "editable_video.write_hyperframes_config",
    )
    _write_text(
        project_dir / "scene_manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=False),
        token,
        "editable_video.write_scene_manifest",
    )
    _write_text(
        project_dir / "index.html",
        _editable_video_index_html(manifest),
        token,
        "editable_video.write_index",
    )
    token.raise_if_cancelled("editable_video.project_complete")
    return {"project_dir": str(project_dir), "manifest": manifest}


def run_editable_video_job(
    *,
    artifact: dict[str, Any],
    runs_dir: Path,
    editor_assets_dir: Path,
    source_run_dir: Path,
    run_id: str,
    run_dir: Path,
    cancellation_token: CancellationToken,
    render_command: Sequence[str] | None = None,
    render_timeout_s: float = 300,
) -> dict[str, Any]:
    """Build and render one derived editable-video job inside its own run.

    The exact result keys are ``run_id``, ``project_dir``, ``mp4_path``,
    ``fps``, and ``render``. Transport adapters must normalize them to their
    private/public protocol explicitly.
    """

    token = cancellation_token
    token.raise_if_cancelled("editable_video_job.start")
    assembled = write_editable_video_project(
        artifact=artifact,
        runs_dir=Path(runs_dir),
        editor_assets_dir=Path(editor_assets_dir),
        source_run_dir=Path(source_run_dir),
        run_id=run_id,
        run_dir=Path(run_dir),
        cancellation_token=token,
    )
    project = artifact.get("video_project")
    fps = int(_positive_float(
        project.get("fps") if isinstance(project, dict) else None,
        30,
        min_value=1,
        max_value=120,
    ))
    project_dir = Path(assembled["project_dir"])
    rendered = _render_editable_video(
        project_dir=project_dir,
        run_dir=Path(run_dir),
        run_id=run_id,
        fps=fps,
        token=token,
        render_command=render_command,
        timeout_s=render_timeout_s,
    )
    token.raise_if_cancelled("editable_video_job.complete")
    return {
        "run_id": run_id,
        "project_dir": str(project_dir),
        "mp4_path": str(rendered["mp4_path"]),
        "fps": fps,
        "render": rendered["diagnostics"],
    }


def _render_editable_video(
    *,
    project_dir: Path,
    run_dir: Path,
    run_id: str,
    fps: int,
    token: CancellationToken,
    render_command: Sequence[str] | None,
    timeout_s: float,
) -> dict[str, Any]:
    token.raise_if_cancelled("editable_video.render.prepare")
    _mkdir(project_dir / "renders", token, "editable_video.render.mkdir")
    if render_command is None:
        from .tools.export_video import resolve_hyperframes_binary

        command_prefix = (str(resolve_hyperframes_binary()),)
    else:
        command_prefix = tuple(str(part) for part in render_command)
    if not command_prefix:
        raise EditableVideoJobError("missing_renderer", "HyperFrames render command is empty")

    started_ns = time.time_ns()
    partial = project_dir / "renders" / f".editable-{started_ns}.partial.mp4"
    final = project_dir / "renders" / f"editable-{started_ns}.mp4"
    command = (
        *command_prefix,
        "render",
        "--fps",
        str(fps),
        "--resolution",
        "landscape",
        "--strict",
        "--no-best-effort",
        "--output",
        str(partial.relative_to(project_dir)),
        ".",
    )
    token.raise_if_cancelled("editable_video.render.before_spawn")
    env = _renderer_environment()
    result = run_external_author_process(
        ExternalAuthorProcessRequest(
            run_id=run_id,
            attempt=0,
            command=command,
            cwd=project_dir,
            prompt="",
            timeout_s=max(0.01, float(timeout_s)),
            stdout_path=project_dir / ".render.stdout.log",
            stderr_path=project_dir / ".render.stderr.log",
            env=env,
            poll_interval_s=0.025,
            run_dir=run_dir,
            cancellation_token=token,
        )
    )
    token.raise_if_cancelled("editable_video.render.after_process")
    diagnostics = {
        "status": result.status,
        "reason": result.reason,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "elapsed_s": round(result.elapsed_s, 3),
        "stdout": result.stdout[-1400:],
        "stderr": result.stderr[-900:],
    }
    if result.status != "ok" or result.returncode != 0:
        raise EditableVideoJobError(
            "render_failed",
            (result.stderr or result.stdout or "HyperFrames render failed").strip(),
            diagnostics=diagnostics,
        )
    token.raise_if_cancelled("editable_video.render.before_output_validation")
    try:
        stat = partial.stat()
    except OSError as exc:
        raise EditableVideoJobError(
            "missing_render_output",
            "HyperFrames exited successfully without producing an MP4",
            diagnostics=diagnostics,
        ) from exc
    if stat.st_size <= 0 or stat.st_mtime_ns + 2_000_000_000 < started_ns:
        raise EditableVideoJobError(
            "invalid_render_output",
            "HyperFrames produced an empty or stale MP4",
            diagnostics=diagnostics,
        )
    _promote_with_cancellation_rollback(
        partial,
        final,
        token,
        "editable_video.render.promotion",
    )
    return {"mp4_path": final, "diagnostics": diagnostics}


def _promote_with_cancellation_rollback(
    partial: Path,
    final: Path,
    token: CancellationToken,
    phase: str,
) -> None:
    """Promote a render while restoring the prior public file on cancellation."""

    backup = final.with_name(f".{final.name}.{time.time_ns()}.rollback")
    had_final = final.exists()
    if had_final:
        import shutil

        shutil.copy2(final, backup)
    try:
        token.raise_if_cancelled(f"{phase}.before_replace")
        os.replace(partial, final)
        token.raise_if_cancelled(f"{phase}.after_replace")
    except BaseException:
        if had_final:
            os.replace(backup, final)
        else:
            try:
                final.unlink()
            except FileNotFoundError:
                pass
        raise
    else:
        if had_final:
            backup.unlink()


def _editable_video_manifest(
    artifact: dict[str, Any],
    *,
    project_dir: Path,
    runs_dir: Path,
    editor_assets_dir: Path,
    source_run_dir: Path,
    token: CancellationToken,
) -> dict[str, Any]:
    project = artifact.get("video_project") if isinstance(artifact.get("video_project"), dict) else {}
    raw_scenes = project.get("scenes") if isinstance(project.get("scenes"), list) else []
    raw_layers = artifact.get("layers") if isinstance(artifact.get("layers"), list) else []
    layers = [layer for layer in raw_layers if isinstance(layer, dict)]
    layer_by_id = {
        str(layer.get("layer_id")): layer
        for layer in layers
        if layer.get("layer_id") is not None
    }
    scenes: list[dict[str, Any]] = []
    start_s = 0.0
    for index, raw_scene in enumerate(raw_scenes):
        token.raise_if_cancelled("editable_video.manifest.scene")
        if not isinstance(raw_scene, dict):
            continue
        frame_layer_id = str(raw_scene.get("frame_layer_id") or "")
        frame_layer = layer_by_id.get(frame_layer_id)
        frame_bbox = _editable_layer_bbox(frame_layer)
        if frame_layer is None or frame_bbox is None:
            continue
        duration_s = _positive_float(raw_scene.get("duration_s"), 4.0, min_value=0.5, max_value=120.0)
        scene_layers: list[dict[str, Any]] = []
        for layer in layers:
            token.raise_if_cancelled("editable_video.manifest.layer")
            if layer.get("visible") is False:
                continue
            bbox = _editable_layer_bbox(layer)
            if bbox is None:
                continue
            if str(layer.get("layer_id")) != frame_layer_id:
                if not _bbox_intersects(bbox, frame_bbox):
                    continue
                if layer.get("kind") == "background" and (
                    bbox["w"] > frame_bbox["w"] * 1.05
                    or bbox["h"] > frame_bbox["h"] * 1.05
                ):
                    continue
            scene_layers.append(
                _export_editable_video_layer(
                    layer,
                    bbox,
                    frame_bbox,
                    project_dir=project_dir,
                    runs_dir=runs_dir,
                    editor_assets_dir=editor_assets_dir,
                    source_run_dir=source_run_dir,
                    token=token,
                )
            )
        scenes.append({
            "scene_id": str(raw_scene.get("scene_id") or f"scene-{index + 1}"),
            "name": str(raw_scene.get("name") or f"Scene {index + 1}"),
            "frame_layer_id": frame_layer_id,
            "transition": str(raw_scene.get("transition") or "cut"),
            "track_index": index + 1,
            "start_s": round(start_s, 3),
            "duration_s": round(duration_s, 3),
            "layers": sorted(scene_layers, key=lambda item: float(item.get("z_index") or 0)),
        })
        start_s += duration_s
    if not scenes:
        raise EditableVideoJobError("no_valid_scenes", "editable video has no valid scenes to render")
    fps = int(_positive_float(project.get("fps"), 30, min_value=1, max_value=120))
    return {
        "composition_id": _COMPOSITION_ID,
        "width": _WIDTH,
        "height": _HEIGHT,
        "fps": fps,
        "duration_s": round(start_s, 3),
        "scenes": scenes,
    }


def _export_editable_video_layer(
    layer: dict[str, Any],
    bbox: dict[str, float],
    frame_bbox: dict[str, float],
    *,
    project_dir: Path,
    runs_dir: Path,
    editor_assets_dir: Path,
    source_run_dir: Path,
    token: CancellationToken,
) -> dict[str, Any]:
    sx = _WIDTH / frame_bbox["w"]
    sy = _HEIGHT / frame_bbox["h"]
    scale = min(sx, sy)
    out = dict(layer)
    out["bbox"] = {
        "x": round((bbox["x"] - frame_bbox["x"]) * sx, 3),
        "y": round((bbox["y"] - frame_bbox["y"]) * sy, 3),
        "w": round(bbox["w"] * sx, 3),
        "h": round(bbox["h"] * sy, 3),
    }
    out["z_index"] = float(layer.get("z_index") or 0)
    for key in ("font_size_px", "letter_spacing", "corner_radius", "stroke_width"):
        if isinstance(layer.get(key), (int, float)):
            out[key] = round(float(layer[key]) * scale, 3)
    shadow = layer.get("shadow")
    if isinstance(shadow, dict):
        out["shadow"] = {
            **shadow,
            "dx": round(float(shadow.get("dx") or 0) * scale, 3),
            "dy": round(float(shadow.get("dy") or 0) * scale, 3),
            "blur": round(float(shadow.get("blur") or 0) * scale, 3),
        }
    if isinstance(layer.get("src"), str):
        out["src"] = _stage_editable_video_src(
            layer["src"],
            project_dir=project_dir,
            runs_dir=runs_dir,
            editor_assets_dir=editor_assets_dir,
            source_run_dir=source_run_dir,
            token=token,
        )
    return out


def _stage_editable_video_src(
    src: str,
    *,
    project_dir: Path,
    runs_dir: Path,
    editor_assets_dir: Path,
    source_run_dir: Path,
    token: CancellationToken,
) -> str:
    clean = src.split("?", 1)[0]
    mappings = (
        ("/api/files/editor-assets/", editor_assets_dir, "editor-assets", None),
        ("/api/files/runs/", runs_dir, "runs", source_run_dir.absolute()),
    )
    for prefix, root, folder, authorized_root in mappings:
        if not clean.startswith(prefix):
            continue
        relative = clean[len(prefix):].lstrip("/")
        if authorized_root is None:
            source = (root / relative).resolve()
            if not _path_inside(source, root.resolve()) or not source.is_file():
                raise EditableVideoJobError(
                    "asset_unavailable",
                    "editable video image source is unavailable",
                )
            destination_name = _asset_destination_name(
                source,
                relative=relative,
                token=token,
            )
            destination_dir = project_dir / "assets" / folder
            _mkdir(destination_dir, token, "editable_video.asset.mkdir")
            destination = destination_dir / destination_name
            _copy_file(source, destination, token, "editable_video.asset.copy")
            return f"assets/{folder}/{destination.name}"
        expected_run_id = authorized_root.name
        requested_run_id = relative.split("/", 1)[0]
        if requested_run_id != expected_run_id:
            raise EditableVideoJobError(
                "unauthorized_run_asset",
                "editable video run asset is outside the explicit source run",
            )
        try:
            opened = open_run_file(
                runs_dir,
                relative,
                expected_run_id=expected_run_id,
            )
        except RunFileAccessError as exc:
            raise EditableVideoJobError(
                "asset_unavailable",
                "editable video image source is unavailable",
            ) from exc
        with opened:
            destination_name = _asset_destination_name(
                opened.path,
                relative=relative,
                token=token,
                handle=opened.handle,
            )
            destination_dir = project_dir / "assets" / folder
            _mkdir(destination_dir, token, "editable_video.asset.mkdir")
            destination = destination_dir / destination_name
            opened.handle.seek(0)
            _copy_open_file(
                opened.handle,
                destination,
                token,
                "editable_video.asset.copy",
            )
        return f"assets/{folder}/{destination.name}"
    raise EditableVideoJobError(
        "unapproved_asset_url",
        "editable video image source must use an approved local asset URL",
    )


def _asset_destination_name(
    source: Path,
    *,
    relative: str,
    token: CancellationToken,
    handle: BinaryIO | None = None,
) -> str:
    digest = hashlib.sha256(relative.encode("utf-8", errors="surrogatepass"))
    token.raise_if_cancelled("editable_video.asset.hash.before")
    if handle is None:
        with source.open("rb") as source_handle:
            while chunk := source_handle.read(1024 * 1024):
                token.raise_if_cancelled("editable_video.asset.hash")
                digest.update(chunk)
    else:
        handle.seek(0)
        while chunk := handle.read(1024 * 1024):
            token.raise_if_cancelled("editable_video.asset.hash")
            digest.update(chunk)
    token.raise_if_cancelled("editable_video.asset.hash.after")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-._") or "asset"
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", source.suffix.lower())[:12]
    return f"{safe_stem[:48]}-{digest.hexdigest()[:16]}{safe_suffix}"


def _editable_video_index_html(manifest: dict[str, Any]) -> str:
    scenes_html = "\n".join(_render_scene(scene) for scene in manifest["scenes"])
    timeline = _timeline_script(manifest)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; media-src 'self' data:; connect-src 'none'; font-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Editable Video Demo</title><style>
html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#17130f;font-family:Inter,Arial,sans-serif}}
#root{{position:relative;width:{_WIDTH}px;height:{_HEIGHT}px;overflow:hidden;background:#17130f}}
.scene{{position:absolute;inset:0;width:{_WIDTH}px;height:{_HEIGHT}px;overflow:hidden;opacity:1}}
.layer{{box-sizing:border-box;position:absolute}}.text-layer{{overflow:hidden;white-space:pre-wrap}}img.layer{{display:block}}
</style></head><body>
<div id="root" data-composition-id="{_attr(manifest['composition_id'])}" data-start="0" data-width="{_WIDTH}" data-height="{_HEIGHT}" data-duration="{_css_num(manifest['duration_s'])}">
{scenes_html}
</div><script>
{timeline}
</script></body></html>
"""


def _render_scene(scene: dict[str, Any]) -> str:
    layers = "\n".join(_render_layer(layer) for layer in scene.get("layers", []) if isinstance(layer, dict))
    return (
        f'<section id="{_attr(scene["scene_id"])}" class="clip scene" '
        f'data-start="{_css_num(scene["start_s"])}" data-duration="{_css_num(scene["duration_s"])}" '
        f'data-track-index="{int(scene["track_index"])}">\n{layers}\n</section>'
    )


def _render_layer(layer: dict[str, Any]) -> str:
    kind = str(layer.get("kind") or "shape")
    layer_id = _attr(layer.get("layer_id") or "")
    name = _attr(layer.get("name") or layer.get("layer_id") or "")
    style = _base_layer_style(layer)
    if kind == "text":
        effects = layer.get("effects") if isinstance(layer.get("effects"), dict) else {}
        text_style = [
            style,
            f"font-family:{_safe_font_family(layer.get('font_family'), 'Inter, Arial, sans-serif')}",
            f"font-size:{_css_num(layer.get('font_size_px') or 36)}px",
            f"font-weight:{int(_positive_float(layer.get('font_weight'), 400, min_value=100, max_value=1000))}",
            f"font-style:{'italic' if layer.get('font_style') == 'italic' else 'normal'}",
            f"line-height:{_css_num(layer.get('line_height') or 1.12)}",
            f"letter-spacing:{_css_num(layer.get('letter_spacing') or 0)}px",
            f"text-align:{_safe_choice(layer.get('align'), {'left', 'center', 'right'}, 'left')}",
            f"text-transform:{'uppercase' if layer.get('text_transform') == 'uppercase' else 'none'}",
            f"color:{_safe_css_color(effects.get('fill'), '#17130f')}",
        ]
        return (
            f'<div class="layer text-layer" data-layer-id="{layer_id}" data-name="{name}" '
            f'style="{_attr(";".join(text_style))}">{html.escape(str(layer.get("text") or ""))}</div>'
        )
    if kind == "image":
        position = layer.get("object_position") if isinstance(layer.get("object_position"), dict) else {}
        image_style = [
            style,
            f"object-fit:{_safe_choice(layer.get('fit'), {'cover', 'contain', 'fill'}, 'cover')}",
            f"object-position:{_css_num(_positive_float(position.get('x'), 0.5, min_value=0, max_value=1) * 100)}% "
            f"{_css_num(_positive_float(position.get('y'), 0.5, min_value=0, max_value=1) * 100)}%",
            f"border-radius:{_css_num(layer.get('corner_radius') or 0)}px",
            f"opacity:{_css_num(_positive_float(layer.get('opacity'), 1, min_value=0, max_value=1))}",
            _shadow_style(layer),
        ]
        return (
            f'<img class="layer image-layer" data-layer-id="{layer_id}" data-name="{name}" '
            f'src="{_attr(layer.get("src") or "")}" alt="{name}" '
            f'style="{_attr(";".join(part for part in image_style if part))}">'
        )
    shape_style = [
        style,
        f"background:{_safe_css_color(layer.get('fill_color'), 'transparent')}",
        "border-radius:9999px" if layer.get("shape_kind") == "ellipse" else f"border-radius:{_css_num(layer.get('corner_radius') or 0)}px",
        f"opacity:{_css_num(_positive_float(layer.get('opacity'), 1, min_value=0, max_value=1))}",
        _border_style(layer),
        _shadow_style(layer),
    ]
    return (
        f'<div class="layer shape-layer" data-layer-id="{layer_id}" data-name="{name}" '
        f'style="{_attr(";".join(part for part in shape_style if part))}"></div>'
    )


def _timeline_script(manifest: dict[str, Any]) -> str:
    timings = [
        {
            "id": str(scene["scene_id"]),
            "start": float(scene["start_s"]),
            "end": float(scene["start_s"]) + float(scene["duration_s"]),
        }
        for scene in manifest["scenes"]
    ]
    return "\n".join((
        "window.__timelines=window.__timelines||{};",
        f"const sceneTimings={_js_json(timings)};",
        "const timeline={",
        "seek(time){for(const item of sceneTimings){const node=document.getElementById(item.id);"
        "if(node){node.style.opacity=(time>=item.start&&time<item.end)?'1':'0';}}return this;},",
        f"duration(){{return {_css_num(manifest['duration_s'])};}},pause(){{return this;}}",
        "};",
        f"window.__timelines[{_js_json(str(manifest['composition_id']))}]=timeline;",
        "timeline.seek(0);",
    ))


def _editable_layer_bbox(layer: dict[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(layer, dict) or not isinstance(layer.get("bbox"), dict):
        return None
    raw = layer["bbox"]
    try:
        result = {key: float(raw.get(key)) for key in ("x", "y", "w", "h")}
    except (TypeError, ValueError):
        return None
    return result if result["w"] > 0 and result["h"] > 0 else None


def _bbox_intersects(a: dict[str, float], b: dict[str, float]) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and a["x"] + a["w"] > b["x"]
        and a["y"] < b["y"] + b["h"]
        and a["y"] + a["h"] > b["y"]
    )


def _base_layer_style(layer: dict[str, Any]) -> str:
    bbox = layer["bbox"]
    return ";".join((
        f"left:{_css_num(bbox['x'])}px",
        f"top:{_css_num(bbox['y'])}px",
        f"width:{_css_num(bbox['w'])}px",
        f"height:{_css_num(bbox['h'])}px",
        f"z-index:{int(float(layer.get('z_index') or 0))}",
    ))


def _border_style(layer: dict[str, Any]) -> str:
    width = _positive_float(layer.get("stroke_width"), 0, min_value=0, max_value=1000)
    color = layer.get("stroke_color")
    if width <= 0 or not color:
        return ""
    dash = _safe_choice(layer.get("stroke_dash"), {"solid", "dashed", "dotted"}, "solid")
    return f"border:{_css_num(width)}px {dash} {_safe_css_color(color, '#17130f')}"


def _shadow_style(layer: dict[str, Any]) -> str:
    shadow = layer.get("shadow")
    if not isinstance(shadow, dict):
        return ""
    opacity = _positive_float(shadow.get("opacity"), 0.18, min_value=0, max_value=1)
    return (
        f"box-shadow:{_css_num(shadow.get('dx') or 0)}px {_css_num(shadow.get('dy') or 0)}px "
        f"{_css_num(shadow.get('blur') or 0)}px {_rgba_from_hex(str(shadow.get('color') or '#17130f'), opacity)}"
    )


def _rgba_from_hex(value: str, opacity: float) -> str:
    clean = value.strip().lstrip("#")
    try:
        number = int(clean, 16) if len(clean) == 6 else None
    except ValueError:
        number = None
    if number is None:
        return f"rgba(23,19,15,{_css_num(opacity)})"
    return f"rgba({(number >> 16) & 255},{(number >> 8) & 255},{number & 255},{_css_num(opacity)})"


def _positive_float(value: Any, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _safe_choice(value: Any, allowed: set[str], default: str) -> str:
    result = str(value or "")
    return result if result in allowed else default


_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{3,8}\Z")
_RGB_COLOR = re.compile(
    r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*(0(?:\.\d+)?|1(?:\.0+)?))?\s*\)\Z",
    re.IGNORECASE,
)
_FONT_FAMILY = re.compile(
    r"(?:[A-Za-z0-9 ._-]+|\"[A-Za-z0-9 ._-]+\"|'[A-Za-z0-9 ._-]+')"
    r"(?:\s*,\s*(?:[A-Za-z0-9 ._-]+|\"[A-Za-z0-9 ._-]+\"|'[A-Za-z0-9 ._-]+'))*\Z"
)


def _safe_css_color(value: Any, default: str) -> str:
    result = str(value or "").strip()
    if result.lower() in {"transparent", "currentcolor"} or _HEX_COLOR.fullmatch(result):
        return result
    match = _RGB_COLOR.fullmatch(result)
    if match is not None and all(int(component) <= 255 for component in match.groups()[:3]):
        return result
    return default


def _safe_font_family(value: Any, default: str) -> str:
    result = str(value or "").strip()
    return result if _FONT_FAMILY.fullmatch(result) else default


def _css_num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.3f}".rstrip("0").rstrip(".") or "0"


def _attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _js_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _renderer_environment() -> dict[str, str]:
    """Pass only runtime plumbing; provider credentials never reach HyperFrames."""

    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "NODE_PATH",
        "NPM_CONFIG_PREFIX",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_NODEJS_PATH",
        "CHROME_PATH",
        "FFMPEG_PATH",
        "FFPROBE_PATH",
    }
    env = {
        name: value
        for name, value in os.environ.items()
        if name in allowed and not (_is_proxy_name(name) and _proxy_has_credentials(value))
    }
    env["HYPERFRAMES_PYTHON"] = sys.executable
    return env


def _is_proxy_name(name: str) -> bool:
    return name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}


def _proxy_has_credentials(value: str) -> bool:
    parsed = urlsplit(value if "://" in value else f"//{value}")
    return parsed.username is not None or parsed.password is not None


def _mkdir(path: Path, token: CancellationToken, phase: str) -> None:
    token.raise_if_cancelled(f"{phase}.before")
    path.mkdir(parents=True, exist_ok=True)
    token.raise_if_cancelled(f"{phase}.after")


def _write_text(path: Path, content: str, token: CancellationToken, phase: str) -> None:
    token.raise_if_cancelled(f"{phase}.before")
    path.write_text(content, encoding="utf-8")
    token.raise_if_cancelled(f"{phase}.after")


def _copy_file(source: Path, destination: Path, token: CancellationToken, phase: str) -> None:
    import shutil

    token.raise_if_cancelled(f"{phase}.before")
    shutil.copy2(source, destination)
    token.raise_if_cancelled(f"{phase}.after")


def _copy_open_file(
    source: BinaryIO,
    destination: Path,
    token: CancellationToken,
    phase: str,
) -> None:
    token.raise_if_cancelled(f"{phase}.before")
    with destination.open("wb") as output:
        while chunk := source.read(1024 * 1024):
            token.raise_if_cancelled(phase)
            output.write(chunk)
    token.raise_if_cancelled(f"{phase}.after")
