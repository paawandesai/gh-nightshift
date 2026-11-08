import csv
import json
import os
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace

from nightshift import audit
from nightshift.gh import GH, GHError


def pr(n, ref=None, merged=True, login="bot", title=None):
    return {"number": n, "title": title or f"PR {n}", "head": {"ref": ref or f"agent/core/issue-{n + 100}"},
            "user": {"login": login}, "merged_at": "2026-11-01T00:00:00Z" if merged else None,
            "merge_commit_sha": f"merge{n}"}


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        base, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        for key, value in self.routes.items():
            if key == base:
                data = value(params) if callable(value) else value
                if isinstance(data, list) and "page" in params:
                    return data if params["page"] == "1" else []
                return data
        raise GHError(f"no route {path}", 404)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def make_gh(routes):
    gh = GH("o/r", transport=FakeTransport(routes))
    gh.login = "bot"
    return gh


class SampleTests(unittest.TestCase):
    def setUp(self):
        prs = [pr(i) for i in range(1, 21)]
        prs += [pr(50, merged=False), pr(51, ref="feature/x"), pr(52, login="human"),
                pr(53, ref="agent/docs/issue-7")]
        self.gh = make_gh({"repos/o/r/pulls": prs})

    def test_excludes_unmerged_non_agent_and_foreign(self):
        got = [p["number"] for p in audit.agent_prs(self.gh)]
        self.assertEqual(got, list(range(1, 21)) + [53])
        self.assertEqual([p["number"] for p in audit.agent_prs(self.gh, ["docs"])], [53])

    def test_sample_reproducible(self):
        a = [p["number"] for p in audit.sample(self.gh, 5, seed=7)]
        b = [p["number"] for p in audit.sample(self.gh, 5, seed=7)]
        self.assertEqual(a, b)
        self.assertEqual(len(a), 5)
        self.assertTrue(set(a) <= set(range(1, 21)) | {53})
        others = {tuple(p["number"] for p in audit.sample(self.gh, 5, seed=s)) for s in range(10)}
        self.assertGreater(len(others), 1)

    def test_sample_all_when_fewer(self):
        got = audit.sample(self.gh, 100, seed=1, lanes=["core"])
        self.assertEqual([p["number"] for p in got], list(range(1, 21)))

    def test_uses_closed_state(self):
        audit.sample(self.gh, 1, seed=1)
        self.assertIn("state=closed", self.gh.transport.calls[0][1])


class ClassifyTests(unittest.TestCase):
    def test_classify(self):
        paths = ["src/app.py", "tests/test_app.py", "pkg/test_x.py", "web/a.test.js", "go/x_test.go",
                 "rb/user_spec.rb", "spec/models/u.rb", "README.md", "docs/guide.txt",
                 "progress/core/2026-11-01-issue-1.txt", "lib/contest.py", "test/helper.js"]
        tests, impl = audit.classify(paths)
        self.assertEqual(tests, ["tests/test_app.py", "pkg/test_x.py", "web/a.test.js", "go/x_test.go",
                                 "rb/user_spec.rb", "spec/models/u.rb", "test/helper.js"])
        self.assertEqual(impl, ["src/app.py", "lib/contest.py"])


