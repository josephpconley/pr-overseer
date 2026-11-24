"""Review one specific PR by URL: parsing, bypasses, unconfigured-repo fallback, closed-PR refusal."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rv", HERE / "review.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)
CP = subprocess.CompletedProcess


class Parse(unittest.TestCase):
    def test_variants(self):
        want = ("acme/widgets", 146)
        for t in ("https://github.com/acme/widgets/pull/146",
                  "https://github.com/acme/widgets/pull/146/",
                  "https://github.com/acme/widgets/pull/146/files",
                  "https://github.com/acme/widgets/pull/146/commits/abc123",
                  "https://github.com/acme/widgets/pull/146#issuecomment-99",
                  "https://github.com/acme/widgets/pull/146?diff=split#diff-1",
                  "  http://www.github.com/acme/widgets/pull/146  ",
                  "github.com/acme/widgets/pull/146",
                  "acme/widgets#146"):
            self.assertEqual(rv.parse_pr_ref(t), want, t)
        self.assertEqual(rv.parse_pr_ref("a.b/c_d-e.f#7"), ("a.b/c_d-e.f", 7))

    def test_rejects(self):
        for t in ("", "123", "#12", "owner/repo", "owner/repo#0", "owner/repo#x",
                  "https://github.com/o/r/issues/5", "https://github.com/o/r", "https://github.com/o/r/pull/",
                  "https://github.com/o/r/pull/abc", "https://evilgithub.com/o/r/pull/5",
                  "https://gitlab.com/o/r/pull/5", "https://github.example.com/o/r/pull/5"):
            with self.assertRaises(ValueError, msg=t):
                rv.parse_pr_ref(t)
        with self.assertRaisesRegex(ValueError, "not a github.com PR URL"):
            rv.parse_pr_ref("https://gitlab.com/o/r/pull/5")

    def test_adhoc_entry(self):
        e = rv.adhoc_entry("o/r")
        self.assertEqual(e["review_mode"], "review")
        self.assertTrue(e["path"].endswith("o__r"))
        self.assertNotIn("merge_method", e)


class MainUrl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "clone").mkdir()
        self.repos = t / "repos.json"
        self.repos.write_text(json.dumps({"github_login": "example-user", "repos": [
            {"repo": "Acme/Own", "path": str(t / "clone"), "review_mode": "self_review"}]}))
        self.harn = t / "h.json"
        self.harn.write_text(json.dumps({"harnesses": [{"id": "cursor", "bin": "agent", "cooldown_until": None}]}))
        self.pr = {"number": 7, "title": "t", "url": "u", "headRefName": "b", "isDraft": False,
                   "author": {"login": "example-user"}, "reviewRequests": [], "state": "OPEN"}
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, rv, "URL_RUN", False)
        self.review_one = mock.Mock(return_value=Path("/x/review.md"))
        self.ps = [
            mock.patch.object(rv, "REPOS_PATH", self.repos), mock.patch.object(rv, "HARNESSES_PATH", self.harn),
            mock.patch.object(rv, "CLONE_CACHE", t / "cache"),
            mock.patch("fcntl.flock"), mock.patch.object(rv, "run", side_effect=self.fake_run),
            mock.patch.object(rv, "review_one", self.review_one),
            mock.patch.object(rv, "fetch_feedback_state", return_value={
                "latest": {"verdict": "APPROVE", "source": "review", "at": "2026-01-01T00:00:00Z"},
                "count": 1, "new_commits": [], "head": "abc", "headRefName": "b", "cross": False}),
            mock.patch.object(rv, "fetch_pr_authoring", return_value={}),
            mock.patch.object(rv, "detect_latest_commit_harness", return_value=({}, {})),
            mock.patch.object(rv, "eligible_harnesses", return_value=([{"id": "cursor"}], "", None)),
        ]
        for p in self.ps:
            p.start(); self.addCleanup(p.stop)
        self.calls = []

    def fake_run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "api", "user"]:
            return CP(cmd, 0, "example-user\n", "")
        if cmd[:3] == ["gh", "pr", "view"]:
            return CP(cmd, 0, json.dumps(self.pr), "")
        if cmd[:3] == ["gh", "repo", "clone"]:
            Path(cmd[4]).mkdir(parents=True, exist_ok=True)
            return CP(cmd, 0, "", "")
        return CP(cmd, 1, "", "unexpected")

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["review.py", *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = rv.main()
        return rc, out.getvalue(), err.getvalue()

    def test_bypasses_already_reviewed_state(self):
        rc, out, _ = self.main("https://github.com/acme/own/pull/7/files")   # case-insensitive repo match
        self.assertEqual(rc, 0)
        self.review_one.assert_called_once()
        self.assertTrue(rv.URL_RUN)
        self.assertEqual(self.review_one.call_args[0][0], "Acme/Own")
        self.assertEqual(self.review_one.call_args[1]["mode"], "self_review")
        self.assertNotIn("skip PR", out)

    def test_draft_allowed(self):
        self.pr["isDraft"] = True
        rc, _, _ = self.main("--url", "Acme/Own#7")
        self.assertEqual(rc, 0); self.review_one.assert_called_once()

    def test_dependabot_reviewed_but_never_merged(self):
        self.pr["author"] = {"login": "dependabot[bot]", "is_bot": True}
        rc, out, _ = self.main("Acme/Own#7")
        self.assertEqual(rc, 0)
        self.assertIs(self.review_one.call_args[1]["merge_on_approve"], False)
        self.assertIn("Dependabot", out)

    def test_closed_pr_refused(self):
        for state in ("MERGED", "CLOSED"):
            self.pr["state"] = state; self.review_one.reset_mock()
            rc, _, err = self.main("Acme/Own#7")
            self.assertEqual(rc, 1)
            self.review_one.assert_not_called()
            self.assertIn("closed/merged", err)

    def test_unconfigured_repo_dry_run_clones_nothing(self):
        rc, out, _ = self.main("--dry-run", "https://github.com/Other/Thing/pull/3")
        self.assertEqual(rc, 0)
        self.assertIn("would gh-repo-clone", out)
        self.assertFalse(any(c[:3] == ["gh", "repo", "clone"] for c in self.calls))

    def test_unconfigured_repo_live_clones_and_reviews_in_review_mode(self):
        self.pr["author"] = {"login": "someone"}
        rc, out, _ = self.main("https://github.com/Other/Thing/pull/7")
        self.assertEqual(rc, 0)
        self.assertTrue(any(c[:3] == ["gh", "repo", "clone"] and c[3] == "Other/Thing" for c in self.calls))
        kw = self.review_one.call_args[1]
        self.assertEqual(kw["mode"], "review")
        self.assertIn("never merges", out)

    def test_no_review_produced_is_nonzero(self):
        self.review_one.return_value = None
        rc, _, err = self.main("Acme/Own#7")
        self.assertEqual(rc, 1)

    def test_bad_args(self):
        for argv in (["https://gitlab.com/o/r/pull/1"], ["not a pr"], ["Acme/Own#7", "--repo", "Acme/Other"],
                     ["Acme/Own#7", "--pr", "8"], ["Acme/Own#7", "--dependabot"], ["Acme/Own#7", "--url", "Acme/Own#8"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm:
                self.main(*argv)
            self.assertEqual(cm.exception.code, 2)

    def test_lock_wait_default_and_no_wait(self):
        waits = []

        def flock(fh, op):
            waits.append(op)
            raise BlockingIOError
        with mock.patch("fcntl.flock", side_effect=flock), mock.patch("time.sleep") as sl, \
                mock.patch("time.monotonic", side_effect=[0, 0, 1300]):
            rc, out, _ = self.main("Acme/Own#7")
        self.assertEqual(rc, 1); self.assertIn("waiting up to 20 min", out); sl.assert_called()
        with mock.patch("fcntl.flock", side_effect=BlockingIOError), mock.patch("time.sleep") as sl:
            rc, out, _ = self.main("--no-wait", "Acme/Own#7")
        self.assertEqual(rc, 1); sl.assert_not_called(); self.assertNotIn("waiting", out)

    def test_normal_run_unaffected(self):
        self.assertFalse(rv.URL_RUN)


class TierCache(unittest.TestCase):
    def test_url_run_skips_tier_cache(self):
        for url_run, want in ((False, True), (True, False)):
            seen = {}
            with mock.patch.object(rv, "load_tiers", return_value={"enabled": True}), \
                    mock.patch.object(rv, "fetch_classify_meta", return_value={}), \
                    mock.patch.object(rv, "classify_pr", side_effect=lambda *a, **k: (
                        seen.update(k) or ("everyday", "r", "rules"))), \
                    mock.patch.object(rv, "set_active_tier"):
                rv.apply_tier_for_pr("o/r", 1, Path("h.json"), diff="d", override=None, use_cache=not url_run)
            self.assertEqual(seen["use_cache"], want)


if __name__ == "__main__":
    unittest.main()
