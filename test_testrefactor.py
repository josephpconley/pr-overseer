"""Unit tests for the weekly test-suite review/refactor (testrefactor.py) and its merge hold in review.py.

Everything external is mocked or pure: no gh, no git worktrees, no harness, no GitHub writes.
Run:  python3 test_testrefactor.py
"""
import importlib.util, io, json, sys, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rv", HERE / "review.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)
spec2 = importlib.util.spec_from_file_location("trf", HERE / "testrefactor.py")
tr = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(tr)
tr.bind(rv)

VITEST_LOG = """\
2026-11-14T19:51:32.6189118Z $ vitest run
2026-11-14T19:51:32.6221287Z \x1b[1m RUN \x1b[22m v5.0.0 /home/runner/work/example-repo/example-repo/packages/graph
2026-11-14T19:51:32.6287645Z  \x1b[32m✓\x1b[39m src/client.test.ts \x1b[2m(\x1b[22m\x1b[2m6 tests\x1b[22m\x1b[2m)\x1b[22m\x1b[33m 323\x1b[2mms\x1b[22m\x1b[39m
2026-11-14T19:51:32.6472665Z  \x1b[32m✓\x1b[39m tests/scripts/prove-dedupe.test.ts \x1b[2m(\x1b[22m\x1b[2m1 test\x1b[22m\x1b[2m)\x1b[22m\x1b[33m 2153\x1b[2mms\x1b[22m\x1b[39m
2026-11-14T19:51:32.6490229Z    \x1b[33m\x1b[2m✓\x1b[22m\x1b[39m prove-dedupe refuses to run (retired)\x1b[33m 2150\x1b[2mms\x1b[22m\x1b[39m
2026-11-14T19:51:39.2451203Z  RUN  v5.0.0 /home/runner/work/example-repo/example-repo/apps/http-server
2026-11-14T19:51:54.4322029Z  ✓ tests/alerts/dispatch-stopped-alert.test.ts (14 tests | 1 skipped) 4ms
2026-11-14T19:51:50.8939530Z  ✓ tests/friday/matches.test.ts (5 tests) 3326ms
2026-11-14T19:51:50.8940277Z    ✓ friday-preview (3)
2026-11-14T19:51:50.8941024Z      ✓ writes the same emails 3238ms
2026-11-14T19:51:51.0000000Z  Duration  9.17s (import 55%, tests 36%)
"""


class CiParse(unittest.TestCase):
    def test_vitest_log_files_and_slow_tests(self):
        p = tr.parse_ci_log(VITEST_LOG)
        self.assertAlmostEqual(p["files"]["packages/graph/src/client.test.ts"], 0.323)
        self.assertAlmostEqual(p["files"]["packages/graph/tests/scripts/prove-dedupe.test.ts"], 2.153)
        self.assertAlmostEqual(p["files"]["apps/http-server/tests/alerts/dispatch-stopped-alert.test.ts"], 0.004)
        self.assertAlmostEqual(
            p["tests"]["packages/graph/tests/scripts/prove-dedupe.test.ts::prove-dedupe refuses to run (retired)"], 2.15)
        self.assertAlmostEqual(p["tests"]["apps/http-server/tests/friday/matches.test.ts::writes the same emails"], 3.238)
        self.assertNotIn("apps/http-server/tests/friday/matches.test.ts::friday-preview (3)", p["tests"])

    def test_pytest_and_jest_lines(self):
        log = ("rootdir: /home/runner/work/x/x/svc\n"
               "1.50s call     tests/test_a.py::test_slow\n"
               "0.20s setup    tests/test_a.py::test_slow\n"
               "PASS src/foo.test.js (2.5 s)\n")
        p = tr.parse_ci_log(log)
        self.assertAlmostEqual(p["tests"]["svc/tests/test_a.py::test_slow"], 1.7)
        self.assertAlmostEqual(p["files"]["svc/src/foo.test.js"], 2.5)

    def test_junit(self):
        xml = ('<testsuites><testsuite><testcase classname="tests.test_a" name="t1" time="1.25"/>'
               '<testcase classname="tests.test_a.TestX" name="t2" time="0.5"><skipped/></testcase>'
               '<testcase file="src/b.test.ts" name="t3" time="0.1"><failure/></testcase></testsuite></testsuites>')
        p = tr.parse_junit(xml)
        self.assertAlmostEqual(p["files"]["tests/test_a.py"], 1.75)
        self.assertIn("tests/test_a.py::t2", p["skipped"])
        self.assertIn("src/b.test.ts::t3", p["failed"])

    def test_runner_rel_and_trend(self):
        self.assertEqual(tr.runner_rel("/home/runner/work/r/r/packages/x"), "packages/x")
        self.assertEqual(tr.runner_rel("/home/runner/work/r/r"), "")
        self.assertAlmostEqual(tr.trend_pct([10, 10, 20, 20]), 100.0)
        self.assertIsNone(tr.trend_pct([1, 2]))


