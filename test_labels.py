"""PR state labels: config, state→label mapping, remove-others, disabled no-op, non-fatal failures, sync."""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rv", HERE / "review.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)

CP = subprocess.CompletedProcess


def fake_gh(labels, author="example-user", fail=(), calls=None):
    """Fake rv.run: `gh pr view` returns labels; label create / api calls recorded; `fail` = substrings that fail."""
    import json

    def _run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        joined = " ".join(cmd)
        if any(f in joined for f in fail):
            return CP(cmd, 1, "", "boom")
        if cmd[:3] == ["gh", "pr", "view"]:
            return CP(cmd, 0, json.dumps({"labels": [{"name": n} for n in labels],
                                          "author": {"login": author}}), "")
        return CP(cmd, 0, "", "")
    return _run


class Config(unittest.TestCase):
    def test_default_and_overrides(self):
        self.assertFalse(rv.labels_enabled({"repo": "a/b"}, {}))
        self.assertTrue(rv.labels_enabled({"repo": "a/b"}, {"labels": True}))
        self.assertFalse(rv.labels_enabled({"repo": "a/b", "labels": False}, {"labels": True}))
        self.assertTrue(rv.labels_enabled({"repo": "a/b", "labels": True}, {"labels": False}))

    def test_configure(self):
        rv.labels_configure([{"repo": "A/b"}, {"repo": "c/d", "labels": False}], {"labels": True})
        self.assertTrue(rv.labels_active("a/B"))
        self.assertFalse(rv.labels_active("c/d"))
        rv.labels_configure([], {})


class Mapping(unittest.TestCase):
    def test_names_are_neutral_and_prefixed(self):
        self.assertEqual(len(rv.STATE_LABELS), 6)
        for name, color, desc in rv.STATE_LABELS.values():
            self.assertTrue(name.startswith("review: "))
            self.assertNotIn("overseer", (name + desc).lower())
            self.assertRegex(color, r"^[0-9a-f]{6}$")

    def test_plan_removes_other_state_labels_only(self):
        add, rem = rv.plan_state_change(["bug", "review: in progress", "review: approved"], "changes_requested")
        self.assertEqual(add, ["review: changes requested"])
        self.assertEqual(sorted(rem), ["review: approved", "review: in progress"])
        add, rem = rv.plan_state_change(["bug", "Review: Approved"], "approved")   # case-insensitive, no-op
        self.assertEqual((add, rem), ([], []))
        self.assertEqual(rv.plan_state_change([], "fixing"), (["review: fixing"], []))


class SetState(unittest.TestCase):
    def setUp(self):
        rv.labels_configure([{"repo": "o/r"}], {"labels": True})
        self.addCleanup(rv.labels_configure, [], {})

    def test_adds_new_then_removes_old(self):
        calls = []
        with mock.patch.object(rv, "run", side_effect=fake_gh(["x", "review: in progress"], calls=calls)):
            prev = rv.set_pr_state("o/r", 5, "approved")
        self.assertEqual(prev, ["review: in progress"])
        kinds = [(c[3] if c[1] == "api" else c[1]) for c in calls if c[1] in ("api", "label")]
        self.assertEqual(kinds, ["label", "POST", "DELETE"])        # create, add, remove (add before remove)
        delete = [c for c in calls if "DELETE" in c][0]
        self.assertTrue(delete[-1].endswith("/review%3A%20in%20progress"))
        self.assertIn("--force", [c for c in calls if c[1] == "label"][0])

    def test_noop_when_already_set(self):
        calls = []
        with mock.patch.object(rv, "run", side_effect=fake_gh(["review: approved"], calls=calls)):
            self.assertEqual(rv.set_pr_state("o/r", 5, "approved"), ["review: approved"])
        self.assertEqual([c for c in calls if c[1] in ("api", "label")], [])

    def test_label_created_once_per_run(self):
        calls = []
        with mock.patch.object(rv, "run", side_effect=fake_gh([], calls=calls)):
            rv.set_pr_state("o/r", 1, "approved"); rv.set_pr_state("o/r", 2, "approved")
        self.assertEqual(len([c for c in calls if c[1] == "label"]), 1)

    def test_disabled_repo_is_noop(self):
        with mock.patch.object(rv, "run") as r:
            self.assertIsNone(rv.set_pr_state("other/repo", 5, "approved"))
            rv.restore_pr_state("other/repo", 5, ["review: approved"])
        r.assert_not_called()

    def test_dependabot_skipped(self):
        calls = []
        with mock.patch.object(rv, "run", side_effect=fake_gh([], author="dependabot[bot]", calls=calls)):
            self.assertIsNone(rv.set_pr_state("o/r", 5, "approved"))
        self.assertEqual([c for c in calls if c[1] in ("api", "label")], [])

    def test_failures_are_non_fatal(self):
        for fail in ("gh label create", "-X POST", "-X DELETE", "gh pr view"):
            with self.subTest(fail=fail), mock.patch.object(rv, "run", side_effect=fake_gh(
                    ["review: in progress"], fail=(fail,))):
                rv.labels_configure([{"repo": "o/r"}], {"labels": True})
                rv.set_pr_state("o/r", 5, "approved")          # must not raise
        with mock.patch.object(rv, "run", side_effect=RuntimeError("gh exploded")):
            self.assertIsNone(rv.set_pr_state("o/r", 5, "approved"))
            rv.restore_pr_state("o/r", 5, ["review: approved"])

    def test_restore_puts_back_previous_and_drops_in_progress(self):
        calls = []
        with mock.patch.object(rv, "run", side_effect=fake_gh(["review: in progress"], calls=calls)):
            rv.restore_pr_state("o/r", 5, ["review: changes requested"])
        posts = [c for c in calls if "POST" in c]
        self.assertIn("labels[]=review: changes requested", posts[0])
        self.assertTrue(any("DELETE" in c for c in calls))


class Derive(unittest.TestCase):
    def st(self, verdict, new=(), ):
        return {"latest": {"verdict": verdict}, "new_commits": [{"oid": o} for o in new]}

    def test_states(self):
        d = rv.derive_state
        self.assertEqual(d({"latest": None}, {}, 2, False)[0], None)
        self.assertEqual(d(self.st("APPROVE"), {}, 2, False)[0], "approved")
        self.assertEqual(d(self.st("APPROVE"), {}, 2, True)[0], "needs_human")
        self.assertEqual(d(self.st("COMMENTED"), {}, 2, True)[0], "needs_human")
        self.assertIsNone(d(self.st("COMMENTED"), {}, 2, False)[0])
        self.assertEqual(d(self.st("REQUEST_CHANGES"), {}, 2, False)[0], "changes_requested")
        fs = {"attempts": [{"result": "pushed", "pushed_sha": "ABC"}]}
        self.assertEqual(d(self.st("REQUEST_CHANGES", ["abc"]), fs, 2, False)[0], "fixing")
        self.assertIsNone(d(self.st("REQUEST_CHANGES", ["zzz"]), fs, 2, False)[0])
        self.assertEqual(d(self.st("REQUEST_CHANGES"), {"attempts": [{"result": "no_changes"}]}, 2, False)[0],
                         "fix_limit")
        self.assertEqual(d(self.st("REQUEST_CHANGES"), {"attempts": [{"result": "pushed"}] * 2}, 2, False)[0],
                         "fix_limit")


if __name__ == "__main__":
    unittest.main()