class EvidenceTests(unittest.TestCase):
    def test_writes_files_and_rows(self):
        def commits(params):
            if params["path"] == "src/app.py":
                return [{"sha": "merge1", "commit": {"message": "own merge", "author": {"date": "d0"}}},
                        {"sha": "abc123456789xyz", "commit": {"message": "fix crash\n\nlong body",
                                                              "author": {"date": "2026-11-03"}}}]
            return []

        gh = make_gh({
            "repos/o/r": {"default_branch": "trunk"},
            "repos/o/r/issues/101": {"title": "Add app", "body": "Do the thing"},
            "repos/o/r/pulls/1/files": [{"filename": "src/app.py", "patch": "@@ +1 @@\n+x = 1"},
                                        {"filename": "tests/test_app.py", "patch": "+def test(): pass"},
                                        {"filename": "README.md", "patch": "+doc"}],
            "repos/o/r/commits": commits,
        })
        with tempfile.TemporaryDirectory() as d:
            rows = audit.collect_evidence(gh, [pr(1)], d)
            folder = os.path.join(d, "pr-1")
            ticket, diff, later = (read(os.path.join(folder, f))
                                   for f in ("ticket.md", "diff.patch", "later_commits.md"))
        self.assertIn("Add app", ticket)
        self.assertIn("Do the thing", ticket)
        self.assertIn("src/app.py", diff)
        self.assertIn("+x = 1", diff)
        self.assertIn("abc123456789 2026-11-03 fix crash", later)
        self.assertNotIn("own merge", later)
        self.assertNotIn("long body", later)
        commit_calls = [c[1] for c in gh.transport.calls if c[1].startswith("repos/o/r/commits")]
        self.assertTrue(all("sha=trunk" in c and "since=" in c for c in commit_calls))
        row = rows[0]
        self.assertEqual((row["pr"], row["issue"], row["lane"], row["files"]), (1, 101, "core", 3))
        self.assertEqual(row["test_files"], ["tests/test_app.py"])
        self.assertEqual(row["impl_files"], ["src/app.py"])
        self.assertEqual(row["later_commit_count"], 1)
        self.assertEqual(row["merge_commit_sha"], "merge1")


class FakeRun:
    """Answers git and test commands; test results come from a list, one per test run."""

    def __init__(self, test_results=(0, 1), parent_has=(), worktree_rc=0, missing=()):
        self.test_results = list(test_results)
        self.parent_has = set(parent_has)
        self.worktree_rc = worktree_rc
        self.missing = set(missing)
        self.calls = []
        self.worktree = None

    def __call__(self, args, **kw):
        self.calls.append((list(args), kw))
        if args[0] in self.missing:
            raise FileNotFoundError(args[0])
        if args[0] == "git":
            sub = args[3]
            if sub == "worktree" and args[4] == "add":
                self.worktree = args[6]
                if self.worktree_rc == 0:
                    os.makedirs(self.worktree, exist_ok=True)
                    for f in ("src/new.py", "src/app.py"):
                        os.makedirs(os.path.dirname(os.path.join(self.worktree, f)), exist_ok=True)
                        open(os.path.join(self.worktree, f), "w").close()
                return SimpleNamespace(returncode=self.worktree_rc, stdout="", stderr="bad sha")
            if sub == "cat-file":
                return SimpleNamespace(returncode=0 if args[5].split(":", 1)[1] in self.parent_has else 128,
                                       stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=self.test_results.pop(0), stdout="", stderr="")

    def git_sub(self, name):
        return [a for a, _ in self.calls if a[0] == "git" and name in a]


