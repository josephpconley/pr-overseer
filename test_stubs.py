#!/usr/bin/env python3
"""Stub placement + context reading tests (no network, no harness calls)."""
import importlib.util, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rv", HERE / "review.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
        p = mock.patch.object(rv, "STUBS", HERE / "stubs"); p.start(); self.addCleanup(p.stop)

    def tearDown(self): self.tmp.cleanup()

    def names(self, created): return sorted(Path(c).name for c in created)

    def test_empty_repo_gets_full_stub_set(self):
        c = rv.ensure_stubs(self.d)
        self.assertEqual(self.names(c), ["AGENTS.md", "CLAUDE.md", "GEMINI.md", "REVIEW.md"])
        self.assertEqual((self.d / "GEMINI.md").read_bytes(), (HERE / "stubs/GEMINI.md").read_bytes())
        self.assertTrue((self.d / "GEMINI.md").read_text().startswith("@AGENTS.md"))

    def test_claude_only_repo_keeps_agents_absent(self):
        (self.d / "CLAUDE.md").write_text("# Real guide\nrun make test\n")
        c = rv.ensure_stubs(self.d)
        self.assertEqual(self.names(c), ["GEMINI.md", "REVIEW.md"])
        self.assertFalse((self.d / "AGENTS.md").exists())  # Muse falls back to the real CLAUDE.md
        self.assertTrue((self.d / "GEMINI.md").read_text().startswith("@CLAUDE.md"))
        self.assertEqual((self.d / "CLAUDE.md").read_text(), "# Real guide\nrun make test\n")

    def test_agents_present_only_pointers_added(self):
        (self.d / "AGENTS.md").write_text("# Real\n")
        c = rv.ensure_stubs(self.d)
        self.assertEqual(self.names(c), ["CLAUDE.md", "GEMINI.md", "REVIEW.md"])
        self.assertEqual((self.d / "AGENTS.md").read_text(), "# Real\n")

    def test_nothing_overwritten(self):
        for n in rv.STUB_NAMES: (self.d / n).write_text("mine " + n)
        self.assertEqual(rv.ensure_stubs(self.d), [])

    def test_fix_commit_excludes_created_stubs(self):
        git(self.d, "init", "-q"); git(self.d, "config", "user.email", "t@t"); git(self.d, "config", "user.name", "t")
        (self.d / "app.py").write_text("x = 1\n"); git(self.d, "add", "app.py"); git(self.d, "commit", "-qm", "init")
        created = rv.ensure_stubs(self.d)
        (self.d / "app.py").write_text("x = 2\n"); (self.d / "sub").mkdir(); (self.d / "sub/AGENTS.md").write_text("n\n")
        git(self.d, "add", "-A", "--", ".", ":(exclude).reviews", *rv.stub_exclude_pathspecs(created))
        staged = sorted(git(self.d, "diff", "--cached", "--name-only").split())
        self.assertEqual(staged, ["app.py", "sub/AGENTS.md"])  # root stubs excluded, real nested edit kept

    def test_remove_created_stubs_deletes_only_created(self):
        (self.d / "CLAUDE.md").write_text("# Real guide\nrun make test\n")
        created = rv.ensure_stubs(self.d)
        self.assertTrue((self.d / "REVIEW.md").exists())
        self.assertTrue((self.d / "GEMINI.md").exists())
        self.assertTrue((self.d / "CLAUDE.md").exists())
        removed = set(rv.remove_created_stubs(created))
        self.assertEqual(removed, {"GEMINI.md", "REVIEW.md"})
        self.assertFalse((self.d / "REVIEW.md").exists())
        self.assertFalse((self.d / "GEMINI.md").exists())
        self.assertEqual((self.d / "CLAUDE.md").read_text(), "# Real guide\nrun make test\n")
        self.assertEqual(rv.remove_created_stubs(created), [])  # already gone

    def test_read_context_root_and_nested(self):
        (self.d / "AGENTS.md").write_text("ROOT RULES\n")
        os.symlink("AGENTS.md", self.d / "CLAUDE.md")  # symlink to AGENTS.md: deduped
        (self.d / "GEMINI.md").write_text("@AGENTS.md\nGEMINI EXTRA\n")
        pk = self.d / "packages/telemetry/src"; pk.mkdir(parents=True)
        (self.d / "packages/telemetry/AGENTS.md").write_text("TELEMETRY RULES\n")
        (self.d / "packages/telemetry/CLAUDE.md").write_text("@AGENTS.md\n")  # pure pointer: skipped
        (self.d / "apps/web").mkdir(parents=True); (self.d / "apps/web/CLAUDE.md").write_text("WEB CLAUDE\n")
        diff = ("diff --git a/packages/telemetry/src/x.ts b/packages/telemetry/src/x.ts\n"
                "diff --git a/apps/web/page.tsx b/apps/web/page.tsx\n"
                "diff --git a/packages/telemetry/src/y.ts b/packages/telemetry/src/y.ts\n")
        ctx = rv.read_context(self.d, rv.changed_paths_from_diff(diff))
        self.assertEqual(ctx.count("ROOT RULES"), 1)
        self.assertIn("=== GEMINI.md ===", ctx)
        self.assertEqual(ctx.count("TELEMETRY RULES"), 1)
        self.assertIn("packages/telemetry/AGENTS.md (nested", ctx)
        self.assertNotIn("packages/telemetry/CLAUDE.md", ctx)
        self.assertIn("WEB CLAUDE", ctx)

    def test_nested_cap_and_escape(self):
        (self.d / "a").mkdir(); (self.d / "a/AGENTS.md").write_text("Z" * 50_000)
        out = Path(self.tmp.name + "_outside"); out.mkdir(exist_ok=True); (out / "AGENTS.md").write_text("SECRET\n")
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        (self.d / "b").mkdir(); os.symlink(out / "AGENTS.md", self.d / "b/AGENTS.md")
        ctx = rv.read_context(self.d, ["a/f.py", "b/g.py", "../etc/passwd"])
        self.assertLess(len(ctx), rv.NESTED_CONTEXT_MAX_CHARS + 500)
        self.assertIn("truncated", ctx); self.assertNotIn("SECRET", ctx)

    def test_no_changed_files_root_only(self):
        (self.d / "REVIEW.md").write_text("R\n")
        self.assertEqual(rv.read_context(self.d), "=== REVIEW.md ===\nR\n\n")


if __name__ == "__main__":
    unittest.main()
