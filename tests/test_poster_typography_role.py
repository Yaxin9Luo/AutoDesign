from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from autodesign.tools.propose_paper_poster_html import _paper_typography_role


class PosterTypographyRoleTests(unittest.TestCase):
    def test_subsection_heading_is_not_classified_as_section_heading(self) -> None:
        soup = BeautifulSoup(
            '<p class="subsection-heading" data-style-role="subsection-heading">'
            "How It Works"
            "</p>",
            "html.parser",
        )

        self.assertEqual(
            _paper_typography_role(soup.p, "text"),
            "subsection_heading",
        )


if __name__ == "__main__":
    unittest.main()
