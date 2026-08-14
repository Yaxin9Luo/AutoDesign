from __future__ import annotations

import inspect
import unittest

from autodesign.util.css_declaration_transform import (
    find_declaration_list_hash_tokens,
    find_stylesheet_hash_tokens,
    transform_declaration_list_values,
    transform_stylesheet_declaration_values,
)


class CssDeclarationTransformTest(unittest.TestCase):
    def test_decodes_escaped_hash_and_name_tokens_for_replacement(self) -> None:
        css = r"""
.panel {
  color: #\41 4113F;
  border-color: #F0D6DF;
  --named-tone: \62 urgundy;
  --unmatched: #\42 4113F;
}
"""
        transformed = transform_stylesheet_declaration_values(
            css,
            {
                "#A4113F": "var(--poster-primary)",
                "#F0D6DF": "var(--poster-secondary)",
                "burgundy": "var(--poster-accent)",
            },
        )

        self.assertIn("color: var(--poster-primary)", transformed)
        self.assertIn("border-color: var(--poster-secondary)", transformed)
        self.assertIn("--named-tone: var(--poster-accent)", transformed)
        self.assertIn(r"--unmatched: #\42 4113F", transformed)
        self.assertIn(
            "color: var(--poster-primary)",
            transform_stylesheet_declaration_values(
                ".panel { color: #\\41\r\n4113F; }",
                {"#A4113F": "var(--poster-primary)"},
            ),
        )

    def test_preserves_selectors_strings_comments_urls_and_escaped_spelling(self) -> None:
        css = r'''
/* marker #A4113F */
[data-tone="#A4113F"]:is(.primary, .secondary) {
  color: #A4113F;
  content: "#A4113F \41 4113F";
  background-image: url(asset-#A4113F.svg);
  mask-image: u\72l(mask-#A4113F.svg);
  --escaped-token: \#A4113F;
}
'''
        transformed = transform_stylesheet_declaration_values(
            css,
            {"#A4113F": "var(--poster-primary)"},
        )

        self.assertIn("/* marker #A4113F */", transformed)
        self.assertIn('[data-tone="#A4113F"]:is(.primary, .secondary)', transformed)
        self.assertIn('content: "#A4113F \\41 4113F"', transformed)
        self.assertIn("url(asset-#A4113F.svg)", transformed)
        self.assertIn(r"u\72l(mask-#A4113F.svg)", transformed)
        self.assertIn(r"--escaped-token: \#A4113F", transformed)
        self.assertEqual(transformed.count("var(--poster-primary)"), 1)
        self.assertEqual(
            transform_stylesheet_declaration_values(css, {"#000000": "black"}),
            css,
        )

    def test_supports_pseudo_class_nesting_and_descriptor_at_rules(self) -> None:
        css = r"""
@media (min-width: 40rem) {
  .panel {
    color: #A4113F;
    &:hover {
      border-color: #F0D6DF;
    }
    @supports (display: grid) {
      & > .child:is(:first-child, :last-child) {
        outline-color: #A4113F;
      }
    }
  }
}
@font-palette-values --Poster {
  font-family: Poster;
  base-palette: 1;
  override-colors: 0 #A4113F;
}
@page {
  @top-left { color: #F0D6DF; }
}
"""
        transformed = transform_stylesheet_declaration_values(
            css,
            {
                "#A4113F": "var(--poster-primary)",
                "#F0D6DF": "var(--poster-secondary)",
            },
        )

        self.assertIn("&:hover {", transformed)
        self.assertIn("& > .child:is(:first-child, :last-child) {", transformed)
        self.assertIn("@font-palette-values --Poster {", transformed)
        self.assertIn("override-colors: 0 var(--poster-primary)", transformed)
        self.assertIn("@top-left { color: var(--poster-secondary); }", transformed)
        self.assertEqual(transformed.count("var(--poster-primary)"), 3)
        self.assertEqual(transformed.count("var(--poster-secondary)"), 2)

    def test_transforms_inline_declarations_and_custom_property_blocks(self) -> None:
        declarations = r'''
color: #\41 4113F;
--theme: { accent: #F0D6DF; nested: var(--fallback, #A4113F); };
content: "#A4113F";
background: url("#A4113F");
'''
        transformed = transform_declaration_list_values(
            declarations,
            {
                "#A4113F": "var(--poster-primary)",
                "#F0D6DF": "var(--poster-secondary)",
            },
        )

        self.assertIn("color: var(--poster-primary)", transformed)
        self.assertIn("accent: var(--poster-secondary)", transformed)
        self.assertIn("nested: var(--fallback, var(--poster-primary))", transformed)
        self.assertIn('content: "#A4113F"', transformed)
        self.assertIn('url("#A4113F")', transformed)

    def test_finds_semantic_hash_tokens_outside_strings_comments_and_urls(self) -> None:
        css = r'''
#\41 4113F[data-tone="#F0D6DF"] {
  color: #\41 4113F;
  content: "#\46 0D6DF";
  background: url(asset-#A4113F.svg);
  /* #F0D6DF */
}
'''
        self.assertEqual(
            find_stylesheet_hash_tokens(css),
            {"#A4113F"},
        )
        self.assertEqual(
            find_declaration_list_hash_tokens(r"color: #\41 4113F;"),
            {"#A4113F"},
        )

    def test_rejects_malformed_css_and_excessive_nesting_with_offsets(self) -> None:
        malformed = (
            ".panel { color: #A4113F;",
            '.panel { content: "unterminated; }',
            ".panel { color: var(--tone, #A4113F; }",
            ".panel { color: #A4113F; /* unterminated comment }",
        )
        for css in malformed:
            with self.subTest(css=css):
                with self.assertRaisesRegex(ValueError, r"malformed CSS at offset \d+"):
                    transform_stylesheet_declaration_values(css, {})

        nested = ".root {" + "&:hover {" * 8 + "color: #A4113F;" + "}" * 9
        with self.assertRaisesRegex(
            ValueError,
            r"malformed CSS at offset \d+: maximum nesting depth 4 exceeded",
        ):
            transform_stylesheet_declaration_values(
                nested,
                {},
                max_nesting_depth=4,
            )

    def test_utility_has_no_node_web_or_subprocess_dependency(self) -> None:
        import autodesign.util.css_declaration_transform as css_transform

        source = inspect.getsource(css_transform).casefold()
        self.assertNotIn("subprocess", source)
        self.assertNotIn("postcss", source)
        self.assertNotIn("node", source)
        self.assertNotIn("node_modules", source)
        self.assertNotIn(' / "web"', source)


if __name__ == "__main__":
    unittest.main()