class GatherCi(unittest.TestCase):
    def test_dependabot_runs_dropped_and_jobs_steps_aggregated(self):
        runs = {"workflow_runs": [
            {"id": 1, "name": "CI", "event": "push", "conclusion": "success", "actor": {"login": "me"},
             "run_started_at": "2026-11-10T10:00:00Z", "updated_at": "2026-11-10T10:02:00Z"},
            {"id": 2, "name": "CI", "event": "push", "conclusion": "success", "actor": {"login": "me"},
             "run_started_at": "2026-11-11T10:00:00Z", "updated_at": "2026-11-11T10:03:00Z"},
            {"id": 3, "name": "npm_and_yarn in /. for x", "event": "dynamic", "conclusion": "success",
             "actor": {"login": "dependabot[bot]"}, "run_started_at": "2026-11-11T10:00:00Z",
             "updated_at": "2026-11-11T10:09:00Z"}]}
        jobs = {"jobs": [{"name": "verify", "conclusion": "success", "started_at": "2026-11-10T10:00:00Z",
                          "completed_at": "2026-11-10T10:01:40Z",
                          "steps": [{"name": "Verify all packages", "conclusion": "success",
                                     "started_at": "2026-11-10T10:00:10Z", "completed_at": "2026-11-10T10:01:30Z"},
                                    {"name": "Checkout", "conclusion": "success",
                                     "started_at": "2026-11-10T10:00:00Z", "completed_at": "2026-11-10T10:00:05Z"}]}]}

        def fake_gh(path, timeout=120):
            if "/jobs" in path:
                return jobs
            if "/artifacts" in path:
                return {"artifacts": []}
            return runs
        with mock.patch.object(tr, "gh_json", side_effect=fake_gh), mock.patch.object(tr, "gh_bytes", return_value=None):
            ci = tr.gather_ci("o/r", "main", dict(tr.DEFAULTS))
        self.assertEqual([w["name"] for w in ci["workflows"]], ["CI"])
        self.assertEqual(ci["runs_total"], 2)
        self.assertEqual(ci["jobs"][0]["job"], "verify")
        self.assertAlmostEqual(ci["jobs"][0]["median_s"], 100.0)
        self.assertEqual([s["step"] for s in ci["test_steps"]], ["Verify all packages"])
        self.assertAlmostEqual(ci["test_steps"][0]["median_s"], 80.0)

    def test_gh_failure_is_a_note(self):
        with mock.patch.object(tr, "gh_json", side_effect=tr.GhError("boom")):
            ci = tr.gather_ci("o/r", "main", dict(tr.DEFAULTS))
        self.assertTrue(any("unavailable" in n for n in ci["notes"]))


