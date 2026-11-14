"""Docs that must stay in sync: the ticket guide copy, and the README sections other docs link to."""
import json
import re
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


def schema_keys(node: dict, prefix: str = ""):
    """(dotted path, leaf name) for every key the config schema names, `lanes[].x` for array items."""
    for key, sub in (node.get("properties") or {}).items():
        yield prefix + key, key
        if sub.get("type") == "object":
            yield from schema_keys(sub, f"{prefix}{key}.")
        if sub.get("type") == "array" and isinstance(sub.get("items"), dict):
            yield from schema_keys(sub["items"], f"{prefix}{key}[].")


class TestConfigDocs(unittest.TestCase):
    """CLAUDE.md: every config key goes in the schema, the README and examples/config.yml together (W-08)."""

    def setUp(self):
        self.keys = list(schema_keys(json.loads((ROOT / "nightshift/schema/config.schema.json").read_text("utf-8"))))
        self.assertGreater(len(self.keys), 50)

    def test_every_schema_key_is_in_the_readme_config_reference(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("\n## Config reference\n", 1)[1].split("\n## ", 1)[0]
        named = set()
        for token in re.findall(r"`([^`\n]+)`", section):  # `reuse_on_update: true` names reuse_on_update
            named |= {token, re.split(r"[:\s]", token, maxsplit=1)[0]}

        def documented(path: str) -> bool:
            if path in named or path + "[]" in named or any(n.startswith(path + ".") for n in named):
                return True
            parts = path.replace("[]", "").split(".")
            # A row for the top-level key may name its sub-keys by their own (or a shorter dotted) name.
            tails = [".".join(parts[i:]) for i in range(1, len(parts))]
            return any(t in named or any(n.startswith(t + ".") for n in named) for t in tails) and parts[0] in section

        missing = [path for path, _ in self.keys if not documented(path)]
        self.assertEqual(missing, [], "add these keys to the Config reference table in README.md")

    def test_every_schema_key_is_in_the_example_config(self):
        example = (ROOT / "examples/config.yml").read_text(encoding="utf-8")
        # A key counts when it is written as a YAML key, live or commented out (`# key:`, `- key:`).
        written = set(re.findall(r"^[ \t]*(?:#[ \t]*)?(?:-[ \t]+)?([A-Za-z_$][\w$]*):", example, re.M))
        missing = [path for path, leaf in self.keys if leaf not in written]
        self.assertEqual(missing, [], "add these keys to examples/config.yml (commented out if a live value would "
                                      "change behaviour)")


if __name__ == "__main__":
    unittest.main()
