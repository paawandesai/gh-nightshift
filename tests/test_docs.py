"""Docs that must stay in sync: the ticket guide copy, and the README sections other docs link to."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestDocs(unittest.TestCase):
    def test_ticket_guide_copy_matches_template(self):
        doc = (ROOT / "docs" / "NIGHT_SHIFT_ISSUES.md").read_bytes()
        template = (ROOT / "nightshift" / "templates" / "NIGHT_SHIFT_ISSUES.md").read_bytes()
        self.assertEqual(doc, template, "docs/NIGHT_SHIFT_ISSUES.md must equal nightshift/templates/NIGHT_SHIFT_ISSUES.md")

    def test_readme_has_results_section(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("\n## Results from the first real run\n", readme)

    def test_readme_has_mermaid_diagram(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("```mermaid\n", readme)


if __name__ == "__main__":
    unittest.main()