class Paths(unittest.TestCase):
    def test_is_test_path(self):
        yes = ["packages/x/tests/a.test.ts", "src/foo.spec.js", "test/a.test.js", "tests/test_x.py", "pkg/conftest.py",
               "packages/x/vitest.config.ts", "fixtures/ctm/a.json", "src/__snapshots__/a.snap", "a_test.go"]
        no = ["src/foo.ts", "package.json", "pnpm-lock.yaml", "scripts/verify/check-test-layout.ts",
              ".github/workflows/ci.yml", "addon/util.js"]
        for p in yes:
            self.assertTrue(tr.is_test_path(p), p)
        for p in no:
            self.assertFalse(tr.is_test_path(p), p)
        self.assertTrue(tr.is_test_path(".github/workflows/ci.yml", allow_ci=True))

    def test_node_script_files_and_config(self):
        self.assertEqual(tr.node_script_files("node test/a.test.js && node test/b.test.js"),
                         ["test/a.test.js", "test/b.test.js"])
        self.assertIsNone(tr.node_script_files("vitest run"))
        self.assertIsNone(tr.repo_config({"repo": "a"}))
        c = tr.repo_config({"repo": "a", "test_refactor": {"top_n": 3}}, {"test_refactor_defaults": {"ci_days": 14}})
        self.assertEqual((c["top_n"], c["ci_days"], c["max_changed_lines"]), (3, 14, 300))
        self.assertIsNone(tr.repo_config({"repo": "a", "test_refactor": {"enabled": False}}))


class Coverage(unittest.TestCase):
    def test_covered_intervals_innermost_wins(self):
        # function 0-100 run once, block 20-40 never run, nested 25-30 run (count 2)
        iv = tr.covered_intervals([(0, 100, 1), (20, 40, 0), (25, 30, 2)])
        self.assertEqual(iv, [(0, 20), (25, 30), (40, 100)])
        self.assertEqual(tr.covered_intervals([(0, 50, 0)]), [])

    def test_unique_and_pct(self):
        full = {"a.js": [(0, 100)], "b.js": [(0, 10)]}
        without = {"a.js": [(0, 40), (60, 100)]}
        self.assertEqual(tr.unique_bytes(full, without), 20 + 10)
        self.assertEqual(tr.cov_total(full), 110)
        self.assertAlmostEqual(tr.v8_pct({"a.js": [(0, 50)]}, {"a.js": 100, "b.js": 100}), 25.0)
        self.assertEqual(tr.merge_cov([{"a": [(0, 5)]}, {"a": [(3, 9)], "b": [(1, 2)]}]), {"a": [(0, 9)], "b": [(1, 2)]})

    def test_v8_dir_parsing_filters_tests_and_node_modules(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d) / "wt"; (wt / "src").mkdir(parents=True)
            cov = Path(d) / "cov"; cov.mkdir()
            data = {"result": [
                {"url": f"file://{wt}/src/a.js", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 10, "count": 1}]}]},
                {"url": f"file://{wt}/test/a.test.js", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 9, "count": 1}]}]},
                {"url": f"file://{wt}/node_modules/x/i.js", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 9, "count": 1}]}]},
                {"url": "node:fs", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 9, "count": 1}]}]},
                {"url": "util.js", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 30, "count": 1},
                                                             {"startOffset": 10, "endOffset": 20, "count": 0}]}]}]}
            (cov / "coverage-1.json").write_text(json.dumps(data))
            m, lengths = tr.v8_coverage(cov, wt)
        self.assertEqual(m, {"src/a.js": [(0, 10)], "util.js": [(0, 10), (20, 30)]})
        self.assertEqual(lengths, {"src/a.js": 10, "util.js": 30})


def _t(f, name, status="passed", dur=0.01):
    return {"id": f"{f}::{name}", "file": f, "name": name, "status": status, "dur_s": dur}


