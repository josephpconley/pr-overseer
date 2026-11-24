"""merge_on_approve and self_review candidate listing (review own PRs, never merge)."""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rv", HERE / "review.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)

LOGIN = "example-user"


def pr(number, login, draft=False, title="t"):
    return {"number": number, "title": title, "url": f"https://example.invalid/{number}",
            "headRefName": f"b{number}", "isDraft": draft, "author": {"login": login}}


class RepoMergeOnApprove(unittest.TestCase):
    def test_default_true_when_omitted(self):
        self.assertTrue(rv.repo_merge_on_approve({"review_mode": "self_review"}))
        self.assertTrue(rv.repo_merge_on_approve({"review_mode": "self_review", "merge_method": "squash"}))

    def test_explicit_false(self):
        self.assertFalse(rv.repo_merge_on_approve({"merge_on_approve": False}))
        self.assertFalse(rv.repo_merge_on_approve({"merge_on_approve": "false"}))
        self.assertFalse(rv.repo_merge_on_approve({"merge_on_approve": "off"}))

    def test_explicit_true(self):
        self.assertTrue(rv.repo_merge_on_approve({"merge_on_approve": True}))
        self.assertTrue(rv.repo_merge_on_approve({"merge_on_approve": "true"}))

    def test_merge_method_none_disables(self):
        self.assertFalse(rv.repo_merge_on_approve({"merge_method": "none"}))
        self.assertFalse(rv.repo_merge_on_approve({"merge_method": "off"}))
        self.assertFalse(rv.repo_merge_on_approve({"merge_method": "no"}))
        # Dependabot path still gets a real method
        self.assertEqual(rv.repo_merge_method({"merge_method": "none"}), "squash")

    def test_explicit_flag_wins_over_method(self):
        self.assertFalse(rv.repo_merge_on_approve({"merge_on_approve": False, "merge_method": "squash"}))
        self.assertTrue(rv.repo_merge_on_approve({"merge_on_approve": True, "merge_method": "none"}))


class PlanActionMergeOnApprove(unittest.TestCase):
    def setUp(self):
        self.own = pr(202, LOGIN)
        self.other = pr(95, "someone-else")
        self.draft = pr(1, LOGIN, draft=True)

    def test_default_self_review_own_approve_merges(self):
        self.assertEqual(rv.plan_action("self_review", "APPROVE", self.own, LOGIN), "merge")

    def test_disabled_never_merges(self):
        self.assertEqual(rv.plan_action("self_review", "APPROVE", self.own, LOGIN,
                                        merge_on_approve=False), "review")
        self.assertEqual(rv.plan_action("self_review", "REQUEST_CHANGES", self.own, LOGIN,
                                        merge_on_approve=False), "review")
        self.assertEqual(rv.plan_action("self_review", "APPROVE", self.other, LOGIN,
                                        merge_on_approve=True), "review")
        self.assertEqual(rv.plan_action("review", "APPROVE", self.own, LOGIN,
                                        merge_on_approve=True), "review")
        self.assertEqual(rv.plan_action("self_review", "APPROVE", self.draft, LOGIN), "review")


class ListCandidatePrs(unittest.TestCase):
    def test_review_mode_is_requested_only(self):
        requested = [pr(10, "alice", title="req")]
        with mock.patch.object(rv, "list_review_requested_prs", return_value=requested) as req, \
             mock.patch.object(rv, "list_self_review_prs", side_effect=AssertionError("should not list own")):
            prs, empty = rv.list_candidate_prs("o/r", LOGIN, "review")
        self.assertEqual(prs, requested)
        self.assertIn("requesting your review", empty)
        req.assert_called_once_with("o/r", LOGIN)

    def test_self_review_unions_own_and_requested(self):
        own = [pr(202, LOGIN, title="mine"), pr(10, LOGIN, title="also mine")]
        requested = [pr(10, LOGIN, title="dup of own"), pr(95, "alice", title="theirs")]
        with mock.patch.object(rv, "list_self_review_prs", return_value=own), \
             mock.patch.object(rv, "list_review_requested_prs", return_value=requested):
            prs, empty = rv.list_candidate_prs("example-org/example-repo", LOGIN, "self_review")
        nums = [p["number"] for p in prs]
        self.assertEqual(nums, [10, 95, 202])
        self.assertEqual(prs[0]["title"], "also mine")  # own copy kept on dup
        self.assertIn("no open non-draft PRs", empty)

    def test_self_review_empty_message(self):
        with mock.patch.object(rv, "list_self_review_prs", return_value=[]), \
             mock.patch.object(rv, "list_review_requested_prs", return_value=[]):
            prs, empty = rv.list_candidate_prs("o/r", LOGIN, "self_review")
        self.assertEqual(prs, [])
        self.assertIn(LOGIN, empty)
        self.assertIn("requesting your review", empty)


if __name__ == "__main__":
    unittest.main()