class MechanicalTests(unittest.TestCase):
    IMPL = ["src/app.py", "src/new.py"]
    TESTS = ["tests/test_app.py"]

    def check(self, run, cmd="python -m pytest {tests}", impl=None, tests=None):
        return audit.mechanical_check("/repo", "abc", self.IMPL if impl is None else impl,
                                      self.TESTS if tests is None else tests, cmd, run=run)

    def test_meaningful(self):
        run = FakeRun(test_results=[0, 1], parent_has=["src/app.py"])
        res = self.check(run)
        self.assertEqual((res["pass_at_commit"], res["fail_on_parent"], res["tests_meaningful_mech"]),
                         ("yes", "yes", "yes"))
        checkout = run.git_sub("checkout")[0]
        self.assertEqual(checkout[-3:], ["abc^", "--", "src/app.py"])
        self.assertFalse(os.path.exists(os.path.join(run.worktree, "src/new.py")))
        tests_cmd = [a for a, _ in run.calls if a[0] == "python"][0]
        self.assertEqual(tests_cmd, ["python", "-m", "pytest", "tests/test_app.py"])
        self.assertEqual(len(run.git_sub("remove")), 1)
        self.assertFalse(os.path.exists(os.path.dirname(run.worktree)))

    def test_not_meaningful_when_still_passing(self):
        run = FakeRun(test_results=[0, 0], parent_has=["src/app.py", "src/new.py"])
        res = self.check(run)
        self.assertEqual((res["pass_at_commit"], res["fail_on_parent"], res["tests_meaningful_mech"]),
                         ("yes", "no", "no"))
        self.assertEqual(len(run.git_sub("remove")), 1)

    def test_fails_at_commit(self):
        run = FakeRun(test_results=[1])
        res = self.check(run)
        self.assertEqual((res["pass_at_commit"], res["tests_meaningful_mech"]), ("no", "no"))
        self.assertEqual(run.git_sub("checkout"), [])
        self.assertEqual(len(run.git_sub("remove")), 1)

    def test_skips(self):
        for kwargs, reason in [({"tests": []}, "no test files"), ({"impl": []}, "no implementation"),
                               ({"cmd": ""}, "no test command")]:
            run = FakeRun()
            res = self.check(run, **kwargs)
            self.assertEqual(res["tests_meaningful_mech"], "n/a")
            self.assertIn(reason, res["reason"])
            self.assertEqual(run.calls, [])

    def test_git_missing(self):
        run = FakeRun(missing=["git"])
        res = self.check(run)
        self.assertEqual(res["pass_at_commit"], "n/a")
        self.assertIn("git not found", res["reason"])

    def test_toolchain_missing_still_removes_worktree(self):
        run = FakeRun(missing=["python"])
        res = self.check(run)
        self.assertEqual(res["tests_meaningful_mech"], "n/a")
        self.assertIn("toolchain", res["reason"])
        self.assertEqual(len(run.git_sub("remove")), 1)
        self.assertFalse(os.path.exists(os.path.dirname(run.worktree)))

    def test_worktree_add_fails(self):
        run = FakeRun(worktree_rc=128)
        res = self.check(run)
        self.assertEqual(res["tests_meaningful_mech"], "n/a")
        self.assertIn("worktree add failed", res["reason"])
        self.assertEqual(run.git_sub("remove"), [])

    def test_exception_still_cleans_up(self):
        run = FakeRun(test_results=[])  # pop from empty list -> IndexError during the test run
        with self.assertRaises(IndexError):
            self.check(run)
        self.assertEqual(len(run.git_sub("remove")), 1)


def row(n, llm="", human="", mech="", llm_tm="", human_tm=""):
    return {"pr": n, "issue": n + 100, "lane": "core", "title": f"T{n}", "merged_at": "2026-11-01",
            "files": 2, "later_commits": 0, "pass_at_commit": "", "fail_on_parent": "",
            "tests_meaningful_mech": mech, "llm_label": llm, "llm_tests_meaningful": llm_tm,
            "human_label": human, "human_tests_meaningful": human_tm, "notes": ""}


class CsvAndReportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "audit.csv")

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip_and_columns(self):
        rows = [row(1, llm="ok", mech="yes"), {**row(2), "title": 'has, comma "quote"', "extra": "x"}]
        audit.write_csv(rows, self.path)
        with open(self.path) as fh:
            header = next(csv.reader(fh))
        self.assertEqual(header, ["pr", "issue", "lane", "title", "merged_at", "files", "later_commits",
                                  "pass_at_commit", "fail_on_parent", "tests_meaningful_mech", "llm_label",
                                  "llm_tests_meaningful", "human_label", "human_tests_meaningful", "notes"])
        back = audit.read_csv(self.path)
        self.assertEqual(back[0]["llm_label"], "ok")
        self.assertEqual(back[1]["title"], 'has, comma "quote"')
        self.assertNotIn("extra", back[1])

    def test_invalid_label_rejected(self):
        audit.write_csv([row(1, human="great")], self.path)
        with self.assertRaises(ValueError):
            audit.read_csv(self.path)
        audit.write_csv([row(1, llm_tm="maybe")], self.path)
        with self.assertRaises(ValueError):
            audit.read_csv(self.path)

    def test_report_llm_only(self):
        rows = [row(i, llm="ok", mech="yes") for i in range(1, 4)] + [row(4, llm="wrong", llm_tm="no")]
        audit.write_csv(rows, self.path)
        text = audit.report(self.path)
        self.assertIn("LLM labels — not ground truth", text)
        self.assertIn("ok rate: 75% (95% CI", text)
        self.assertIn("human labels not filled yet", text)
        self.assertIn("Tests meaningful: 75%", text)
        self.assertIn("3 mechanical, 1 from labels", text)
        self.assertIn("n=4: about", text)
        self.assertIn(" points", text)

    def test_report_detection_floor_points(self):
        audit.write_csv([row(i, llm="ok") for i in range(25)], self.path)
        self.assertIn("about 36 points", audit.report(self.path))

    def test_report_with_human_kappa(self):
        rows = [row(1, llm="ok", human="ok"), row(2, llm="ok", human="ok"),
                row(3, llm="wrong", human="wrong"), row(4, llm="ok", human="defect_fixed_later")]
        audit.write_csv(rows, self.path)
        text = audit.report(self.path)
        self.assertIn("Labels: human (4 of 4", text)
        self.assertNotIn("not ground truth", text)
        # observed 3/4, expected (3*2 + 1*1)/16 = 7/16 -> kappa = (12/16-7/16)/(9/16) = 0.56
        self.assertIn("kappa 0.56, raw 3/4", text)
        self.assertIn("ok rate: 50%", text)

    def test_report_no_labels(self):
        audit.write_csv([row(1)], self.path)
        text = audit.report(self.path)
        self.assertIn("none filled yet", text)
        self.assertIn("Tests meaningful: n/a (n=0)", text)