class Scoring(unittest.TestCase):
    def test_rank_combines_cost_and_low_impact(self):
        local_files = {"a.test.ts": 8.0, "b.test.ts": 0.2, "c.test.ts": 0.1, "d.test.ts": 0.05}
        tests = ([_t("a.test.ts", "slow one", dur=7.5), _t("a.test.ts", "fast")]
                 + [_t("b.test.ts", f"t{i}") for i in range(3)]
                 + [_t("c.test.ts", "skipped", "skipped"), _t("c.test.ts", "todo", "todo")]
                 + [_t("d.test.ts", "x")])
        static = {"a.test.ts": tr.static_scan("it('slow one', async () => { await sleep(2000); expect(1).toBe(1) })"),
                  "b.test.ts": tr.static_scan("it('t', () => expect(1).toBe(1))"),
                  "c.test.ts": tr.static_scan("it.skip('skipped', () => {})")}
        uniq = {"b.test.ts": {"unique": 0, "total": 1000}, "a.test.ts": {"unique": 500, "total": 1000}}
        ranked = tr.score_candidates(local_files, tests, {"a.test.ts": 12.0, "ci-only.test.ts": 30.0},
                                     {"a.test.ts::slow one": 11.0}, static, uniq, set())
        by = {c["file"]: c for c in ranked}
        self.assertEqual(ranked[0]["file"], "ci-only.test.ts")           # most expensive overall
        self.assertFalse(by["ci-only.test.ts"]["local"])
        self.assertEqual(by["a.test.ts"]["suggested"], "REBUILD")
        self.assertEqual(by["a.test.ts"]["cost_s"], 12.0)                # max(CI, local)
        self.assertTrue(any("sleep" in h for h in by["a.test.ts"]["hints"]))
        self.assertTrue(any("dominates" in h for h in by["a.test.ts"]["hints"]))
        self.assertEqual(by["b.test.ts"]["suggested"], "DELETE")         # no unique coverage
        self.assertEqual(by["c.test.ts"]["suggested"], "DELETE")         # all skipped
        self.assertEqual(by["d.test.ts"]["suggested"], "REVIEW")         # cheap and fine
        picks = tr.pick_harness_candidates(ranked, 5)
        self.assertNotIn("ci-only.test.ts", [c["file"] for c in picks])
        self.assertNotIn("d.test.ts", [c["file"] for c in picks])

    def test_flaky_and_min_rebuild(self):
        ranked = tr.score_candidates({"f.test.ts": 0.3, "g.test.ts": 0.4}, [_t("f.test.ts", "a"), _t("g.test.ts", "b")],
                                     {}, {}, {}, {}, {"f.test.ts"})
        by = {c["file"]: c for c in ranked}
        self.assertEqual(by["f.test.ts"]["suggested"], "REBUILD")
        self.assertEqual(by["g.test.ts"]["suggested"], "REVIEW")   # relatively slow but < min_rebuild_s
        self.assertEqual(ranked[0]["file"], "f.test.ts")

    def test_static_scan(self):
        s = tr.static_scan("describe('x', () => {\n it('a', () => {}); it('a', () => {});\n"
                           " xit('b', () => {}); vi.mock('y'); // retired helper\n setTimeout(done, 1500)\n})")
        self.assertEqual(s["dup_names"], ["a"])
        self.assertTrue(s["retired"])
        self.assertGreaterEqual(s["skips"], 1)
        self.assertEqual(s["sleeps"], 1)
        self.assertEqual(s["asserts"], 0)


