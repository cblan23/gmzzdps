import unittest
from pathlib import Path


SITE_JS = Path(__file__).with_name("web") / "gmzz" / "site.js"


class WebSearchLoadingTests(unittest.TestCase):
    def test_character_search_does_not_wait_for_leaderboards(self):
        source = SITE_JS.read_text(encoding="utf-8")
        history_loader = source[
            source.index("async function loadHistoryData"):
            source.index("async function loadCatalogData")
        ]
        history_renderer = source[
            source.index("async function renderHistory"):
            source.index("function renderShare")
        ]

        self.assertNotIn("leaderboards?limit=500", history_loader)
        self.assertNotIn("/catalog", history_loader)
        self.assertIn("loadHistoryData()", history_renderer)
        self.assertNotIn("loadBaseData()", history_renderer)


if __name__ == "__main__":
    unittest.main()
