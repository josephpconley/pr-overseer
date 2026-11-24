"""Regression test (2026-11-06): quota detection must not trip on review text.
Run: python3 test_quota_detect.py"""
import importlib.util, subprocess, unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location("rv", Path(__file__).resolve().parent / "review.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)
CP = lambda rc, out="", err="": subprocess.CompletedProcess(["x"], rc, out, err)
REVIEW = ("## Review of MIL-1778: retry Cloud Logging quota errors\n" + "The retry on 429 / rate limit / "
          "resource exhausted looks right; billing capacity unaffected.\n" * 20 + "\nVERDICT: APPROVE\n")

class T(unittest.TestCase):
    def test_clean_review_mentioning_quota_is_not_quota(self):
        self.assertFalse(rv.looks_like_quota(CP(0, REVIEW)))
    def test_short_clean_verdict_mentioning_quota(self):
        self.assertFalse(rv.looks_like_quota(CP(0, "quota retry fine.\nVERDICT: APPROVE")))
    def test_long_fix_output_mentioning_rate_limit(self):
        self.assertFalse(rv.looks_like_quota(CP(0, "fixed rate limit handling\n" * 40)))
    def test_real_quota_stderr_nonzero(self):
        self.assertTrue(rv.looks_like_quota(CP(1, "", "Error: You've hit your usage limit. Resets at 5:52pm")))
    def test_real_quota_banner_on_stdout_exit0(self):
        self.assertTrue(rv.looks_like_quota(CP(0, "You've hit your usage limit for this period.")))
    def test_rc_429(self):
        self.assertTrue(rv.looks_like_quota(CP(429)))
    def test_nonzero_with_long_review_body_only_checks_stderr(self):
        self.assertFalse(rv.looks_like_quota(CP(1, REVIEW, "some unrelated crash")))
        self.assertTrue(rv.looks_like_quota(CP(1, REVIEW, "429 Too Many Requests")))
    def test_ordinary_failure_not_quota(self):
        self.assertFalse(rv.looks_like_quota(CP(2, "", "segfault")))

if __name__ == "__main__":
    unittest.main(verbosity=2)