class PytestCollection(unittest.TestCase):
    XML = """<testsuites><testsuite name="pytest" errors="1" skipped="1" tests="2">
<testcase classname="" name="pkg.tests.test_main" time="0.000"><error message="collection failure">x</error></testcase>
<testcase classname="" name="pkg.tests.test_pg" time="0.000"><skipped message="no pg"/></testcase>
</testsuite></testsuites>"""

    def test_parse_junit_module_entries(self):
        p = tr.parse_junit(self.XML)
        self.assertEqual(p["collect_errors"], ["pkg/tests/test_main.py"])
        self.assertNotIn("", p["files"])
        self.assertEqual(p["skipped"], ["pkg/tests/test_pg.py::(module skipped)"])
        self.assertEqual(p["failed"], [])

    def test_run_pytest_collection_error_is_not_green(self):
        with tempfile.TemporaryDirectory() as d:
            wt, out = Path(d), Path(d) / "out"
            out.mkdir()

            def fake_run(cmd, **kw):
                if "-c" in cmd:
                    return mock.Mock(returncode=1)
                (out / "pytest-before.xml").write_text(self.XML)
                return mock.Mock(returncode=2, stdout="Interrupted: 1 error during collection", stderr="")
            with mock.patch.object(tr.subprocess, "run", side_effect=fake_run):
                r = tr.run_pytest(wt, {"kind": "pytest", "dir": ""}, out, "before", wt)
        self.assertFalse(r["ok"])
        self.assertIn("collection failed", r["error"])

    def test_rerun_that_ran_nothing_is_not_flaky(self):
        first = {"suite": {}, "rc": 1, "wall_s": 1, "error": None, "ok": False, "files": {"t_test.py": 1.0},
                 "tests": [{"id": "t_test.py::a", "file": "t_test.py", "name": "a", "status": "failed", "dur_s": 1}]}
        rerun = {"suite": {}, "rc": 5, "wall_s": 1, "error": None, "ok": True, "files": {}, "tests": []}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(tr, "run_pytest", side_effect=[first, rerun]):
            m = tr.measure(Path(d), Path(d), [{"kind": "pytest", "dir": ""}], Path(d), "before", dict(tr.DEFAULTS))
        self.assertEqual(m["flaky"], [])
        self.assertEqual(m["still_failing"], ["t_test.py"])
        self.assertFalse(m["ok"])

    def test_pick_skips_empty_path(self):
        ranked = [{"file": "", "local": True, "suggested": "REBUILD"},
                  {"file": "a.test.ts", "local": True, "suggested": "DELETE"}]
        self.assertEqual([c["file"] for c in tr.pick_harness_candidates(ranked, 5)], ["a.test.ts"])


