"""Config file: the YAML subset, the JSON Schema and the validator."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nightshift import schema, yamlish  # noqa: E402
from nightshift.config import BUILTIN_PROTECTED, ConfigError, loads  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
MINIMAL = "version: 1\nlanes:\n  - name: api\n    claude_token_secret: CLAUDE_TOKEN_A\n"


class Yaml(unittest.TestCase):
    def test_subset(self):
        text = ("a: 1\nb: 'x # y'  # c\nc: [1, \"two\", three]\nd:\n  - k: v\n    n: 2\n  - plain\n"
                "e: |\n  line 1\n\n  # kept\n  line 3\nf: {x: 1, y: no-quote}\ng: ~\nh: true\n")
        self.assertEqual(yamlish.loads(text), {
            "a": 1, "b": "x # y", "c": [1, "two", "three"], "d": [{"k": "v", "n": 2}, "plain"],
            "e": "line 1\n\n# kept\nline 3\n", "f": {"x": 1, "y": "no-quote"}, "g": None, "h": True})

    def test_round_trip(self):
        data = yamlish.loads(open(os.path.join(ROOT, "examples/config.yml")).read())
        self.assertEqual(yamlish.loads(yamlish.dumps(data)), data)

    def test_rejects_what_it_cannot_parse(self):
        for bad in ("a: &x 1", "a:\n\tb: 1", "a: 1\na: 2", "- a\nb: 1", "a: [1, [2]]", "---\na: 1\n---\nb: 2"):
            with self.subTest(bad=bad), self.assertRaises(yamlish.YAMLError):
                yamlish.loads(bad)


class Schema(unittest.TestCase):
    def test_example_is_valid(self):
        cfg = loads(open(os.path.join(ROOT, "examples/config.yml")).read())
        self.assertEqual(cfg.lane_names, ["api", "web"])
        self.assertEqual(cfg.lane("web").setup_action, ".night-shift/actions/web-setup")
        self.assertEqual(cfg.lane("web").setup_commands, "cd web && npm ci")
        self.assertEqual(cfg.lane("api").postgres_image, "postgres:16")
        self.assertEqual(cfg.id_prefix("web"), "W")
        self.assertIn("contracts/", cfg.protected_paths)
        self.assertEqual(cfg.secret_names(), ["AGENT_PAT", "AGENT_PAT_BOB", "CLAUDE_TOKEN_ALICE", "CLAUDE_TOKEN_ALICE_2",
                                              "CLAUDE_TOKEN_BOB", "CLAUDE_TOKEN_REVIEW"])

    def test_defaults(self):
        cfg = loads(MINIMAL)
        self.assertEqual((cfg.main_branch, cfg.merge_mode, cfg.max_attempts, cfg.infra_cap), ("main", "strict", 3, 6))
        self.assertEqual(cfg.protected_paths, BUILTIN_PROTECTED)
        self.assertEqual(cfg.effective_fix_lane, "api")

    def test_builtin_protection_cannot_be_removed(self):
        cfg = loads(MINIMAL + "protected_paths: []\n")
        for p in (".github/", ".claude/", "CLAUDE.md", "AGENTS.md", ".mcp.json"):
            self.assertIn(p, cfg.protected_paths)

    def test_errors_name_the_path(self):
        cases = {
            "version: 1\nlanes: []\n": "at least 1",
            MINIMAL + "bogus: 1\n": "unknown key 'bogus'",
            MINIMAL.replace("CLAUDE_TOKEN_A", "lowercase") : "$.lanes[0].claude_token_secret",
            MINIMAL + "merge:\n  mode: yolo\n": "$.merge.mode",
            MINIMAL + "fix_lane: web\n": "fix_lane 'web' is not a lane",
            MINIMAL + "limits:\n  max_attempts: 0\n": "$.limits.max_attempts",
            MINIMAL + "  - name: api\n    claude_token_secret: X\n": "unique",
        }
        for text, msg in cases.items():
            with self.subTest(msg=msg), self.assertRaises(ConfigError) as ctx:
                loads(text)
            self.assertIn(msg, str(ctx.exception))

    def test_validator_subset(self):
        s = {"type": "object", "properties": {"a": {"type": ["string", "array"], "items": {"type": "string"}}},
             "additionalProperties": {"type": "integer"}}
        self.assertEqual(schema.validate({"a": ["x"], "b": 1}, s), [])
        self.assertEqual(len(schema.validate({"a": [1], "b": "x"}, s)), 2)


if __name__ == "__main__":
    unittest.main()