class CatchRateTests(unittest.TestCase):
    def test_math(self):
        # 40 merged, 2 bad of 10 audited -> 8 estimated merged bad; 12 rejected bad -> 12/20 = 60%
        text = audit.catch_rate(12, 40, (2, 10))
        self.assertIn("Catch rate: 60%", text)
        self.assertIn("n=20", text)
        self.assertIn("8.0 estimated merged bad", text)
        self.assertIn("Assumption", text)
        self.assertIn("representative", text)

    def test_rounding_and_edges(self):
        self.assertIn("n=13", audit.catch_rate(10, 10, (1, 3)))  # 3.33 estimated -> n = 10 + 3
        self.assertIn("n/a", audit.catch_rate(5, 10, (0, 0)))
        self.assertIn("n/a", audit.catch_rate(0, 10, (0, 5)))
        self.assertIn("Catch rate: 100%", audit.catch_rate(4, 10, (0, 5)))


class LlmLabelTests(unittest.TestCase):
    def test_fills_missing_rows_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.csv")
            audit.write_csv([row(1, llm="trivial"), row(2), row(3)], path)
            calls = []

            def run(args, **kw):
                calls.append(args)
                if args[args.index("--add-dir") + 1].endswith("pr-3"):
                    return SimpleNamespace(returncode=1, stdout="", stderr="boom")
                answer = {"label": "extended_later", "tests_meaningful": "yes", "notes": "built on"}
                return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": answer}), stderr="")

            out = audit.label_with_llm(path, d, run=run, model="sonnet")
            rows = audit.read_csv(path)
        self.assertTrue(out.startswith("LLM labels are not ground truth; fill human_label"))
        self.assertEqual(len(calls), 2)
        args = calls[0]
        self.assertEqual(args[:2], ["claude", "-p"])
        for flag, value in [("--output-format", "json"), ("--allowedTools", "Read,Grep,Glob"),
                            ("--model", "sonnet"), ("--add-dir", os.path.join(d, "pr-2"))]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1])["required"],
                         ["label", "tests_meaningful"])
        self.assertIn("ticket.md", args[2])
        self.assertEqual(rows[0]["llm_label"], "trivial")
        self.assertEqual((rows[1]["llm_label"], rows[1]["llm_tests_meaningful"]), ("extended_later", "yes"))
        self.assertIn("built on", rows[1]["notes"])
        self.assertEqual(rows[2]["llm_label"], "")
        self.assertIn("PR #3: labelling failed", out)

    def test_result_string_and_bad_answer(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.csv")
            audit.write_csv([row(1), row(2)], path)
            answers = iter([json.dumps({"result": json.dumps({"label": "ok", "tests_meaningful": "n/a"})}),
                            json.dumps({"structured_output": {"label": "great", "tests_meaningful": "yes"}})])
            out = audit.label_with_llm(path, d, run=lambda a, **k: SimpleNamespace(
                returncode=0, stdout=next(answers), stderr=""))
            rows = audit.read_csv(path)
        self.assertEqual(rows[0]["llm_label"], "ok")
        self.assertEqual(rows[1]["llm_label"], "")
        self.assertIn("PR #2: labelling failed", out)


if __name__ == "__main__":
    unittest.main()