class Guardrails(unittest.TestCase):
    CFG = dict(tr.DEFAULTS)

    def _run(self, ok=True, per_file=None, t=10.0, failed=()):
        pf = per_file or {"a.test.ts": 3, "b.test.ts": 2}
        res = [{"tests": [{"file": f, "name": f"t{i}", "status": "passed"} for f, k in pf.items() for i in range(k)]}]
        return {"ok": ok, "per_file": pf, "test_time_s": t, "tests": sum(pf.values()), "failed": list(failed),
                "results": res}

    def test_edited_test_that_did_not_run(self):
        after = self._run()
        after["results"][0]["tests"].append({"file": "c_test.py", "name": "x", "status": "skipped"})
        ok, fails, _ = tr.check_guardrails(self._run(), after, ["a.test.ts", "c_test.py", "tests/support/db.ts"],
                                           10, [], self.CFG)
        self.assertFalse(ok)
        self.assertTrue(any("did not run locally" in f and "c_test.py" in f and "a.test.ts" not in f
                            and "db.ts" not in f for f in fails), fails)

    def test_pass(self):
        ok, fails, m = tr.check_guardrails(self._run(), self._run(per_file={"a.test.ts": 3}, t=6.0),
                                           ["b.test.ts", "a.test.ts"], 120,
                                           [{"file": "b.test.ts", "action": "DELETE", "why": "dup"}], self.CFG, 80.0, 79.8)
        self.assertTrue(ok, fails)
        self.assertAlmostEqual(m["saved_s"], 4.0)

    def test_each_failure(self):
        before = self._run()
        cases = {
            "not green after": (self._run(ok=False, failed=["a::x"]), ["a.test.ts"], 10, [], None, None),
            "non-test files": (self._run(), ["src/app.ts", "a.test.ts"], 10, [], None, None),
            "cap": (self._run(), ["a.test.ts"], 301, [], None, None),
            "coverage dropped": (self._run(), ["a.test.ts"], 10, [], 80.0, 79.0),
            "without a DELETE": (self._run(per_file={"a.test.ts": 2, "b.test.ts": 2}), ["a.test.ts"], 10, [], None, None),
            "worse": (self._run(t=12.0), ["a.test.ts"], 10, [], None, None),
            "no changes": (self._run(), [], 0, [], None, None),
        }
        for key, (after, changed, lines, dec, cb, ca) in cases.items():
            ok, fails, _ = tr.check_guardrails(before, after, changed, lines, dec, self.CFG, cb, ca)
            self.assertFalse(ok, key)
            self.assertTrue(any(key in f for f in fails), (key, fails))
        ok, fails, _ = tr.check_guardrails(self._run(ok=False), self._run(), ["a.test.ts"], 5, [], self.CFG)
        self.assertTrue(any("before" in f for f in fails))

    def test_deleted_unique_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            dec = [{"file": "x.test.ts", "action": "DELETE", "why": "w"}, {"file": "y.test.ts", "action": "DELETE", "why": "w"}]
            uniq = {"x.test.ts": {"unique": 500, "total": 10000}, "y.test.ts": {"unique": 0, "total": 10000}}
            f = tr.deleted_unique_failures(dec, uniq, wt, 0.5)
        self.assertEqual(len(f), 1)
        self.assertIn("x.test.ts", f[0])

    def test_parse_decisions(self):
        out = ("DECISION: `a.test.ts` | REBUILD | fake timers \u2014 same checks\n"
               "- DECISION: b.test.ts | delete | duplicated by c\nDECISION: bad line\nFIX_STATUS: APPLIED\n")
        d = tr.parse_decisions(out)
        self.assertEqual([(x["file"], x["action"]) for x in d], [("a.test.ts", "REBUILD"), ("b.test.ts", "DELETE")])
        self.assertNotIn("\u2014", d[0]["why"])

    def test_summarize_run_and_jest_json(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            data = {"testResults": [{"name": str(wt / "pkg/a.test.ts"), "startTime": 1000, "endTime": 3500,
                                     "assertionResults": [{"fullName": "a ok", "status": "passed", "duration": 2000},
                                                          {"fullName": "a skip", "status": "pending", "duration": None}]}]}
            p = tr.parse_jest_json(data, wt)
        self.assertEqual(p["files"], {"pkg/a.test.ts": 2.5})
        self.assertEqual([t["status"] for t in p["tests"]], ["passed", "skipped"])
        s = tr.summarize_run([{"ok": True, "tests": p["tests"], "files": p["files"], "wall_s": 3}])
        self.assertEqual((s["tests"], s["passed"], len(s["skipped"]), s["per_file"]), (1, 1, 1, {"pkg/a.test.ts": 1}))


class Prompt(unittest.TestCase):
    def test_prompt_and_body(self):
        c = {"file": "a.test.ts", "suggested": "REBUILD", "cost_s": 5.0, "local_s": 4.0, "ci_s": 5.0, "tests": 3,
             "skipped": 0, "reasons": ["r"], "hints": ["h"], "slow_tests": [("slow", 3.0, 4.0)], "low_impact": 0,
             "flaky": False, "local": True, "score": 1}
        ci = {"days": 21, "branch": "main", "test_steps": [{"workflow": "CI", "job": "v", "step": "Test", "median_s": 90}],
              "jobs": [{"workflow": "CI", "job": "v", "median_s": 100, "p90_s": 120, "trend_pct": 10.0}]}
        p = tr.build_prompt("o/r", "main", [c], ci, dict(tr.DEFAULTS))
        self.assertIn("DECISION: <path>", p)
        self.assertIn("At most 300 changed lines", p)
        self.assertIn("NEVER edit production", p)
        body = tr.pr_body("o/r", "main", "a" * 40, [c], [{"file": "a.test.ts", "action": "REBUILD", "why": "faster"}],
                          {"before_s": 10, "after_s": 6, "saved_s": 4, "tests_before": 3, "tests_after": 3,
                           "cov_before": 80.0, "cov_after": 80.0, "changed_lines": 40, "changed_files": 1},
                          {"files": {"a.test.ts": 4.0}, "per_file": {"a.test.ts": 3}},
                          {"files": {"a.test.ts": 1.0}, "per_file": {"a.test.ts": 3}}, ci, "muse", "m", "summary", {})
        # 2026-11-18: PR text never mentions the overseer / harness / merge-hold instructions
        self.assertNotIn("overseer", body.lower())
        self.assertNotIn("muse", body.lower())
        self.assertNotIn("--release-human-review", body)
        self.assertIn("| `a.test.ts` | REBUILD | 4.00s | 1.00s | 5.00s | 3 -> 3 |", body)


class MergeHold(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ovs-tr-"))
        self.p = mock.patch.object(rv, "FALLBACK_REVIEWS", self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_branch_prefix_holds_without_record(self):
        held, why = rv.test_refactor_merge_hold("o/r", 7, {"headRefName": "overseer/test-refactor-20261115"})
        self.assertTrue(held)
        self.assertIn("--release-human-review", why)
        self.assertEqual(rv.test_refactor_merge_hold("o/r", 7, {"headRefName": "feat/x"}),
                         (False, "not a test-refactor PR"))

    def test_record_holds_until_released(self):
        rp = rv.test_refactor_record_path("o/r", 9)
        rp.parent.mkdir(parents=True)
        rp.write_text(json.dumps({"repo": "o/r", "number": 9}))
        self.assertTrue(rv.test_refactor_merge_hold("o/r", 9, {"headRefName": "anything"})[0])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rv.release_human_review("o/r", 9), 0)
        self.assertIn("released test-refactor hold", out.getvalue())
        held, why = rv.test_refactor_merge_hold("o/r", 9, {"headRefName": "overseer/test-refactor-x"})
        self.assertFalse(held)
        self.assertIn("released", why)
        rp.write_text("{bad")
        self.assertTrue(rv.test_refactor_merge_hold("o/r", 9, None)[0])

    def test_review_one_never_merges_test_refactor_pr(self):
        """self_review APPROVE on an overseer test-refactor PR → comment, no admin_merge (even with hold_merge off)."""
        wt = self.tmp / "wt"; wt.mkdir()
        hpath = self.tmp / "harnesses.json"
        hpath.write_text(json.dumps({"harnesses": [{"id": "claude", "bin": "true", "cooldown_until": None}]}))
        pr = {"number": 5, "title": "test: weekly", "url": "u", "headRefName": "overseer/test-refactor-20261115",
              "isDraft": False, "author": {"login": "me"}}
        cp = mock.Mock(returncode=0, stdout="LGTM\n\nVERDICT: APPROVE\n", stderr="")
        merged = mock.Mock(return_value=(True, "merged"))
        with mock.patch.object(rv, "fetch_ci_status", return_value={"statusCheckRollup": []}), \
             mock.patch.object(rv, "create_pr_worktree", return_value=wt), \
             mock.patch.object(rv, "remove_worktree"), mock.patch.object(rv, "ensure_stubs", return_value=[]), \
             mock.patch.object(rv, "run", return_value=mock.Mock(returncode=0, stdout="a" * 40, stderr="")), \
             mock.patch.object(rv, "fetch_pr_diff", return_value="diff"), \
             mock.patch.object(rv, "apply_tier_for_pr", return_value=""), \
             mock.patch.dict(rv.RUNNERS, {"claude": mock.Mock(return_value=cp)}), \
             mock.patch.object(rv, "write_review", return_value=self.tmp / "r.md"), \
             mock.patch.object(rv, "post_review_comment", return_value=(True, "commented")), \
             mock.patch.object(rv, "load_tiers", return_value={"human_review": {"hold_merge": False}, "tiers": {}}), \
             mock.patch.object(rv, "admin_merge", merged), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            rv.review_one("o/r", self.tmp, pr, {"harnesses": []}, hpath, mode="self_review", login="me",
                          pre=(None, {}, {}))
        merged.assert_not_called()
        self.assertIn("merge held: test-refactor", out.getvalue())


class VitestHook(unittest.TestCase):
    def test_wrapper_merges_base_config(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d)
            self.assertIn("setupFiles: [\"./.overseer-cov-hook.mjs\"]", tr._vitest_wrapper(pkg))
            (pkg / "vitest.config.ts").write_text("export default {}")
            w = tr._vitest_wrapper(pkg)
        self.assertIn('import base from "./vitest.config.ts"', w)
        self.assertIn("[].concat(c.test.setupFiles || [])", w)
        self.assertIn("typeof base === 'function'", w)
        self.assertIn("v8.takeCoverage()", tr.VITEST_HOOK)

    def test_per_testfile_dumps(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d) / "wt"; (wt / "src").mkdir(parents=True); (wt / "tests").mkdir()
            cov = Path(d) / "cov"; cov.mkdir()
            def dump(name, s, e):
                (cov / name).write_text(json.dumps({"result": [{"url": f"file://{wt}/src/a.ts", "functions": [
                    {"ranges": [{"startOffset": 0, "endOffset": 100, "count": 0}, {"startOffset": s, "endOffset": e, "count": 1}]}]}]}))
            h1, h2 = "a" * 16, "b" * 16
            (cov / f"tf-{h1}.path").write_text(str(wt / "tests/one.test.ts"))
            (cov / f"tf-{h2}.path").write_text(str(wt / "tests/two.test.ts"))
            dump(f"tf-{h1}-1-1-0.json", 0, 40)
            dump(f"tf-{h2}-1-1-0.json", 30, 60)
            dump("coverage-9-1-1.json", 90, 100)     # spawned child, not attributable
            per, lengths, rest = tr.v8_per_testfile(cov, wt)
            self.assertEqual(per, {"tests/one.test.ts": {"src/a.ts": [(0, 40)]}, "tests/two.test.ts": {"src/a.ts": [(30, 60)]}})
            self.assertEqual(rest, {"src/a.ts": [(90, 100)]})
            self.assertEqual(lengths, {"src/a.ts": 100})
            u = tr.unique_coverage({"v8_files": per, "v8": tr.merge_cov(list(per.values()) + [rest])},
                                   ["tests/one.test.ts", "tests/two.test.ts"])
        self.assertEqual(u["tests/one.test.ts"]["unique"], 30)
        self.assertEqual(u["tests/two.test.ts"]["unique"], 20)
        self.assertEqual(u["tests/one.test.ts"]["total"], 70)

    def test_spawning_file_with_zero_unique_is_not_low_impact(self):
        st = {"x.test.ts": tr.static_scan("it('a', async () => { spawn('node', ['x']); expect(1).toBe(1) })")}
        r = tr.score_candidates({"x.test.ts": 0.5}, [_t("x.test.ts", "a")], {}, {}, st,
                                {"x.test.ts": {"unique": 0, "total": 100, "covered": 10}}, set())[0]
        self.assertEqual(r["low_impact"], 0)
        self.assertTrue(any("not attributable" in x for x in r["reasons"]))


class CliGuards(unittest.TestCase):
    def test_live_requires_mode(self):
        with mock.patch.object(sys, "argv", ["review.py", "--live"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                rv.main()
        with mock.patch.object(sys, "argv", ["review.py", "--test-refactor", "--dependabot"]), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                rv.main()


if __name__ == "__main__":
    unittest.main(verbosity=1)
