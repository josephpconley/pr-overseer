"""Regression test (2026-11-05): review_one(pre=...) + self_review CI pre-check.

Former crash: review_one() reused the name `pre` for fetch_ci_status() output, so a green-CI
self_review merge candidate crashed at `authoring, exclude, last_info = pre` (ValueError) —
every cron tick died on a self_review PR.  Everything external is mocked: no gh,
no git, no harness, no GitHub writes.  The overseer lock is bypassed (fcntl.flock no-op) so
this never contends with cron.   Run:  python3 test_review_one_pre.py
"""
import importlib.util, io, json, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rv", HERE / "review.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)

LOGIN = "example-user"
SHA = "a" * 40
PR = {"number": 121, "title": "ci: gate", "url": "https://example.invalid/pr/121", "headRefName": "feat/x",
      "isDraft": False, "author": {"login": LOGIN}}
GREEN = [{"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}]
PENDING = [{"name": "build", "status": "IN_PROGRESS", "conclusion": ""}]
RED = [{"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"}]
# Latest commit made by Claude (trailer) → claude must be excluded.
CLAUDE_META = {"title": "t", "body": "", "commits": [{"oid": SHA, "messageHeadline": "fix",
               "messageBody": "x\n\nCo-authored-by: Claude Opus 5.5 <noreply@anthropic.com>",
               "authors": [{"login": LOGIN, "name": "Example User", "email": "j@x"}]}]}


def ci_status(rollup):
    return {"number": 121, "state": "OPEN", "isDraft": False, "headRefOid": SHA,
            "statusCheckRollup": rollup, "mergeable": "MERGEABLE"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ovs-test-"))
        self.wt = self.tmp / "wt"; self.wt.mkdir()
        self.hpath = self.tmp / "harnesses.json"
        self.set_cooldowns(set())
        self.cmds, self.harness_calls, self.posted = [], [], []

        def fake_run(cmd, **kw):
            self.cmds.append(list(cmd))
            if cmd[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(cmd, 0, SHA + "\n", "")
            if cmd[:3] == ["gh", "pr", "merge"]:   # recorded only; never executed
                return subprocess.CompletedProcess(cmd, 0, "merged", "")
            if cmd[:3] == ["gh", "pr", "view"] and cmd[-1] == "labels":   # needs-human-review hold check
                return subprocess.CompletedProcess(cmd, 0, '{"labels": []}', "")
            return subprocess.CompletedProcess(cmd, 1, "", "blocked in test")

        def runner(hid, out):
            def _r(wt, prompt):
                self.harness_calls.append(hid)
                return subprocess.CompletedProcess([hid], 0, out, "")
            return _r
        self.verdict_out = "Looks fine.\n\nVERDICT: APPROVE\n"
        runners = {h: runner(h, None) for h in ("muse", "cursor", "claude")}
        def mk(h):
            def _r(wt, prompt):
                self.harness_calls.append(h)
                return subprocess.CompletedProcess([h], 0, self.verdict_out, "")
            return _r
        runners = {h: mk(h) for h in ("muse", "cursor", "claude")}

        def post_comment(repo, n, verdict, body, reviewed_sha=None, soft_why=None):
            self.posted.append(("comment", verdict, reviewed_sha)); return True, f"COMMENT ({verdict})"

        def submit(repo, n, verdict, body, soft_why=None):
            self.posted.append(("review", verdict)); return True, verdict

        self.patches = [
            mock.patch.object(rv, "run", side_effect=fake_run),
            mock.patch.object(rv, "RUNNERS", runners),
            mock.patch.object(rv.shutil, "which", return_value="/usr/bin/fake"),
            mock.patch.object(rv, "ensure_stubs", return_value=[]),
            mock.patch.object(rv, "create_pr_worktree", return_value=self.wt),
            mock.patch.object(rv, "remove_worktree"),
            mock.patch.object(rv, "read_context", return_value="ctx"),
            mock.patch.object(rv, "fetch_pr_diff", return_value="diff"),
            mock.patch.object(rv, "write_review", side_effect=lambda *a, **k: self.tmp / "PR-121.md"),
            mock.patch.object(rv, "post_review_comment", side_effect=post_comment),
            mock.patch.object(rv, "submit_github_review", side_effect=submit),
            mock.patch.object(rv, "fetch_commit_identity", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(mock.patch.stopall)

    def set_cooldowns(self, cooling):
        self.hpath.write_text(json.dumps({"default_cooldown_minutes": 15, "harnesses": [
            {"id": h, "bin": h, "cooldown_until": "2099-01-01T00:00:00Z" if h in cooling else None}
            for h in ("muse", "cursor", "claude")]}))

    def call_review_one(self, rollup, pre, exclude_note="", merge_on_approve=True):
        with mock.patch.object(rv, "fetch_ci_status", return_value=ci_status(rollup) if rollup is not None else None), \
             mock.patch.object(rv, "fetch_pr_authoring", side_effect=AssertionError("refetch despite pre")), \
             mock.patch.object(rv, "detect_latest_commit_harness", side_effect=AssertionError("refetch despite pre")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                res = rv.review_one("example-org/example-repo", self.tmp, PR, rv.load_json(self.hpath), self.hpath,
                                    mode="self_review", merge_method="squash", login=LOGIN, pre=pre,
                                    merge_on_approve=merge_on_approve)
            return res, out.getvalue() + err.getvalue()

    def merges(self):
        return [c for c in self.cmds if c[:3] == ["gh", "pr", "merge"]]


class ReviewOneWithPre(Base):
    def test_stubs_only_in_worktree_never_main_clone(self):
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        res, log = self.call_review_one(GREEN, (CLAUDE_META, exclude, info))
        calls = [c.args[0] for c in rv.ensure_stubs.call_args_list]
        self.assertEqual(calls, [self.wt], log)                       # never self.tmp (the live clone)

    def test_green_ci_with_pre_tuple_no_crash_exact_former_path(self):
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        self.assertIn("claude", exclude)
        self.assertEqual(rv.plan_action("self_review", "APPROVE", PR, LOGIN), "merge")  # CI pre-check branch on
        res, log = self.call_review_one(GREEN, (CLAUDE_META, exclude, info))
        self.assertIsNotNone(res, log)
        self.assertIn("CI (pre-review): green", log)
        self.assertEqual(self.harness_calls, ["muse"], log)          # first eligible, never claude
        self.assertIn("excluded claude (latest commit)", log)
        self.assertEqual(self.posted, [("comment", "APPROVE", SHA)])
        m = self.merges()                                            # gate passed → merge cmd (recorded only)
        self.assertEqual(len(m), 1); self.assertIn("--match-head-commit", m[0]); self.assertIn(SHA, m[0])

    def test_merge_on_approve_false_comments_and_never_merges(self):
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        self.assertEqual(rv.plan_action("self_review", "APPROVE", PR, LOGIN, merge_on_approve=False), "review")
        res, log = self.call_review_one(GREEN, (CLAUDE_META, exclude, info), merge_on_approve=False)
        self.assertIsNotNone(res, log)
        self.assertEqual(self.posted, [("comment", "APPROVE", SHA)], log)
        self.assertEqual(self.merges(), [], log)
        self.assertIn("self_review: not merging (merge_on_approve=false)", log)
        # Must not defer on CI: review happens immediately even if we passed green CI.
        self.assertNotIn("defer: CI still running", log)

    def test_latest_commit_exclusion_with_failover(self):
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        self.set_cooldowns({"muse"})
        res, log = self.call_review_one(GREEN, (CLAUDE_META, exclude, info))
        self.assertIsNotNone(res, log)
        self.assertEqual(self.harness_calls, ["cursor"], log)

    def test_pending_ci_defers_before_worktree_and_harness(self):
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        with mock.patch.object(rv, "create_pr_worktree", side_effect=AssertionError("worktree on pending CI")):
            res, log = self.call_review_one(PENDING, (CLAUDE_META, exclude, info))
        self.assertIsNone(res); self.assertIn("defer: CI still running", log)
        self.assertEqual(self.harness_calls, []); self.assertEqual(self.posted, []); self.assertEqual(self.merges(), [])

    def test_red_ci_reviews_but_never_merges(self):
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        res, log = self.call_review_one(RED, (CLAUDE_META, exclude, info))
        self.assertIsNotNone(res, log)
        self.assertEqual(self.posted, [("comment", "APPROVE", SHA)])
        self.assertEqual(self.merges(), [], log); self.assertIn("not merged: CI gate blocked merge", log)

    def test_ci_fetch_failure_fails_closed(self):
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        res, log = self.call_review_one(None, (CLAUDE_META, exclude, info))
        self.assertIn("unknown (fetch failed)", log)
        self.assertEqual(self.merges(), [], log)

    def test_request_changes_never_merges(self):
        self.verdict_out = "blocker\n\nVERDICT: REQUEST_CHANGES\n"
        exclude, info = rv.detect_latest_commit_harness(CLAUDE_META, None)
        res, log = self.call_review_one(GREEN, (CLAUDE_META, exclude, info))
        self.assertEqual(self.posted, [("comment", "REQUEST_CHANGES", SHA)]); self.assertEqual(self.merges(), [])


class MainPathCounting(Base):
    """Drive main() → pre-check → review_one(pre=...) for a self_review re-review shape."""
    def run_main(self, prs, max_reviews=3):
        repos = self.tmp / "repos.json"
        repos.write_text(json.dumps({"github_login": LOGIN, "address_feedback": False, "repos": [
            {"repo": "example-org/example-repo", "path": str(self.tmp), "review_mode": "self_review",
             "merge_method": "squash"}]}))
        st = {"latest": {"verdict": "REQUEST_CHANGES", "at": "2026-11-02T21:46:58Z", "source": "self-review comment"},
              "count": 1, "new_commits": [{"oid": SHA}], "head": SHA, "headRefName": "feat/x"}
        rv.RUN_SUMMARY.clear()
        out, err = io.StringIO(), io.StringIO()
        argv = ["review.py", "--repos-config", str(repos), "--harnesses-config", str(self.hpath),
                "--max-reviews", str(max_reviews)]
        with mock.patch.object(sys, "argv", argv), mock.patch("fcntl.flock"), \
             mock.patch.object(rv, "list_self_review_prs", return_value=prs), \
             mock.patch.object(rv, "fetch_feedback_state", return_value=st), \
             mock.patch.object(rv, "load_fix_state", return_value={"attempts": []}), \
             mock.patch.object(rv, "fetch_pr_authoring", return_value=CLAUDE_META), \
             mock.patch.object(rv, "fetch_ci_status", return_value=ci_status(GREEN)), \
             redirect_stdout(out), redirect_stderr(err):
            rc = rv.main()
        return rc, out.getvalue() + err.getvalue()

    def test_main_rereview_green_ci_completes(self):
        rc, log = self.run_main([PR])
        self.assertEqual(rc, 0, log); self.assertNotIn("Traceback", log)
        self.assertIn("rereview PR #121", log); self.assertIn("Reviews written:", log)
        self.assertEqual(self.harness_calls, ["muse"], log)
        # End-of-run summary: PR link + review result.
        summary = log.split("Run summary:", 1)[1]
        self.assertIn("https://github.com/example-org/example-repo/pull/121", summary)
        self.assertIn("review: APPROVE by muse", summary)
        self.assertIn("merged", summary)

    def test_no_summary_when_nothing_acted_on(self):
        self.set_cooldowns({"muse", "cursor"})            # pre-check skip: no PR acted on
        rc, log = self.run_main([PR])
        self.assertNotIn("Run summary:", log)

    def test_no_eligible_harness_skips_quietly_uncounted(self):
        self.set_cooldowns({"muse", "cursor"})            # only claude left, and it's excluded
        prs = [dict(PR, number=n) for n in (121, 122, 123, 124)]
        rc, log = self.run_main(prs, max_reviews=1)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.harness_calls, [], log)
        self.assertEqual(log.count("no eligible review harness now"), 4, log)   # none consumed the budget
        self.assertNotIn("max-reviews 1 reached", log)
        for line in log.splitlines():
            if "no eligible" in line:
                self.assertFalse(line.lstrip().startswith("!"), line)

    def test_quota_exhaustion_not_counted(self):
        def quota(wt, prompt):
            self.harness_calls.append("q")
            return subprocess.CompletedProcess(["x"], 1, "", "rate limit exceeded; usage limit reached")
        prs = [dict(PR, number=n) for n in (121, 122)]
        with mock.patch.object(rv, "RUNNERS", {"muse": quota, "cursor": quota, "claude": quota}):
            rc, log = self.run_main(prs, max_reviews=1)
        self.assertIn("not counted toward --max-reviews", log)
        self.assertNotIn("max-reviews 1 reached", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
