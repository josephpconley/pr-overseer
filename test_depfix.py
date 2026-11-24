#!/usr/bin/env python3
"""Manual Dependabot fix path (--dependabot --address-feedback --repo X --pr N). All external calls mocked."""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review as rv  # noqa: E402


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class VerifyDirs(unittest.TestCase):
    def test_dirs(self):
        self.assertEqual(rv.js_verify_dirs(["frontend/package.json", "frontend/package-lock.json", "README.md"]),
                         ["frontend"])
        self.assertEqual(rv.js_verify_dirs(["package.json", "a/pnpm-lock.yaml"]), ["", "a"])
        self.assertEqual(rv.js_verify_dirs(["requirements.txt"]), [])


class CiExcerpts(unittest.TestCase):
    def test_latest_failing_job_only(self):
        rollup = [
            {"name": "check", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/r/actions/runs/1/job/10"},
            {"name": "check", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/r/actions/runs/2/job/20"},
            {"name": "lint", "conclusion": "SUCCESS", "detailsUrl": "https://github.com/o/r/actions/runs/2/job/21"},
        ]
        log = "2026-11-13T14:02:12.07Z ##[error]lib/x.ts(1,1): error TS2353: nope\n2026-11-13T14:02:12.1Z done\n"
        with mock.patch.object(rv, "run", return_value=_cp(0, log)) as r:
            txt = rv.failed_ci_excerpts("o/r", rollup)
        self.assertEqual(r.call_count, 1)
        self.assertIn("repos/o/r/actions/jobs/20/logs", r.call_args[0][0])
        self.assertIn("error TS2353", txt)
        self.assertNotIn("2026-11-13T", txt)

    def test_none(self):
        self.assertIn("no failing", rv.failed_ci_excerpts("o/r", [{"name": "x", "conclusion": "SUCCESS"}]))


class JsVerify(unittest.TestCase):
    def test_runs_install_then_existing_scripts_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            (wt / "fe").mkdir()
            (wt / "fe/package.json").write_text(json.dumps({"scripts": {"typecheck": "tsc", "test": "vitest", "build": "b"}}))
            (wt / "fe/package-lock.json").write_text("{}")
            calls = []

            def fake(cmd, **kw):
                calls.append(cmd)
                return _cp(1 if cmd[-1] == "test" else 0, "boom" if cmd[-1] == "test" else "")
            with mock.patch.object(rv.subprocess, "run", side_effect=fake):
                ok, note = rv.make_js_verify(["fe"])(wt)
        self.assertFalse(ok)
        self.assertEqual(calls, [["npm", "ci", "--no-audit", "--no-fund"], ["npm", "run", "typecheck"],
                                 ["npm", "run", "test"]])
        self.assertIn("npm run test exit 1", note)


class Prompt(unittest.TestCase):
    def test_keeps_bump(self):
        p = rv.build_dependabot_fix_prompt("o/r", {"number": 9, "title": "Bump x", "headRefName": "dependabot/x",
                                                    "body": "notes"}, "TS2353", ["frontend"])
        self.assertIn("KEEP the version bump", p)
        self.assertIn("TS2353", p)
        self.assertIn("frontend", p)
        self.assertIn("FIX_STATUS: APPLIED", p)


class FixDependabotOne(unittest.TestCase):
    def _args(self, **kw):
        a = mock.Mock(pr=91, no_push=False, force=False, max_fix_attempts=None)
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_refuses_non_dependabot(self):
        meta = {"author": {"login": "someone"}, "state": "OPEN"}
        with mock.patch.object(rv, "fetch_dependabot_meta", return_value=meta), \
                mock.patch.object(rv, "fix_one") as fo:
            self.assertEqual(rv.fix_dependabot_one(self._args(), {"repo": "o/r", "path": "/tmp"}, Path("h")), 1)
        fo.assert_not_called()

    def test_green_ci_noop(self):
        meta = {"author": {"login": "app/dependabot"}, "state": "OPEN", "title": "Bump x",
                "statusCheckRollup": [{"name": "c", "conclusion": "SUCCESS", "status": "COMPLETED"}]}
        with mock.patch.object(rv, "fetch_dependabot_meta", return_value=meta), \
                mock.patch.object(rv, "is_dependabot_pr", return_value=True), \
                mock.patch.object(rv, "fix_one") as fo:
            self.assertEqual(rv.fix_dependabot_one(self._args(), {"repo": "o/r", "path": "/tmp"}, Path("h")), 0)
        fo.assert_not_called()

    def test_calls_fix_one_with_overrides(self):
        meta = {"author": {"login": "app/dependabot"}, "state": "OPEN", "title": "Bump glob and @sentry/nextjs in /frontend",
                "headRefName": "dependabot/npm/x", "headRefOid": "abc", "url": "u", "body": "b",
                "files": [{"path": "frontend/package.json"}, {"path": "frontend/package-lock.json"}],
                "statusCheckRollup": [{"name": "c", "conclusion": "FAILURE", "status": "COMPLETED",
                                       "detailsUrl": "https://github.com/o/r/actions/runs/1/job/2"}]}
        with mock.patch.object(rv, "fetch_dependabot_meta", return_value=meta), \
                mock.patch.object(rv, "is_dependabot_pr", return_value=True), \
                mock.patch.object(rv, "failed_ci_excerpts", return_value="TS2353"), \
                mock.patch.object(rv, "fix_one", return_value=Path("art")) as fo:
            self.assertEqual(rv.fix_dependabot_one(self._args(), {"repo": "o/r", "path": "/tmp"}, Path("h")), 0)
        kw = fo.call_args.kwargs
        self.assertTrue(kw["push"])
        self.assertIn("TS2353", kw["prompt_override"])
        self.assertTrue(kw["commit_msg_override"].startswith("fix(deps): make CI pass with bump glob"))
        self.assertLessEqual(len(kw["commit_msg_override"]), 72)
        self.assertIsNotNone(kw["verify"])
        self.assertEqual(fo.call_args.args[3]["head"], "abc")


class SummaryLinks(unittest.TestCase):
    def test_local_links_stripped(self):
        import re
        src = "fixed [sentry.ts](/home/u/wt/PR-1/frontend/lib/sentry.ts:29) and [docs](https://x.y/z)"
        out = re.sub(r"\[([^\]]+)\]\((?:/|file:)[^)]*\)", r"\1", src)
        self.assertEqual(out, "fixed sentry.ts and [docs](https://x.y/z)")
        self.assertIn('summary = re.sub(r"\\[([^\\]]+)\\]\\((?:/|file:)[^)]*\\)", r"\\1", summary)',
                      Path(rv.__file__).read_text())


class Cli(unittest.TestCase):
    def _main(self, argv):
        with mock.patch.object(sys, "argv", ["review.py", *argv]), self.assertRaises(SystemExit) as e, \
                mock.patch("sys.stderr"):
            rv.main()
        return e.exception.code

    def test_guards(self):
        self.assertEqual(self._main(["--dependabot", "--address-feedback", "--repo", "o/r"]), 2)
        self.assertEqual(self._main(["--dependabot", "--address-feedback", "--repo", "o/r", "--pr", "1", "--live"]), 2)


if __name__ == "__main__":
    unittest.main()
