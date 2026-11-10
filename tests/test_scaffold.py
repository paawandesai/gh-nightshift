"""Generated files: caller workflows (one job per lane, explicit secrets), review and planner workflows,
stack detection, and (when the binary is available) actionlint over everything init writes."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nightshift import scaffold
from nightshift.config import loads


def make_config(lanes=("api", "web"), mode="strict", refill_below=0, pat=None, **extra):
    data = {
        "version": 1,
        "main_branch": "main",
        "ci_workflow": "ci.yml",
        "merge": {"mode": mode},
        "lanes": [],
        "planner": {"docs": ["docs/PRD.md"], "refill_below": refill_below},
    }
    for name in lanes:
        lane = {"name": name, "paths": [f"{name}/"], "claude_token_secret": f"CLAUDE_TOKEN_{name.upper()}",
                "test_command": "pytest"}
        if pat and name in pat:
            lane["agent_pat_secret"] = pat[name]
        data["lanes"].append(lane)
    data.update(extra)
    text = scaffold.config_yaml(data)
    return loads(text), text


def lane_jobs(text):
    """{lane name: job text} for every `  lane-<name>:` job in a caller workflow."""
    starts = [(m.group(1), m.start()) for m in re.finditer(r"^  lane-([a-z0-9_-]+):\s*$", text, re.M)]
    jobs = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        jobs.setdefault(name, []).append(text[start:end])
    return jobs


class LaneWorkflowTests(unittest.TestCase):
    def test_exactly_one_job_per_lane_with_its_own_secrets(self):
        cfg, _ = make_config(lanes=("api", "web", "infra"), pat={"web": "WEB_PAT"})
        text = scaffold.lane_workflow(cfg)
        jobs = lane_jobs(text)
        self.assertEqual(sorted(jobs), ["api", "infra", "web"])
        for lane in cfg.lanes:
            self.assertEqual(len(jobs[lane.name]), 1, f"lane {lane.name} must have exactly one job")
            job = jobs[lane.name][0]
            self.assertIn(f"claude_token: ${{{{ secrets.{lane.token_secret} }}}}", job)
            self.assertRegex(job, rf"agent_pat: \$\{{\{{ [^}}\n]*secrets\.{lane.agent_pat_secret}\b")
            self.assertIn(f"lane: {lane.name}", job)
            # No other lane's token reaches this job.
            for other in cfg.lanes:
                if other.token_secret != lane.token_secret:
                    self.assertNotIn(other.token_secret, job)
        self.assertIn("secrets.WEB_PAT", jobs["web"][0])
        self.assertNotIn("WEB_PAT", jobs["api"][0] + jobs["infra"][0])
        self.assertIn("secrets.AGENT_PAT", jobs["api"][0])
        self.assertEqual(text.count("uses: "), 3)

    def test_no_dynamic_secrets(self):
        cfg, _ = make_config(lanes=("api", "web"))
        for text in (scaffold.lane_workflow(cfg), scaffold.review_workflow(cfg), scaffold.planner_workflow(cfg)):
            body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            self.assertNotIn("secrets[", body)
            self.assertNotIn("secrets: inherit", body)
            self.assertNotIn("matrix", body)

    def test_pinned_to_tool_ref(self):
        cfg, _ = make_config(tool_ref="v1.2.3")
        text = scaffold.lane_workflow(cfg)
        self.assertIn(f"uses: {scaffold.TOOL_REPO}/.github/workflows/lane.yml@v1.2.3", text)
        self.assertNotIn("@main", text)

    def test_ci_name_quoted_in_workflow_run(self):
        cfg, _ = make_config()
        text = scaffold.lane_workflow(cfg, ci_name="Bob's CI")
        self.assertIn("workflows: ['Bob''s CI', night-shift-review]", text)


class ReviewWorkflowTests(unittest.TestCase):
    def test_merge_group_only_in_queue_mode(self):
        strict, _ = make_config(mode="strict")
        queue, _ = make_config(mode="queue")
        self.assertNotIn("merge_group", scaffold.review_workflow(strict))
        self.assertIn("merge_group:", scaffold.review_workflow(queue))

    def test_review_token_secret(self):
        cfg, _ = make_config(review_token_secret="REVIEWER_TOKEN")
        self.assertIn("claude_token: ${{ secrets.REVIEWER_TOKEN }}", scaffold.review_workflow(cfg))


class PlannerWorkflowTests(unittest.TestCase):
    def test_schedule_only_when_refill_below_positive(self):
        off, _ = make_config(refill_below=0)
        on, _ = make_config(refill_below=3)
        self.assertNotIn("schedule:", scaffold.planner_workflow(off))
        self.assertNotIn("cron:", scaffold.planner_workflow(off))
        text = scaffold.planner_workflow(on)
        self.assertIn("schedule:", text)
        self.assertIn(f'- cron: "{on.planner_schedule}"', text)

    def test_planner_uses_first_lane_token_by_default(self):
        cfg, _ = make_config(lanes=("api", "web"))
        self.assertIn("claude_token: ${{ secrets.CLAUDE_TOKEN_API }}", scaffold.planner_workflow(cfg))


class FilesTests(unittest.TestCase):
    def test_files_lists_everything_and_planner_toggle(self):
        cfg, text = make_config()
        with tempfile.TemporaryDirectory() as d:
            files = scaffold.files(cfg, text, Path(d))
            self.assertEqual(files[".night-shift/config.yml"], text)
            for path in (".github/workflows/night-shift.yml", ".github/workflows/night-shift-review.yml",
                         ".github/workflows/night-shift-planner.yml", "AGENT_RULES.md",
                         "docs/NIGHT_SHIFT_ISSUES.md", ".github/ISSUE_TEMPLATE/night-shift-ticket.md"):
                self.assertIn(path, files)
            cfg_off, text_off = make_config(planner={"enabled": False})
            self.assertNotIn(".github/workflows/night-shift-planner.yml", scaffold.files(cfg_off, text_off, Path(d)))

    def test_ci_name_read_from_ci_file(self):
        cfg, text = make_config()
        with tempfile.TemporaryDirectory() as d:
            wf = Path(d, ".github", "workflows")
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text('name: "Build and test"\non: [push]\n', encoding="utf-8")
            files = scaffold.files(cfg, text, Path(d))
            self.assertIn("workflows: ['Build and test', night-shift-review]", files[".github/workflows/night-shift.yml"])

    def test_config_yaml_round_trips(self):
        cfg, text = make_config(lanes=("api",), refill_below=2)
        again = loads(text)
        self.assertEqual(again.lane_names, cfg.lane_names)
        self.assertEqual(again.planner_refill_below, 2)


class DetectStackTests(unittest.TestCase):
    def test_detects_root_and_top_level_folders(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pyproject.toml").write_text("", encoding="utf-8")
            for folder, marker in (("api", "requirements.txt"), ("web", "package.json"), ("svc", "go.mod"),
                                   (".hidden", "Cargo.toml"), ("docs", "README.md")):
                (root / folder).mkdir()
                (root / folder / marker).write_text("", encoding="utf-8")
            found = scaffold.detect_stack(root)
            by_path = {f["path"]: f for f in found}
            self.assertEqual([f["path"] for f in found], ["./", "api/", "svc/", "web/"])
            self.assertEqual(by_path["./"]["stack"], "python")
            self.assertEqual(by_path["./"]["test"], "pytest")
            self.assertEqual(by_path["api/"], {"path": "api/", "stack": "python",
                                               "setup": "cd api && pip install -r requirements.txt",
                                               "test": "cd api && pytest"})
            self.assertEqual(by_path["web/"]["setup"], "cd web && npm ci")
            self.assertEqual(by_path["svc/"]["test"], "cd svc && go test ./...")

    def test_first_marker_wins_in_a_folder(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "app").mkdir()
            (root / "app" / "pyproject.toml").write_text("", encoding="utf-8")
            (root / "app" / "package.json").write_text("", encoding="utf-8")
            found = scaffold.detect_stack(root)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["stack"], "python")

    def test_empty_tree(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(scaffold.detect_stack(Path(d)), [])


def actionlint_path():
    return os.environ.get("ACTIONLINT") or shutil.which("actionlint")


# actionlint cannot fetch remote reusable workflows offline; that one message is acceptable.
REMOTE_REUSABLE = re.compile(r"reusable workflow|could not (read|fetch|parse) .*workflow", re.I)


@unittest.skipUnless(actionlint_path(), "actionlint not on PATH (set ACTIONLINT to its path)")
class ActionlintTests(unittest.TestCase):
    def lint(self, cfg, text):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            for rel, content in scaffold.files(cfg, text, root).items():
                if rel.startswith(".github/workflows/"):
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
            r = subprocess.run([actionlint_path(), "-no-color"], cwd=root, capture_output=True, text=True)
            output = (r.stdout + r.stderr).strip()
            if r.returncode != 0:
                problems = [ln for ln in output.splitlines()
                            if re.match(r"^\.github/workflows/\S+:\d+:\d+:", ln) and not REMOTE_REUSABLE.search(ln)]
                self.assertEqual(problems, [], output)
            return r

    def test_strict_mode_workflows_pass_actionlint(self):
        cfg, text = make_config(lanes=("api", "web"), mode="strict", refill_below=0)
        self.lint(cfg, text)

    def test_queue_mode_with_refill_and_pat_passes_actionlint(self):
        cfg, text = make_config(lanes=("api", "web", "db"), mode="queue", refill_below=3, pat={"db": "DB_PAT"},
                                tool_ref="v1.0.0")
        self.lint(cfg, text)



class RunnerTests(unittest.TestCase):
    def test_default_callers_name_no_runner(self):
        cfg, _ = make_config()
        for text in (scaffold.lane_workflow(cfg), scaffold.planner_workflow(cfg), scaffold.review_workflow(cfg),
                     scaffold.commands_workflow(cfg), scaffold.digest_workflow(cfg)):
            self.assertNotIn("runs_on", text)

    def test_runners_reach_every_caller_by_job_class(self):
        cfg, _ = make_config(runners={"github": ["self-hosted", "ns-github"], "agent": "ns-agent"})
        github = """runs_on: '["self-hosted", "ns-github"]'"""
        agent = """agent_runs_on: '["ns-agent"]'"""
        for job in sum(lane_jobs(scaffold.lane_workflow(cfg)).values(), []):
            self.assertIn(github, job)
            self.assertIn(agent, job)
        self.assertIn(agent, scaffold.planner_workflow(cfg))
        self.assertIn(github, scaffold.planner_workflow(cfg))
        review = scaffold.review_workflow(cfg)  # the reviewer runs Claude and holds no PAT or App key
        self.assertIn(agent, review)
        self.assertNotIn(github, review)
        for text in (scaffold.commands_workflow(cfg), scaffold.digest_workflow(cfg)):
            self.assertIn(github, text)
            self.assertNotIn("agent_runs_on", text)  # these jobs hold the PAT or App key


class ReusableWorkflowRunnerTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    AGENT_JOBS = {"lane.yml": {"agent"}, "planner.yml": {"draft"}, "review.yml": {"verdict"}}

    def test_every_job_takes_its_runner_from_an_input(self):
        for name in ("lane.yml", "planner.yml", "review.yml", "commands.yml", "digest.yml"):
            text = (self.ROOT / name).read_text()
            self.assertNotIn("runs-on: ubuntu-latest", text, name)
            jobs = re.split(r"^  ([a-z_-]+):\n", text.split("\njobs:\n", 1)[1], flags=re.M)[1:]
            for job, body in zip(jobs[::2], jobs[1::2]):
                want = "agent_runs_on" if job in self.AGENT_JOBS.get(name, set()) else "runs_on"
                with self.subTest(workflow=name, job=job):
                    self.assertIn(f"runs-on: ${{{{ fromJSON(inputs.{want}) }}}}", body)

    def test_token_and_claude_never_meet_in_one_job_class(self):
        token = re.compile(r"secrets\.(agent_pat|app_private_key)|create-github-app-token")
        for name in ("lane.yml", "planner.yml", "review.yml", "commands.yml", "digest.yml"):
            text = (self.ROOT / name).read_text()
            jobs = re.split(r"^  ([a-z_-]+):\n", text.split("\njobs:\n", 1)[1], flags=re.M)[1:]
            for job, body in zip(jobs[::2], jobs[1::2]):
                with self.subTest(workflow=name, job=job):
                    if token.search(body):
                        self.assertIn("fromJSON(inputs.runs_on)", body)
                        self.assertNotIn("Install Claude Code", body)
                    if "Install Claude Code" in body:
                        self.assertIn("fromJSON(inputs.agent_runs_on)", body)

    def test_claude_code_is_installed_fresh_into_the_job_temp_dir(self):
        for name in ("lane.yml", "planner.yml", "review.yml"):
            text = (self.ROOT / name).read_text()
            with self.subTest(workflow=name):
                self.assertIn('npm install -g --prefix "$RUNNER_TEMP/claude-code"', text)
                self.assertIn('echo "$RUNNER_TEMP/claude-code/bin" >> "$GITHUB_PATH"', text)
                self.assertNotRegex(text, r'npm install -g "@anthropic-ai')


if __name__ == "__main__":
    unittest.main()
