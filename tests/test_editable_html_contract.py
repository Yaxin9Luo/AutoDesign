from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from autodesign.util.editable_html import ensure_editable_html_contract
from autodesign.util.io import sha256_file
from autodesign.util.layer_parse import parse_html_layers
from scripts import web_server


class EditableHtmlContractTests(unittest.TestCase):
    def test_real_main_deck_root_receives_editable_layers_on_all_18_slides(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "deck.html"
            slides = "".join(
                f"<section class='deck-slide'><h2>Slide {index}</h2>"
                f"<p>Body {index}</p>"
                f"<img src='layers/figure-{index}.png' alt='Figure {index}'></section>"
                for index in range(1, 19)
            )
            html_path.write_text(
                "<!doctype html><html><body>"
                f"<main id='deck' data-slide-count='18'>{slides}</main>"
                "</body></html>",
                encoding="utf-8",
            )

            result = ensure_editable_html_contract(html_path, "deck")
            doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
            root = doc.select_one("main#deck")

            self.assertTrue(result.changed)
            self.assertEqual(result.text_layer_count, 36)
            self.assertEqual(result.image_layer_count, 18)
            self.assertIsNotNone(root)
            assert root is not None
            self.assertEqual(root.get("data-autodesign-artifact-root"), "deck")
            self.assertEqual(len(root.select(".deck-slide")), 18)
            self.assertEqual(len(root.select("[data-layer-id]")), 54)

    def test_real_main_deck_normalization_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(
                "<!doctype html><html><body>"
                "<main id='deck' data-slide-count='1'>"
                "<section class='deck-slide'><h1>Only slide</h1>"
                "<img src='figure.png'></section></main>"
                "</body></html>",
                encoding="utf-8",
            )

            first = ensure_editable_html_contract(html_path, "deck")
            first_text = html_path.read_text(encoding="utf-8")
            second = ensure_editable_html_contract(html_path, "deck")

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(first_text, html_path.read_text(encoding="utf-8"))

    def test_explicit_deck_root_wins_over_main_deck_conventions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(
                "<!doctype html><html><body>"
                "<main id='deck' data-slide-count='1'><section class='deck-slide'>"
                "<h1>Wrong root</h1></section></main>"
                "<article data-autodesign-artifact-root='deck'>"
                "<section class='deck-slide'><h2>Chosen root</h2></section></article>"
                "</body></html>",
                encoding="utf-8",
            )

            ensure_editable_html_contract(html_path, "deck")
            doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

            self.assertIsNone(doc.find("h1").get("data-layer-id"))
            self.assertIsNotNone(doc.find("h2").get("data-layer-id"))

    def test_deck_root_falls_back_to_narrow_common_slide_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(
                "<!doctype html><html><body><main><div class='deck-shell'>"
                "<section class='deck-slide'><h1>One</h1></section>"
                "<section class='deck-slide'><h2>Two</h2></section>"
                "</div><aside><p>Unrelated copy</p></aside></main></body></html>",
                encoding="utf-8",
            )

            ensure_editable_html_contract(html_path, "deck")
            doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
            shell = doc.select_one(".deck-shell")

            self.assertIsNotNone(shell)
            assert shell is not None
            self.assertEqual(shell.get("data-autodesign-artifact-root"), "deck")
            self.assertIsNone(doc.find("aside").get("data-layer-id"))
            self.assertIsNone(doc.find("aside").find("p").get("data-layer-id"))

    def test_deck_normalization_ignores_unrelated_deck_class_and_body_slides(
        self,
    ) -> None:
        fixtures = {
            "unrelated-deck-class": (
                "<!doctype html><html><body><aside class='deck'>"
                "<h1>Navigation</h1></aside></body></html>"
            ),
            "body-is-only-common-ancestor": (
                "<!doctype html><html><body>"
                "<section class='deck-slide'><h1>One</h1></section>"
                "<section class='deck-slide'><h2>Two</h2></section>"
                "</body></html>"
            ),
        }
        for name, source in fixtures.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                html_path = Path(tmp) / "deck.html"
                html_path.write_text(source, encoding="utf-8")

                result = ensure_editable_html_contract(html_path, "deck")

                self.assertFalse(result.changed)
                self.assertEqual(html_path.read_text(encoding="utf-8"), source)

    def test_duplicate_ids_in_real_deck_root_are_repaired_deterministically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(
                "<!doctype html><html><body><main data-slide-count='2'>"
                "<section class='deck-slide'><h1 data-layer-id='shared'>One</h1>"
                "<img data-layer-id='shared' src='one.png'></section>"
                "<section class='deck-slide'><p data-layer-id='shared'>Two</p></section>"
                "</main></body></html>",
                encoding="utf-8",
            )

            ensure_editable_html_contract(html_path, "deck")
            first_text = html_path.read_text(encoding="utf-8")
            layer_ids = [layer["layer_id"] for layer in parse_html_layers(html_path)]
            second = ensure_editable_html_contract(html_path, "deck")

            self.assertEqual(len(layer_ids), 3)
            self.assertEqual(len(layer_ids), len(set(layer_ids)))
            self.assertIn("shared", layer_ids)
            self.assertFalse(second.changed)
            self.assertEqual(first_text, html_path.read_text(encoding="utf-8"))

    def test_external_slides_receive_stable_editable_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(
                """<!doctype html><html><body><main class="deck">
                <section class="deck-slide"><h1 contenteditable="true">Title</h1>
                <figure><img src="layers/figure.png"><figcaption>Evidence</figcaption></figure></section>
                <section class="deck-slide"><h2>Method</h2><p>Body</p></section>
                </main></body></html>""",
                encoding="utf-8",
            )

            first = ensure_editable_html_contract(html_path, "deck")
            first_text = html_path.read_text(encoding="utf-8")
            second = ensure_editable_html_contract(html_path, "deck")

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(first_text, html_path.read_text(encoding="utf-8"))
            self.assertEqual(first.text_layer_count, 4)
            self.assertEqual(first.image_layer_count, 1)
            self.assertIn('data-autodesign-artifact-root="deck"', first_text)
            self.assertIn('data-autodesign-editable="true"', first_text)
            self.assertEqual(len(parse_html_layers(html_path)), 5)

    def test_structured_contenteditable_container_does_not_become_one_destructive_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(
                """<!doctype html><html><body><main class="deck">
                <section class="deck-slide"><div class="two-col" contenteditable="true">
                <p>Editable paragraph</p><div class="callout"><strong>Evidence</strong></div>
                <img src="figure.png"></div></section></main></body></html>""",
                encoding="utf-8",
            )

            ensure_editable_html_contract(html_path, "deck")
            text = html_path.read_text(encoding="utf-8")
            doc = BeautifulSoup(text, "html.parser")

            self.assertNotIn('class="two-col" contenteditable="true" data-layer-id=', text)
            paragraph = doc.find("p")
            self.assertIsNotNone(paragraph)
            assert paragraph is not None
            self.assertEqual(paragraph.get("data-autodesign-editable"), "true")
            self.assertIsNone(paragraph.get("contenteditable"))
            self.assertIn('data-kind="image" data-layer-id="html_image_0001"', text)

    def test_external_landing_text_and_images_become_editable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "index.html"
            html_path.write_text(
                """<!doctype html><html><body><main id="main">
                <nav><a href="#method">Method</a></nav>
                <section><h1>Paper title</h1><p>Summary</p><img src="hero.png"></section>
                <section id="method"><h2>Method</h2><p>Details</p></section>
                </main><script>window.ok = true;</script></body></html>""",
                encoding="utf-8",
            )

            result = ensure_editable_html_contract(html_path, "landing")
            text = html_path.read_text(encoding="utf-8")
            layers = parse_html_layers(html_path)

            self.assertTrue(result.changed)
            self.assertEqual(result.text_layer_count, 5)
            self.assertEqual(result.image_layer_count, 1)
            self.assertIn('data-autodesign-artifact-root="landing"', text)
            self.assertNotIn("contenteditable", text)
            self.assertIn("window.ok = true", text)
            self.assertEqual({layer["kind"] for layer in layers}, {"text", "image"})
            self.assertEqual(len(layers), 6)

    def test_external_video_composition_receives_editable_scene_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "index.html"
            html_path.write_text(
                """<!doctype html><html><body>
                <main data-composition-id="conference-video">
                <section class="clip"><h1>Paper title</h1>
                <p>Scene narration</p><img src="figure.png"></section>
                </main></body></html>""",
                encoding="utf-8",
            )

            first = ensure_editable_html_contract(html_path, "video")
            first_text = html_path.read_text(encoding="utf-8")
            second = ensure_editable_html_contract(html_path, "video")

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertIn('data-autodesign-artifact-root="video"', first_text)
            self.assertEqual(first.text_layer_count, 2)
            self.assertEqual(first.image_layer_count, 1)
            self.assertEqual(len(parse_html_layers(html_path)), 3)

    def test_duplicate_authored_layer_ids_are_repaired_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "index.html"
            html_path.write_text(
                """<!doctype html><html><body><main>
                <h1 data-layer-id="shared">Title</h1>
                <p data-layer-id="shared">Summary</p>
                <img data-layer-id="shared" src="figure.png">
                </main></body></html>""",
                encoding="utf-8",
            )

            ensure_editable_html_contract(html_path, "landing")
            first_text = html_path.read_text(encoding="utf-8")
            first_layers = parse_html_layers(html_path)
            second = ensure_editable_html_contract(html_path, "landing")

            layer_ids = [layer["layer_id"] for layer in first_layers]
            self.assertEqual(len(layer_ids), len(set(layer_ids)))
            self.assertIn("shared", layer_ids)
            self.assertFalse(second.changed)
            self.assertEqual(first_text, html_path.read_text(encoding="utf-8"))

    def test_structured_list_item_exposes_leaf_link_without_claiming_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "index.html"
            html_path.write_text(
                """<!doctype html><html><body><main>
                <ul><li>Read <a href="paper.pdf">the paper</a></li></ul>
                </main></body></html>""",
                encoding="utf-8",
            )

            ensure_editable_html_contract(html_path, "landing")
            doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
            item = doc.find("li")
            link = doc.find("a")

            self.assertIsNotNone(item)
            self.assertIsNotNone(link)
            assert item is not None and link is not None
            self.assertIsNone(item.get("data-layer-id"))
            self.assertEqual(link.get("data-autodesign-editable"), "true")
            self.assertEqual(link.get("href"), "paper.pdf")

    def test_generic_authored_html_edits_patch_only_the_selected_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "index.html"
            dst = root / "edited.html"
            src.write_text(
                """<!doctype html><html><body><main data-autodesign-artifact-root="landing">
                <section><h1 data-layer-id="title" data-kind="text">Old title</h1>
                <div class="keep"><p>Keep this structure</p></div>
                <img data-layer-id="hero" data-kind="image" src="old.png"></section>
                </main></body></html>""",
                encoding="utf-8",
            )

            web_server._patch_html_for_apply_edits(
                src,
                dst,
                {"layers": {
                    "title": {"text": "New title", "font_weight": 700},
                    "hero": {"src": "new.png"},
                }},
            )
            text = dst.read_text(encoding="utf-8")

            self.assertIn(">New title</h1>", text)
            self.assertIn("font-weight:700", text)
            self.assertIn('src="new.png"', text)
            self.assertIn('<div class="keep"><p>Keep this structure</p></div>', text)

    def test_authored_poster_image_replacement_targets_nested_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "poster.html"
            dst = root / "edited.html"
            src.write_text(
                """<!doctype html><html><body><main class="paper-poster">
                <section class="poster-section" data-block-id="results"
                         data-layer-id="ingest_fig_32">
                  <figure data-layer-id="ingest_fig_32">
                    <img data-layer-id="ingest_fig_32" src="old.png"
                         style="object-fit:cover;object-position:20% 30%">
                  </figure>
                </section>
                </main></body></html>""",
                encoding="utf-8",
            )

            web_server._patch_html_for_apply_edits(
                src,
                dst,
                {"layers": {
                    "ingest_fig_32": {
                        "src": "new.png",
                        "bbox": {"x": 30, "y": 40, "w": 640, "h": 360},
                        "flow_offset": {"dx": 12, "dy": -8},
                        "fit": "contain",
                        "object_position": {"x": 0.5, "y": 0.5},
                    },
                }},
            )

            doc = BeautifulSoup(dst.read_text(encoding="utf-8"), "html.parser")
            image = doc.find("img")
            section = doc.find("section")
            self.assertIsNotNone(image)
            self.assertIsNotNone(section)
            assert image is not None and section is not None
            self.assertEqual(image.get("src"), "new.png")
            self.assertIn("object-fit:contain", image.get("style", ""))
            self.assertIn("object-position:50% 50%", image.get("style", ""))
            self.assertIn("left:12px", image.get("style", ""))
            self.assertIn("top:-8px", image.get("style", ""))
            self.assertNotIn("left:", section.get("style", ""))

    def test_authored_poster_identity_text_edit_and_move_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "poster.html"
            dst = root / "edited.html"
            src.write_text(
                """<!doctype html><html><body><main class="paper-poster">
                <header class="poster-header" data-block-id="identity">
                  <h1 data-block-id="heading">Paper title</h1>
                  <p class="authors" data-block-id="authors">Old authors</p>
                  <p class="institutions">Old institution</p>
                </header>
                </main></body></html>""",
                encoding="utf-8",
            )
            institution = next(
                layer
                for layer in parse_html_layers(src)
                if layer.get("text") == "Old institution"
            )

            web_server._patch_html_for_apply_edits(
                src,
                dst,
                {"layers": {
                    institution["layer_id"]: {
                        "text": "New institution",
                        "bbox": {"x": 120, "y": 88, "w": 420, "h": 36},
                        "flow_offset": {"dx": 24, "dy": -6},
                    },
                }},
            )

            doc = BeautifulSoup(dst.read_text(encoding="utf-8"), "html.parser")
            node = doc.select_one(".institutions")
            self.assertIsNotNone(node)
            assert node is not None
            self.assertEqual(node.get_text(strip=True), "New institution")
            style = node.get("style", "")
            self.assertIn("position:relative", style)
            self.assertIn("left:24px", style)
            self.assertIn("top:-6px", style)
            self.assertIn("width:420px", style)
            self.assertIn("height:36px", style)

    def test_web_artifact_response_exposes_authored_deck_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "deck.html").write_text(
                """<!doctype html><html><body><main class="deck">
                <section class="deck-slide"><h1>Title</h1><img src="figure.png"></section>
                <section class="deck-slide"><h2>Method</h2><p>Details</p></section>
                </main></body></html>""",
                encoding="utf-8",
            )

            artifact = web_server._build_artifact_response(
                run_dir,
                "editable-deck",
                "deck",
                baseline_artifact_json=None,
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertEqual(artifact.artifact_type, "deck")
            self.assertEqual(artifact.canvas.w, 1920)
            self.assertEqual(len(artifact.layers), 4)
            self.assertTrue(all(layer["layer_id"] for layer in artifact.layers))

    def test_authored_od_deck_without_native_layers_uses_editable_html_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "deck.html").write_text(
                """<!doctype html><html><body><main class="od-deck">
                <section class="deck-slide"><h1>Title</h1><img src="figure.png"></section>
                <section class="deck-slide"><h2>Method</h2><p>Details</p></section>
                </main></body></html>""",
                encoding="utf-8",
            )

            artifact = web_server._build_artifact_response(
                run_dir,
                "editable-od-deck",
                "deck",
                baseline_artifact_json=None,
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertEqual(len(artifact.layers), 4)
            self.assertEqual(
                {layer["kind"] for layer in artifact.layers},
                {"text", "image"},
            )

    def test_web_artifact_migration_keeps_author_manifest_hash_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            deck_path = final_dir / "deck.html"
            deck_path.write_text(
                """<!doctype html><html><body><main class="deck">
                <section class="deck-slide"><h1>Title</h1></section>
                </main></body></html>""",
                encoding="utf-8",
            )
            manifest_path = final_dir / "slides_author_manifest.json"
            manifest_path.write_text(
                '{"artifact_type":"deck","html_sha256":"stale"}',
                encoding="utf-8",
            )

            web_server._build_artifact_response(
                run_dir,
                "editable-deck",
                "deck",
                baseline_artifact_json=None,
            )

            manifest = web_server._read_json_file(manifest_path)
            self.assertEqual(manifest["html_sha256"], sha256_file(deck_path))
            self.assertEqual(
                sha256_file(final_dir / "slides.html"), sha256_file(deck_path)
            )


if __name__ == "__main__":
    unittest.main()
