#!/usr/bin/env python3
"""Classifier gate (tiers.json) unit tests — no network, no harness calls."""
import importlib.util, json, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
spec = importlib.util.spec_from_file_location("rv", Path(__file__).resolve().parent / "review.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)
CFG = json.loads((Path(__file__).resolve().parent / "tiers.json").read_text())


def meta(files, labels=(), title="t", head="abc123"):
    return {"title": title, "body": "b", "headRefOid": head, "labels": [{"name": l} for l in labels],
            "files": [{"path": p, "additions": a, "deletions": d} for p, a, d in files],
            "additions": sum(a for _, a, _ in files), "deletions": sum(d for _, _, d in files),
            "changedFiles": len(files)}


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hp = Path(self.tmp.name) / "h.json"
        self.hp.write_text(json.dumps({"default_cooldown_minutes": 15, "harnesses": [
            {"id": "muse", "bin": "muse", "cooldown_until": None},
            {"id": "antigravity", "bin": "agy", "cooldown_until": None}]}))
        p = mock.patch.object(rv, "FALLBACK_REVIEWS", Path(self.tmp.name) / "reviews"); p.start(); self.addCleanup(p.stop)
        self.llm = mock.patch.object(rv, "run_classifier_llm"); self.m_llm = self.llm.start(); self.addCleanup(self.llm.stop)

    def tearDown(self): self.tmp.cleanup(); rv.set_active_tier(None, None)

    def c(self, m, diff="", **kw):
        return rv.classify_pr("o/r", 1, CFG, self.hp, meta=m, diff=diff, **kw)

    def test_docs_only_everyday_rules(self):
        t, r, s = self.c(meta([("README.md", 120, 30), ("docs/setup.md", 80, 0)]))
        self.assertEqual((t, s), ("everyday", "rules")); self.m_llm.assert_not_called()

    def test_small_migration_everyday(self):
        # 2026-11-13: migrations are tier 1 — no deterministic deep rule for migration paths / *.sql
        t, r, s = self.c(meta([("db/migrations/008_ebitda.sql", 10, 0), ("src/a.ts", 2, 1)]))
        self.assertEqual((t, s), ("everyday", "rules")); self.m_llm.assert_not_called()
        for pth in ("alembic/versions/6650_add.py", "migrations/001.py", "server/migrate/x.ts", "q.sql"):
            self.assertEqual(self.c(meta([(pth, 5, 1)]), use_cache=False)[::2], ("everyday", "rules"), pth)

    def test_mid_migration_goes_to_llm_which_can_pick_deep(self):
        self.m_llm.return_value = ("deep", "drops a column live code still reads")
        m = meta([("packages/db/migrations/0010-drop.ts", 60, 5), ("src/a.ts", 40, 10), ("src/b.ts", 30, 0),
                  ("src/c.ts", 5, 5)])
        t, r, s = self.c(m)
        self.assertEqual((t, s), ("deep", "llm")); self.m_llm.assert_called_once()

    def test_migration_label_not_deep(self):
        self.assertEqual(self.c(meta([("src/util.ts", 12, 4)], labels=["migration"]))[::2], ("everyday", "rules"))

    def test_schema_still_deep(self):
        self.assertEqual(self.c(meta([("prisma/schema.prisma", 3, 1)]))[::2], ("deep", "rules"))

    def test_classifier_prompt_migrations_not_deep_alone(self):
        pr = rv.build_classifier_prompt("o/r", meta([("db/migrations/1.sql", 1, 0)]), "", CFG["classifier"])
        self.assertIn("Database migrations are NOT a reason for deep on their own", pr)
        self.assertNotIn("data migrations or schema changes", pr)

    def test_cache_invalidated_when_rules_change(self):
        self.m_llm.return_value = ("deep", "x")
        m = meta([("src/a.ts", 90, 20), ("src/b.ts", 60, 10), ("src/c.ts", 40, 0), ("src/d.ts", 5, 5)])
        self.c(m); self.assertEqual(self.c(m)[2], "llm+cached")
        cfg2 = json.loads(json.dumps(CFG)); cfg2["rules"]["small_max_lines"] = 41
        self.assertEqual(rv.classify_pr("o/r", 1, cfg2, self.hp, meta=m, diff="")[2], "llm")
        self.assertEqual(self.m_llm.call_count, 2)

    def test_pre_fingerprint_cache_ignored(self):
        m = meta([("src/util.ts", 12, 4)])
        cp_ = rv.tier_cache_path("o/r", 1); cp_.parent.mkdir(parents=True, exist_ok=True)
        cp_.write_text(json.dumps({"head": "abc123", "tier": "deep", "reason": "x matches **/migrations/**",
                                   "source": "rules"}))
        self.assertEqual(self.c(m)[::2], ("everyday", "rules"))

    def test_workflow_deep(self):
        self.assertEqual(self.c(meta([(".github/workflows/ci.yml", 3, 1)]))[0], "deep")

    def test_root_glob(self):
        self.assertEqual(self.c(meta([("auth/session.py", 3, 1)]))[0], "deep")   # **/auth/** at repo root

    def test_small_code_everyday(self):
        t, r, s = self.c(meta([("src/util.ts", 12, 4)])); self.assertEqual((t, s), ("everyday", "rules"))

    def test_large_deep(self):
        t, r, s = self.c(meta([(f"src/m{i}.ts", 30, 5) for i in range(25)])); self.assertEqual((t, s), ("deep", "rules"))

    def test_label_deep(self):
        self.assertEqual(self.c(meta([("README.md", 1, 0)], labels=["security"]))[0], "deep")

    def test_major_bump_deep(self):
        diff = ('diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
                '-    "react": "^18.2.0",\n+    "react": "^19.0.0",\n')
        t, r, s = self.c(meta([("package.json", 1, 1)]), diff=diff)
        self.assertEqual((t, s), ("deep", "rules")); self.assertIn("react 18→19", r)

    def test_minor_bump_not_deep(self):
        diff = ('diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n'
                '-requests==2.31.0\n+requests==2.32.3\n')
        self.assertEqual(self.c(meta([("requirements.txt", 1, 1)]), diff=diff)[:3:2], ("everyday", "rules"))

    def test_mid_size_goes_to_llm(self):
        self.m_llm.return_value = ("everyday", "UI-only tweak")
        t, r, s = self.c(meta([("src/a.ts", 90, 20), ("src/b.tsx", 60, 10), ("src/c.test.ts", 40, 0), ("src/d.ts", 5, 5)]))
        self.assertEqual((t, s, r), ("everyday", "llm", "UI-only tweak")); self.m_llm.assert_called_once()

    def test_llm_failure_deep_fallback(self):
        self.m_llm.return_value = (None, "classifier timed out after 240s")
        t, r, s = self.c(meta([("src/a.ts", 90, 20), ("src/b.ts", 60, 10), ("src/c.ts", 40, 0), ("src/d.ts", 5, 5)]))
        self.assertEqual((t, s), ("deep", "fallback"))

    def test_override(self):
        self.assertEqual(self.c(meta([("README.md", 1, 0)]), override="deep")[::2], ("deep", "override"))

    def test_cache_hit(self):
        self.m_llm.return_value = ("deep", "touches retry logic")
        m = meta([("src/a.ts", 90, 20), ("src/b.ts", 60, 10), ("src/c.ts", 40, 0), ("src/d.ts", 5, 5)])
        self.c(m); t, r, s = self.c(m)
        self.assertEqual((t, s), ("deep", "llm+cached")); self.m_llm.assert_called_once()

    def test_fallback_not_cached(self):
        self.m_llm.return_value = (None, "x")
        m = meta([("src/a.ts", 90, 20), ("src/b.ts", 60, 10), ("src/c.ts", 40, 0), ("src/d.ts", 5, 5)])
        self.c(m); self.c(m); self.assertEqual(self.m_llm.call_count, 2)

    def test_dry_run_no_llm(self):
        t, r, s = self.c(meta([("src/a.ts", 90, 20), ("src/b.ts", 60, 10), ("src/c.ts", 40, 0), ("src/d.ts", 5, 5)]),
                         use_llm=False, use_cache=False)
        self.assertEqual(s, "llm-pending"); self.m_llm.assert_not_called()

    def test_runner_models(self):
        rv.set_active_tier(CFG, "deep")
        self.assertEqual(rv._model_args("claude"), ["--model", "claude-opus-5-5"])
        self.assertEqual(rv._tier_model("muse", rv.MUSE_REVIEW_MODEL), "muse-wonder-1.0")
        rv.set_active_tier(CFG, "everyday")
        self.assertEqual(rv._tier_model("muse", rv.MUSE_REVIEW_MODEL), "muse-spark-1.3")
        rv.set_active_tier(None, None)
        self.assertEqual(rv._model_args("cursor"), ["--model", "claude-opus-5-5-high"])  # fallback, not CLI default
        self.assertEqual(rv._tier_model("antigravity", rv.ANTIGRAVITY_MODEL), "gemini-4-hydrogen-high")

    def test_runner_argv(self):
        rv.set_active_tier(CFG, "everyday")
        with mock.patch.object(rv, "run") as r:
            r.return_value = subprocess.CompletedProcess([], 0, "x", "")
            rv.run_cursor(Path(self.tmp.name), "p"); a = r.call_args[0][0]
            self.assertEqual(a[:4], ["agent", "-p", "--model", "claude-sonnet-5-5-high"])
            rv.run_muse(Path(self.tmp.name), "p"); a = r.call_args[0][0]
            self.assertEqual(a[a.index("--model") + 1], "muse-spark-1.3")


class AgySafety(unittest.TestCase):
    PERM_ERR = (
        'jetski: no output produced — a tool required the "command" permission that '
        "headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under "
        "permissions.allow in settings.json (e.g. command(<target>)). Alternatively, "
        "re-run with --dangerously-skip-permissions to auto-approve all tools."
    )

    def test_allowlist_is_read_only(self):
        for c in ("git cat-file", "git merge-base", "git shortlog", "git rev-list",
                  "git ls-tree", "git merge-tree", "git for-each-ref", "git remote -v",
                  "git tag -l", "git config --get", "jq", "nl", "realpath", "cut", "tr", "du", "pwd"):
            self.assertIn(c, rv._AGY_READONLY_CMDS)
        for bad in ("python", "python3", "pytest", "node", "sh", "bash", "curl", "wget",
                    "git push", "git commit", "git tag", "git remote", "git config",
                    "rm", "mv", "cp", "awk", "sed", "xargs"):
            self.assertNotIn(bad, rv._AGY_READONLY_CMDS)
        self.assertNotIn("--dangerously-skip-permissions", rv.ANTIGRAVITY_REVIEW_SETTINGS)
        rev_cfg = json.loads(rv.ANTIGRAVITY_REVIEW_SETTINGS)
        self.assertIn("write_file(*)", rev_cfg["permissions"]["deny"])
        self.assertIn("edit_file(*)", rev_cfg["permissions"]["deny"])

    def test_pointer_tells_agy_not_to_run_code(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(rv, "run") as r:
            r.return_value = subprocess.CompletedProcess([], 0, "ok VERDICT: APPROVE", "")
            rv.run_antigravity(Path(tmp.name), "p")
            a = r.call_args[0][0]
            self.assertNotIn("--dangerously-skip-permissions", a)
            self.assertIn(rv.AGY_READONLY_RULE, a[a.index("-p") + 1])
            self.assertIn("Do NOT run python", a[a.index("-p") + 1])
            self.assertIn("$(...)", a[a.index("-p") + 1])

    def test_normalize_permission_denial_and_no_false_positive(self):
        # Exit 0 + empty stdout (standard agy auto-deny) -> exit 1 for failover
        cp = rv._agy_normalize(subprocess.CompletedProcess(["agy"], 0, "", self.PERM_ERR))
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(cp.stdout, "")
        # Banner accidentally written to stdout with exit 0 -> exit 1 + empty stdout
        cp2 = rv._agy_normalize(subprocess.CompletedProcess(["agy"], 0, self.PERM_ERR, ""))
        self.assertEqual(cp2.returncode, 1)
        self.assertEqual(cp2.stdout, "")
        self.assertIn("auto-denied", cp2.stderr)
        # Non-zero exit with partial stdout + permission-denial stderr -> cleared stdout
        cp3 = rv._agy_normalize(subprocess.CompletedProcess(["agy"], 1, "Checking PR...", self.PERM_ERR))
        self.assertEqual(cp3.returncode, 1)
        self.assertEqual(cp3.stdout, "")
        # Real review quoting the error message (e.g. reviewing pr-overseer) must NOT be rejected
        review = f"Discussed `{self.PERM_ERR}` in review.py.\n\nVERDICT: APPROVE\n"
        cp4 = rv._agy_normalize(subprocess.CompletedProcess(["agy"], 0, review, ""))
        self.assertEqual(cp4.returncode, 0)
        self.assertEqual(cp4.stdout, review)

    def test_harness_chain_fails_over_on_agy_permission_denial(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hp = Path(tmp.name) / "h.json"
        hp.write_text(json.dumps({"default_cooldown_minutes": 15, "harnesses": [
            {"id": "antigravity", "bin": "agy", "cooldown_until": None},
            {"id": "cursor", "bin": "agent", "cooldown_until": None},
        ]}))
        seen = []
        def fake_agy(wt, prompt):
            seen.append("antigravity")
            return rv._agy_normalize(subprocess.CompletedProcess(["agy"], 0, "", self.PERM_ERR))
        def fake_cursor(wt, prompt):
            seen.append("cursor")
            return subprocess.CompletedProcess(["agent"], 0, "ok\nVERDICT: APPROVE\n", "")
        with mock.patch.object(rv.shutil, "which", return_value="/x"):
            res = rv.run_harness_chain(Path(tmp.name), "p", hp,
                                       runners={"antigravity": fake_agy, "cursor": fake_cursor})
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "cursor")
        self.assertEqual(seen, ["antigravity", "cursor"])

    def test_fetch_pr_diff_local_git_fallback_when_gh_refuses_large_diff(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = Path(tmp.name)
        def fake_run(cmd, **kw):
            if cmd[:3] == ["gh", "pr", "diff"]:
                return subprocess.CompletedProcess(cmd, 1, "", "HTTP 406: diff exceeded max lines")
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                return subprocess.CompletedProcess(cmd, 0, "7fe4446\n", "")
            if cmd[:3] == ["git", "diff", "--stat"]:
                return subprocess.CompletedProcess(cmd, 0, " src/a.py | 10 ++++\n 1 file changed\n", "")
            if cmd[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(cmd, 0, "diff --git a/src/a.py b/src/a.py\n+x = 1\n", "")
            raise AssertionError(f"unexpected cmd: {cmd}")
        with mock.patch.object(rv, "run", side_effect=fake_run):
            out = rv.fetch_pr_diff("o/r", 11, wt, "main")
        self.assertIn("local git diff against origin/main", out)
        self.assertIn("=== DIFFSTAT ===", out)
        self.assertIn("diff --git a/src/a.py b/src/a.py", out)
        self.assertEqual(rv.changed_paths_from_diff(out), ["src/a.py"])


class HighReasoning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def test_claude_and_muse_pass_high_effort(self):
        rv.set_active_tier(None, None)
        with mock.patch.object(rv, "run") as r:
            r.return_value = subprocess.CompletedProcess([], 0, "x", "")
            for fn in (rv.run_claude, rv.run_claude_fix):
                fn(Path(self.tmp.name), "p"); a = r.call_args[0][0]
                self.assertEqual(a[a.index("--effort") + 1], "high")
            for fn in (rv.run_muse, rv.run_muse_fix):
                fn(Path(self.tmp.name), "p"); a = r.call_args[0][0]
                self.assertEqual(a[a.index("--reasoning-effort") + 1], "high")

    def test_classifier_muse_high_effort_and_default_model(self):
        hp = Path(self.tmp.name) / "h.json"
        hp.write_text(json.dumps({"harnesses": [{"id": "muse", "bin": "muse", "cooldown_until": None}]}))
        with mock.patch.object(rv, "run") as r, mock.patch.object(rv.shutil, "which", return_value="/x"):
            r.return_value = subprocess.CompletedProcess([], 0, "TIER: deep\nREASON: x\n", "")
            rv.run_classifier_llm("p", {"harness": "muse"}, hp); a = r.call_args[0][0]
            self.assertEqual(a[a.index("--reasoning-effort") + 1], "high")
            self.assertEqual(a[a.index("--model") + 1], "gemini-4-hydrogen-high")

    def test_fallback_models_are_high(self):
        for hid, want in (("cursor", "claude-opus-5-5-high"), ("claude", "claude-opus-5-5"),
                          ("antigravity", "gemini-4-hydrogen-high")):
            self.assertEqual(rv._model_label(hid), want)


class LLMParse(unittest.TestCase):
    def test_parse_and_failures(self):
        tmp = tempfile.TemporaryDirectory(); hp = Path(tmp.name) / "h.json"
        hp.write_text(json.dumps({"harnesses": [{"id": "antigravity", "bin": "agy", "cooldown_until": None}]}))
        cc = CFG["classifier"]
        with mock.patch.object(rv, "run") as r, mock.patch.object(rv.shutil, "which", return_value="/x"):
            r.return_value = subprocess.CompletedProcess([], 0, "TIER: deep\nREASON: touches auth\n", "")
            self.assertEqual(rv.run_classifier_llm("p", cc, hp), ("deep", "touches auth"))
            a = r.call_args[0][0]; self.assertIn("gemini-4-hydrogen-high", a); self.assertIn("plan", a)
            r.return_value = subprocess.CompletedProcess([], 0, "I think it's fine", "")
            self.assertIsNone(rv.run_classifier_llm("p", cc, hp)[0])
            r.return_value = subprocess.CompletedProcess([], 1, "", "boom")
            self.assertIsNone(rv.run_classifier_llm("p", cc, hp)[0])
            r.side_effect = subprocess.TimeoutExpired("agy", 1)
            self.assertIn("timed out", rv.run_classifier_llm("p", cc, hp)[1])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
