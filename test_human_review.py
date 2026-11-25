#!/usr/bin/env python3
"""needs-human-review ping: trigger logic, dedup, failure isolation — no network, gh mocked."""
import importlib.util, json, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rv", HERE / "review.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)
CFG = json.loads((HERE / "examples" / "tiers.example.json").read_text())
CFG["human_review"]["email_to"] = "you@example.com"
CFG["human_review"]["email_transport"] = "sendmail"   # Flag tests assert the sendmail pipe; class Smtp covers smtp
CFG_ALL = json.loads(json.dumps(CFG)); CFG_ALL["human_review"]["channels"] = ["email", "label", "comment"]  # opt-in channels
PR = {"number": 7, "title": "T", "url": "https://github.com/o/r/pull/7", "author": {"login": "me"}}
DEP = {"number": 8, "title": "bump", "author": {"login": "app/dependabot", "is_bot": True}}


def meta(files, labels=()):
    return {"title": "t", "body": "b", "headRefOid": "abc", "labels": [{"name": l} for l in labels],
            "files": [{"path": p, "additions": a, "deletions": d} for p, a, d in files],
            "additions": sum(a for _, a, _ in files), "deletions": sum(d for _, _, d in files),
            "changedFiles": len(files)}


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class Trigger(unittest.TestCase):
    def d(self, tier, m, diff="", pr=PR, cfg=CFG):
        return rv.human_review_decision(tier, m, diff, cfg, pr)

    def test_workflow_path_flags(self):
        f, rs, _ = self.d("deep", meta([(".github/workflows/deploy.yml", 5, 0)]))
        self.assertTrue(f); self.assertIn(".github/workflows", rs[0])

    def test_migrations_never_flag(self):
        # 2026-11-13: migrations are tier 1 — not a human-review trigger (path, *.sql, or label)
        for pth in ("db/migrations/1.sql", "alembic/versions/a.py", "x.sql", "pkg/migrate/2.ts"):
            self.assertFalse(self.d("deep", meta([(pth, 5, 0)]))[0], pth)
        self.assertFalse(self.d("deep", meta([("src/a.ts", 5, 0)], labels=["migration"]))[0])
        self.assertNotIn("migration", rv.HUMAN_REVIEW_DEFAULTS["labels"])
        # a very large migration PR still flags on size
        self.assertTrue(self.d("deep", meta([("db/migrations/big.sql", 1600, 0)]))[0])

    def test_workflow_auth_infra_payment_schema(self):
        for p in (".github/workflows/ci.yml", "src/auth/login.ts", "infra/main.tf", "Dockerfile",
                  "src/billing.ts", "app/models.py", "prisma/schema.prisma"):
            self.assertTrue(self.d("deep", meta([(p, 3, 1)]))[0], p)

    def test_risk_label(self):
        self.assertTrue(self.d("deep", meta([("src/a.ts", 3, 1)], labels=["Security"]))[0])
        # deep-review label alone is an escalation, not a hard-risk signal
        self.assertFalse(self.d("deep", meta([("src/a.ts", 3, 1)], labels=["deep-review"]))[0])

    def test_major_bump(self):
        diff = ('diff --git a/package.json b/package.json\n-    "react": "^18.2.0",\n+    "react": "^19.0.0",\n')
        f, rs, _ = self.d("deep", meta([("package.json", 1, 1)]), diff)
        self.assertTrue(f); self.assertIn("react 18→19", rs[0])

    def test_size_thresholds(self):
        self.assertTrue(self.d("deep", meta([(f"src/m{i}.ts", 30, 0) for i in range(50)]))[0])   # 50 files
        self.assertTrue(self.d("deep", meta([("src/big.ts", 1400, 100)]))[0])                  # 1500 lines
        self.assertFalse(self.d("deep", meta([("src/big.ts", 1400, 99)]))[0])                  # 1499

    def test_800_rule_alone_no_flag(self):
        f, _, why = self.d("deep", meta([(f"src/m{i}.ts", 30, 5) for i in range(30)]))   # 1050 lines, 30 files
        self.assertFalse(f); self.assertIn("no hard-risk", why)

    def test_classifier_only_deep_no_flag(self):
        self.assertFalse(self.d("deep", meta([("src/a.ts", 90, 20), ("src/b.ts", 60, 10)]))[0])

    def test_not_deep_no_flag(self):
        self.assertFalse(self.d("everyday", meta([(".github/workflows/ci.yml", 5, 0)]))[0])
        self.assertFalse(self.d("?", meta([(".github/workflows/ci.yml", 5, 0)]))[0])

    def test_dependabot_never(self):
        f, _, why = self.d("deep", meta([(".github/workflows/ci.yml", 3, 1)]), pr=DEP)
        self.assertFalse(f); self.assertIn("Dependabot", why)

    def test_disabled_and_defaults(self):
        off = json.loads(json.dumps(CFG)); off["human_review"]["enabled"] = False
        self.assertFalse(self.d("deep", meta([("infra/main.tf", 1, 0)]), cfg=off)[0])
        nob = json.loads(json.dumps(CFG)); del nob["human_review"]      # missing block → code defaults
        self.assertTrue(self.d("deep", meta([("infra/main.tf", 1, 0)]), cfg=nob)[0])
        self.assertEqual(rv.human_review_config(nob)["min_lines"], 1500)
        self.assertFalse(self.d("deep", meta([("infra/main.tf", 1, 0)]), cfg=None)[0])  # tiers off

    def test_rules_list_filters(self):
        c = json.loads(json.dumps(CFG)); c["human_review"]["rules"] = ["size"]
        self.assertFalse(self.d("deep", meta([("infra/main.tf", 1, 0)]), cfg=c)[0])


class Flag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(rv, "FALLBACK_REVIEWS", Path(self.tmp.name)); p.start(); self.addCleanup(p.stop)
        self.info = {"cfg": CFG_ALL, "tier": "deep", "reason": "ci.yml matches .github/workflows/**", "source": "rules",
                     "meta": meta([(".github/workflows/x.yml", 3, 0)])}

    def go(self, side):
        with mock.patch.object(rv, "run", side_effect=side) as r:
            out = rv.flag_for_human_review("o/r", 7, PR, "me", tier_info=self.info, diff="", verdict="APPROVE",
                                           reviewer="muse muse-wonder-1.0", head="abc")
        return out, [c.args[0] for c in r.call_args_list]

    def router(self, view='{"labels":[],"comments":[]}', fail=()):
        def f(cmd, **kw):
            key = cmd[0] if cmd[0] != "gh" else " ".join(cmd[1:3])
            if any(k in key for k in fail):
                return cp(1, "", f"{key} boom")
            if key == "pr view":
                return cp(0, view)
            return cp(0, "ok")
        return f

    def test_full_flag_once(self):
        out, calls = self.go(self.router())
        kinds = [" ".join(c[:3]) if c[0] == "gh" else c[0] for c in calls]
        self.assertEqual(kinds, ["gh pr view", "gh label create", "gh api -X", "gh pr comment", "/usr/sbin/sendmail"])
        self.assertIn("repos/o/r/issues/7/labels", calls[2])
        body = calls[3][calls[3].index("--body") + 1]
        self.assertTrue(body.startswith(rv.HUMAN_REVIEW_MARKER)); self.assertIn("@your-github-login", body)
        self.assertIn("APPROVE", body); self.assertIn("+3 -0 across 1 files", body)
        # must not look like a self-review / dependabot marker to the already-reviewed gate
        self.assertFalse(body.startswith(rv.SELF_REVIEW_MARKER)); self.assertIsNone(rv.SELF_VERDICT_RE.match(body))
        self.assertEqual((out["label"], out["comment"]), ("added", "posted"))
        self.assertTrue(rv.human_marker_path("o/r", 7).is_file())
        out2, calls2 = self.go(self.router())       # local marker → nothing at all
        self.assertIsNone(out2); self.assertEqual(calls2, [])

    def test_merge_candidate_comment_and_email_say_held(self):
        with mock.patch.object(rv, "run", side_effect=self.router()) as r:
            rv.flag_for_human_review("o/r", 7, PR, "me", tier_info=self.info, diff="", verdict="APPROVE",
                                     merge_candidate=True)
        calls = [c for c in r.call_args_list]
        body = next(c.args[0] for c in calls if c.args[0][:3] == ["gh", "pr", "comment"])
        self.assertIn("Auto-merge is held until @your-github-login releases it", body[body.index("--body") + 1])
        mail = next(c.kwargs["input_text"] for c in calls if c.args[0][0] == "/usr/sbin/sendmail")
        self.assertIn("Auto-merge is HELD", mail)
        self.assertIn("python3 review.py --release-human-review --repo o/r --pr 7", mail)

    def test_skip_when_label_present(self):
        out, calls = self.go(self.router(view='{"labels":[{"name":"needs-human-review"}],"comments":[]}'))
        self.assertEqual(len(calls), 1); self.assertIn("label", out["prior"])
        self.assertTrue(rv.human_marker_path("o/r", 7).is_file())

    def test_skip_when_prior_comment(self):
        v = json.dumps({"labels": [], "comments": [{"author": {"login": "ME"},
                        "body": rv.HUMAN_REVIEW_MARKER + "\n\nx", "createdAt": "z"}]})
        out, calls = self.go(self.router(view=v))
        self.assertEqual(len(calls), 1); self.assertIn("comment", out["prior"])

    def test_other_users_marker_does_not_count(self):
        v = json.dumps({"labels": [], "comments": [{"author": {"login": "bob"}, "body": rv.HUMAN_REVIEW_MARKER}]})
        out, _ = self.go(self.router(view=v)); self.assertEqual(out["comment"], "posted")

    def test_failures_never_raise(self):
        out, _ = self.go(self.router(fail=("api -X", "pr comment", "label create", "sendmail")))
        self.assertTrue(out["label"].startswith("failed")); self.assertTrue(out["comment"].startswith("failed"))
        self.assertTrue(out["email"].startswith("failed"))

    def test_prior_check_failure_labels_but_no_comment(self):
        out, calls = self.go(self.router(fail=("pr view",)))
        self.assertNotIn(["gh", "pr", "comment"], [c[:3] for c in calls])
        self.assertEqual(out["label"], "added")

    def test_exception_swallowed(self):
        with mock.patch.object(rv, "human_review_decision", side_effect=ValueError("x")):
            self.assertIsNone(rv.flag_for_human_review("o/r", 7, PR, "me", tier_info=self.info, diff=""))

    def test_no_flag_no_calls(self):
        self.info["meta"] = meta([("src/a.ts", 90, 20)])
        out, calls = self.go(self.router()); self.assertIsNone(out); self.assertEqual(calls, [])

    def test_email_noop_without_recipient(self):
        hr = rv.human_review_config({"human_review": {"email_to": ""}})
        with mock.patch.object(rv, "run") as r:
            self.assertIn("not set", rv.send_human_review_email(hr, "s", "b")); r.assert_not_called()


class Gate(unittest.TestCase):
    def test_existing_review_by_ignores_human_marker(self):
        v = json.dumps({"reviews": [], "comments": [{"author": {"login": "me"}, "body": rv.HUMAN_REVIEW_MARKER}]})
        with mock.patch.object(rv, "run", return_value=cp(0, v)):
            self.assertIsNone(rv.existing_review_by("o/r", 7, "me"))
        v2 = json.dumps({"reviews": [], "comments": [{"author": {"login": "me"},
                         "body": "**Overseer self-review — VERDICT: APPROVE**"}]})
        with mock.patch.object(rv, "run", return_value=cp(0, v2)):
            self.assertIn("self-review", rv.existing_review_by("o/r", 7, "me"))


class Hold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(rv, "FALLBACK_REVIEWS", Path(self.tmp.name)); p.start(); self.addCleanup(p.stop)

    def marker(self, **kw):
        m = rv.human_marker_path("o/r", 7); m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(json.dumps({"label": "added", **kw})); return m

    def h(self, labels=(), rc=0, rec=None, cfg=CFG, write=True):
        out = json.dumps({"labels": [{"name": l} for l in labels]})
        with mock.patch.object(rv, "run", return_value=cp(rc, out, "boom")) as r:
            res = rv.human_review_merge_hold("o/r", 7, cfg, rec, write=write)
        return res, r

    def test_just_flagged_holds_without_gh(self):
        (held, why), r = self.h(rec={"reasons": ["x.sql"]})
        self.assertTrue(held); self.assertIn("flagged this run", why); r.assert_not_called()

    def test_label_present_holds(self):
        self.assertTrue(self.h(labels=["Needs-Human-Review"])[0][0])

    def test_gh_failure_fails_closed(self):
        self.assertTrue(self.h(rc=1)[0][0])

    def test_not_flagged_merges(self):
        self.assertEqual(self.h()[0], (False, "not flagged"))

    def test_label_removed_clears_and_merges(self):
        m = self.marker()
        held, why = self.h()[0]
        self.assertFalse(held); self.assertIn("cleared", why)
        self.assertIn("cleared_at", json.loads(m.read_text()))
        self.assertFalse(self.h()[0][0])                       # stays cleared

    def test_dry_run_does_not_write_clear(self):
        m = self.marker(); self.assertFalse(self.h(write=False)[0][0])
        self.assertNotIn("cleared_at", json.loads(m.read_text()))

    def test_label_never_added_holds(self):
        self.marker(label="failed (no perms)")
        held, why = self.h()[0]; self.assertTrue(held); self.assertIn("--release-human-review", why)

    def test_hold_merge_off(self):
        c = json.loads(json.dumps(CFG)); c["human_review"]["hold_merge"] = False
        self.assertFalse(self.h(labels=["needs-human-review"], cfg=c)[0][0])

    def test_default_on_when_block_missing(self):
        c = json.loads(json.dumps(CFG)); del c["human_review"]
        self.assertTrue(self.h(labels=["needs-human-review"], cfg=c)[0][0])
        self.assertTrue(rv.HUMAN_REVIEW_DEFAULTS["hold_merge"])


class ReviewOneMerge(unittest.TestCase):
    """review_one end to end with gh/harness mocked: a flagged self_review APPROVE must not merge."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.hp = t / "h.json"
        self.hp.write_text(json.dumps({"default_cooldown_minutes": 15, "harnesses": [
            {"id": "muse", "bin": "muse", "cooldown_until": None}]}))
        for name, val in (("FALLBACK_REVIEWS", t / "reviews"), ("load_tiers", lambda *a, **k: CFG)):
            p = mock.patch.object(rv, name, val); p.start(); self.addCleanup(p.stop)
        self.pr = {"number": 7, "title": "T", "url": "u", "headRefName": "b", "isDraft": False,
                   "author": {"login": "me"}}

    def go(self, files, label_on_pr=False):
        m = meta(files)
        stubs = {
            "fetch_ci_status": lambda *a: {"statusCheckRollup": []},
            "summarize_ci": lambda *a: {"state": "green"},
            "ci_label": lambda *a: "green",
            "create_pr_worktree": lambda *a: Path(self.tmp.name),
            "remove_worktree": lambda *a: None, "ensure_stubs": lambda *a: [],
            "fetch_pr_diff": lambda *a: "", "read_context": lambda *a: "",
            "fetch_classify_meta": lambda *a: m,
            "write_review": lambda *a: Path(self.tmp.name) / "PR-7.md",
            "post_review_comment": lambda *a, **k: (True, "COMMENT (APPROVE)"),
            "_ensure_label": lambda *a: None,
            "send_human_review_email": lambda *a: "skipped (test)",
        }
        view = json.dumps({"labels": [{"name": "needs-human-review"}] if label_on_pr else [], "comments": []})

        def fake_run(cmd, **kw):
            if cmd[:3] == ["gh", "pr", "view"]:
                return cp(0, view)
            if cmd[:2] == ["git", "rev-parse"]:
                return cp(0, "abc\n")
            return cp(0, "ok")
        runner = mock.Mock(return_value=cp(0, "fine\n\nVERDICT: APPROVE\n", ""))
        with mock.patch.multiple(rv, **stubs), mock.patch.object(rv, "run", side_effect=fake_run), \
                mock.patch.object(rv, "admin_merge", return_value=(True, "MERGED")) as merge, \
                mock.patch.dict(rv.RUNNERS, {"muse": runner}), \
                mock.patch.object(rv.shutil, "which", return_value="/x"), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            rv.review_one("o/r", Path(self.tmp.name), self.pr, {}, self.hp, mode="self_review", login="me",
                          pre=(None, {}, {}))
        return merge, out.getvalue()

    def test_flagged_approve_is_held(self):
        merge, out = self.go([(".github/workflows/ci.yml", 5, 0)])
        merge.assert_not_called(); self.assertIn("merge held: needs-human-review", out)

    def test_unflagged_approve_merges(self):
        merge, out = self.go([("src/a.ts", 5, 0)])
        merge.assert_called_once(); self.assertNotIn("merge held", out)

    def test_label_already_on_pr_holds_even_if_not_risky(self):
        merge, out = self.go([("src/a.ts", 5, 0)], label_on_pr=True)
        merge.assert_not_called(); self.assertIn("merge held", out)

    def test_cleared_marker_merges_on_force_rereview(self):
        mk = rv.human_marker_path("o/r", 7); mk.parent.mkdir(parents=True, exist_ok=True)
        mk.write_text(json.dumps({"label": "added"}))
        merge, out = self.go([(".github/workflows/ci.yml", 5, 0)])
        merge.assert_called_once(); self.assertIn("hold released", out)


class Comment(unittest.TestCase):
    def test_hold_sentence(self):
        b = rv.human_review_comment("@me", ["x"], {}, "r", "APPROVE", None, None,
                                    "Auto-merge is held until @me removes the `needs-human-review` label")
        self.assertIn("Auto-merge is held", b)
        self.assertNotIn("Auto-merge", rv.human_review_comment("@me", ["x"], {}, "r", "APPROVE", None, None))


class Smtp(unittest.TestCase):
    PW = "abcd efgh ijkl mnop"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.pwf = Path(self.tmp.name) / "pw"
        self.pwf.write_text(self.PW + "\n"); self.pwf.chmod(0o600)
        self.hr = rv.human_review_config({"human_review": {
            "email_to": "you@example.com", "email_transport": "smtp", "smtp_user": "bot@example.com",
            "password_file": str(self.pwf)}})

    def send(self, **smtp_kw):
        with mock.patch("smtplib.SMTP") as S:
            srv = S.return_value.__enter__.return_value
            for k, v in smtp_kw.items():
                getattr(srv, k).side_effect = v
            out = rv.send_human_review_email(self.hr, "subj", "body")
        return out, S, srv

    def test_sends_with_starttls_and_stripped_password(self):
        out, S, srv = self.send()
        self.assertTrue(out.startswith("sent via smtp smtp.gmail.com:587"), out)
        S.assert_called_once_with("smtp.gmail.com", 587, timeout=60)
        srv.starttls.assert_called_once(); srv.login.assert_called_once_with("bot@example.com", "abcdefghijklmnop")
        msg = srv.send_message.call_args[0][0]
        self.assertEqual((msg["To"], msg["From"], msg["Subject"]), ("you@example.com", "bot@example.com", "subj"))
        self.assertNotIn("abcd", out)

    def test_refuses_open_mode(self):
        for mode in (0o644, 0o640, 0o604):
            self.pwf.chmod(mode)
            out, S, _ = self.send()
            self.assertIn("refusing", out); self.assertIn(oct(mode), out); S.assert_not_called()
        self.pwf.chmod(0o400); self.assertTrue(self.send()[0].startswith("sent"))   # tighter than 600 is fine

    def test_missing_and_empty_file(self):
        self.pwf.write_text("  \n"); self.assertIn("empty", self.send()[0])
        self.pwf.unlink(); out, S, _ = self.send()
        self.assertIn("not found", out); self.assertTrue(out.startswith("skipped")); S.assert_not_called()

    def test_auth_failure_never_leaks_password(self):
        import smtplib
        out, _, _ = self.send(login=smtplib.SMTPAuthenticationError(535, b"bad creds abcdefghijklmnop"))
        self.assertTrue(out.startswith("failed (smtp auth rejected")); self.assertNotIn("abcdefghijklmnop", out)
        out, _, _ = self.send(send_message=smtplib.SMTPException("oops abcdefghijklmnop"))
        self.assertTrue(out.startswith("failed")); self.assertNotIn("abcdefghijklmnop", out)
        out, _, _ = self.send(starttls=OSError("conn reset"))
        self.assertIn("OSError", out)

    def test_sendmail_transport_unchanged(self):
        hr = dict(self.hr, email_transport="sendmail")
        with mock.patch("smtplib.SMTP") as S, mock.patch.object(rv, "run", return_value=cp(0)) as r:
            out = rv.send_human_review_email(hr, "s", "b")
        S.assert_not_called(); self.assertEqual(r.call_args[0][0][0], "/usr/sbin/sendmail"); self.assertIn("handed", out)

    def test_cli_missing_password_prints_setup(self):
        self.pwf.unlink()
        cfg = {"human_review": {"email_to": "you@example.com", "email_transport": "smtp",
                                "password_file": str(self.pwf)}}
        with mock.patch.object(rv, "load_tiers", return_value=cfg), mock.patch("smtplib.SMTP") as S, \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            rc = rv.test_email_cli()
        self.assertEqual(rc, 1); S.assert_not_called()
        self.assertIn("FAILED", out.getvalue()); self.assertIn("apppasswords", out.getvalue())
        self.assertIn(str(self.pwf), out.getvalue())

    def test_cli_success_and_no_password_in_output(self):
        cfg = {"human_review": {"email_to": "you@example.com", "email_transport": "smtp",
                                "password_file": str(self.pwf)}}
        with mock.patch.object(rv, "load_tiers", return_value=cfg), mock.patch("smtplib.SMTP") as S, \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            rc = rv.test_email_cli()
        self.assertEqual(rc, 0); self.assertIn("OK: sent via smtp", out.getvalue())
        self.assertNotIn("abcd", out.getvalue()); S.return_value.__enter__.return_value.send_message.assert_called_once()


class EmailOnly(unittest.TestCase):
    """Default channels ["email"]: no label, no comment; email says held + release command."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(rv, "FALLBACK_REVIEWS", Path(self.tmp.name)); p.start(); self.addCleanup(p.stop)
        self.info = {"cfg": CFG, "tier": "deep", "reason": "r", "source": "rules", "meta": meta([(".github/workflows/x.yml", 3, 0)])}

    def go(self, view='{"labels":[],"comments":[]}', fail_view=False, merge_candidate=True):
        def f(cmd, **kw):
            if cmd[:3] == ["gh", "pr", "view"]:
                return cp(1, "", "boom") if fail_view else cp(0, view)
            return cp(0, "ok")
        with mock.patch.object(rv, "run", side_effect=f) as r:
            out = rv.flag_for_human_review("o/r", 7, PR, "me", tier_info=self.info, diff="", verdict="APPROVE",
                                           merge_candidate=merge_candidate)
        return out, r.call_args_list

    def test_default_channels_email(self):
        self.assertEqual(rv.human_review_channels(rv.human_review_config(CFG)), {"email"})
        self.assertEqual(rv.human_review_channels(rv.human_review_config({})), {"email"})

    def test_no_label_no_comment_only_email(self):
        out, calls = self.go()
        cmds = [c.args[0] for c in calls]
        self.assertFalse([c for c in cmds if c[:3] in (["gh", "pr", "edit"], ["gh", "pr", "comment"])
                          or c[:2] in (["gh", "label"], ["gh", "api"])])
        self.assertEqual((out["label"], out["comment"]), ("off (channels)", "off (channels)"))
        mail = next(c.kwargs["input_text"] for c in calls if c.args[0][0] == "/usr/sbin/sendmail")
        self.assertIn("Auto-merge is HELD", mail); self.assertIn("--release-human-review --repo o/r --pr 7", mail)
        self.assertIn("--repo o/r --pr 7 --force", mail); self.assertIn("Subject: [overseer] Merge held", mail)
        self.assertTrue(out["merge_held"]); self.assertTrue(rv.human_marker_path("o/r", 7).is_file())

    def test_review_mode_email_no_hold_text(self):
        out, calls = self.go(merge_candidate=False)
        mail = next(c.kwargs["input_text"] for c in calls if c.args[0][0] == "/usr/sbin/sendmail")
        self.assertNotIn("HELD", mail); self.assertFalse(out["merge_held"])

    def test_existing_label_or_marker_counts_as_flagged(self):
        out, calls = self.go(view='{"labels":[{"name":"needs-human-review"}],"comments":[]}')
        self.assertEqual(out["email"], "skipped (already flagged)")
        self.assertFalse([c for c in calls if c.args[0][0] == "/usr/sbin/sendmail"])

    def test_prior_check_failure_still_emails(self):
        out, calls = self.go(fail_view=True)
        self.assertTrue([c for c in calls if c.args[0][0] == "/usr/sbin/sendmail"])
        self.assertEqual(out["comment"], "off (channels)")

    def test_record_holds_without_gh_and_release_cli(self):
        self.go()
        with mock.patch.object(rv, "run", side_effect=AssertionError("no gh needed")):
            held, why = rv.human_review_merge_hold("o/r", 7, CFG)
        self.assertTrue(held); self.assertIn("--release-human-review", why)
        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as o, \
                mock.patch.object(rv, "run", side_effect=AssertionError("release must not touch GitHub")):
            self.assertEqual(rv.release_human_review("o/r", 7), 0)
            held, why = rv.human_review_merge_hold("o/r", 7, CFG)
        self.assertFalse(held); self.assertIn("--release-human-review", why)
        self.assertIn("--repo o/r --pr 7 --force", o.getvalue())
        rec = json.loads(rv.human_marker_path("o/r", 7).read_text())
        self.assertEqual(rec["cleared_by"], "--release-human-review")

    def test_release_overrides_legacy_label_still_on_pr(self):
        mk = rv.human_marker_path("o/r", 7); mk.parent.mkdir(parents=True, exist_ok=True)
        mk.write_text(json.dumps({"label": "added"}))
        lab = cp(0, '{"labels":[{"name":"needs-human-review"}]}')
        with mock.patch.object(rv, "run", return_value=lab):
            self.assertTrue(rv.human_review_merge_hold("o/r", 7, CFG)[0])
        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
            rv.release_human_review("o/r", 7)
        with mock.patch.object(rv, "run", return_value=lab):
            self.assertFalse(rv.human_review_merge_hold("o/r", 7, CFG)[0])

    def test_release_without_record_creates_one(self):
        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
            self.assertEqual(rv.release_human_review("o/r", 9), 0)
        rec = json.loads(rv.human_marker_path("o/r", 9).read_text()); self.assertIn("cleared_at", rec)
        # a later flag on this PR is deduped by the record (no new email)
        self.assertTrue(rv.human_marker_path("o/r", 9).is_file())


class WithheldNonOwnReview(unittest.TestCase):
    """Non-own PRs flagged for human review: never post a GitHub review; email full review; skip on later cron ticks."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.hp = t / "h.json"
        self.hp.write_text(json.dumps({"default_cooldown_minutes": 15, "harnesses": [
            {"id": "muse", "bin": "muse", "cooldown_until": None}]}))
        for name, val in (("FALLBACK_REVIEWS", t / "reviews"), ("load_tiers", lambda *a, **k: CFG)):
            p = mock.patch.object(rv, name, val); p.start(); self.addCleanup(p.stop)
        self.other_pr = {"number": 18, "title": "Workflow change", "url": "https://github.com/o/r/pull/18",
                         "headRefName": "feat/wf", "isDraft": False, "author": {"login": "alice"}}
        self.info_all = {"cfg": CFG_ALL, "tier": "deep", "reason": "wf", "source": "rules",
                         "meta": meta([(".github/workflows/x.yml", 3, 0)])}

    def test_flag_withheld_emails_full_review_and_skips_github_channels(self):
        full_body = "Summary of findings.\n\n### Important\n- `x.yml:10`: check permissions.\n\nVERDICT: APPROVE\n"
        with mock.patch.object(rv, "run", return_value=cp(0, "ok")) as r:
            rec = rv.flag_for_human_review("o/r", 18, self.other_pr, "me", tier_info=self.info_all, diff="",
                                           verdict="APPROVE", reviewer="muse muse-wonder-1.0", head="abc123",
                                           review_body=full_body, review_withheld=True, review_path="/tmp/PR-18.md")
        cmds = [c.args[0] for c in r.call_args_list]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][0], "/usr/sbin/sendmail")
        mail = r.call_args_list[0].kwargs["input_text"]
        self.assertIn("Subject: [overseer] Review not posted, needs human review: o/r#18 Workflow change", mail)
        self.assertIn("Author: alice", mail)
        self.assertIn("No review was posted to GitHub", mail)
        self.assertIn("Local review file: /tmp/PR-18.md", mail)
        self.assertIn("--- Full review ---", mail)
        self.assertIn("`x.yml:10`: check permissions.", mail)
        self.assertTrue(rec["review_withheld"])
        self.assertEqual(rec["label"], "skipped (review withheld)")
        self.assertEqual(rec["comment"], "skipped (review withheld)")
        saved = json.loads(rv.human_marker_path("o/r", 18).read_text())
        self.assertTrue(saved["review_withheld"])
        self.assertEqual(saved["review_path"], "/tmp/PR-18.md")

        # Re-running with review_withheld=True (e.g. --force / URL run) sends the fresh full review email.
        full_body_2 = "Second pass.\n\nVERDICT: REQUEST_CHANGES\n"
        with mock.patch.object(rv, "run", return_value=cp(0, "ok")) as r2:
            rec2 = rv.flag_for_human_review("o/r", 18, self.other_pr, "me", tier_info=self.info_all, diff="",
                                            verdict="REQUEST_CHANGES", reviewer="muse muse-wonder-1.0", head="def456",
                                            review_body=full_body_2, review_withheld=True, review_path="/tmp/PR-18.md")
        self.assertEqual(len(r2.call_args_list), 1)
        self.assertIn("Second pass.", r2.call_args_list[0].kwargs["input_text"])
        self.assertEqual(rec2["verdict"], "REQUEST_CHANGES")

    def test_flag_non_withheld_also_includes_full_review_when_given(self):
        full_body = "Own PR review details.\n\nVERDICT: APPROVE\n"
        info = {"cfg": CFG, "tier": "deep", "reason": "wf", "source": "rules",
                "meta": meta([(".github/workflows/x.yml", 3, 0)])}
        def fake(cmd, **kw):
            return cp(0, '{"labels":[],"comments":[]}') if cmd[:3] == ["gh", "pr", "view"] else cp(0, "ok")
        with mock.patch.object(rv, "run", side_effect=fake) as r:
            rv.flag_for_human_review("o/r", 7, PR, "me", tier_info=info, diff="", verdict="APPROVE",
                                     merge_candidate=True, review_body=full_body, review_withheld=False)
        mail = next(c.kwargs["input_text"] for c in r.call_args_list if c.args[0][0] == "/usr/sbin/sendmail")
        self.assertIn("--- Full review ---", mail)
        self.assertIn("Own PR review details.", mail)

    def test_withheld_human_review_by_gate(self):
        self.assertIsNone(rv.withheld_human_review_by("o/r", 18))
        mp = rv.human_marker_path("o/r", 18); mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"review_withheld": False, "verdict": "APPROVE", "at": "2026-11-25T10:00:00Z"}))
        self.assertIsNone(rv.withheld_human_review_by("o/r", 18))
        mp.write_text(json.dumps({"review_withheld": True, "verdict": "APPROVE", "at": "2026-11-25T10:00:00Z"}))
        why = rv.withheld_human_review_by("o/r", 18)
        self.assertIsNotNone(why); self.assertIn("review withheld for human review", why)
        mp.write_text(json.dumps({"review_withheld": True, "verdict": "APPROVE", "at": "2026-11-25T10:00:00Z",
                                  "cleared_at": "2026-11-25T11:00:00Z"}))
        self.assertIsNone(rv.withheld_human_review_by("o/r", 18))

    def _run_review_one(self, pr, files, review_text, mode="review"):
        m = meta(files)
        emails = []
        stubs = {
            "fetch_ci_status": lambda *a: {"statusCheckRollup": []},
            "summarize_ci": lambda *a: {"state": "green"},
            "ci_label": lambda *a: "green",
            "create_pr_worktree": lambda *a: Path(self.tmp.name),
            "remove_worktree": lambda *a: None, "ensure_stubs": lambda *a: [],
            "fetch_pr_diff": lambda *a: "", "read_context": lambda *a: "",
            "fetch_classify_meta": lambda *a: m,
            "write_review": lambda *a: Path(self.tmp.name) / f"PR-{pr['number']}.md",
            "_ensure_label": lambda *a: None,
            "send_human_review_email": lambda hr, subj, body: (emails.append((subj, body)), "sent (test)")[1],
        }
        def fake_run(cmd, **kw):
            if cmd[:3] == ["gh", "pr", "view"]:
                return cp(0, '{"labels":[],"comments":[]}')
            if cmd[:2] == ["git", "rev-parse"]:
                return cp(0, "abc\n")
            return cp(0, "ok")
        runner = mock.Mock(return_value=cp(0, review_text, ""))
        with mock.patch.multiple(rv, **stubs), mock.patch.object(rv, "run", side_effect=fake_run), \
                mock.patch.object(rv, "submit_github_review", return_value=(True, "APPROVE")) as submit, \
                mock.patch.object(rv, "post_review_comment", return_value=(True, "COMMENT")) as comment, \
                mock.patch.object(rv, "admin_merge", return_value=(True, "MERGED")) as merge, \
                mock.patch.object(rv, "set_pr_state", return_value=[]) as set_state, \
                mock.patch.dict(rv.RUNNERS, {"muse": runner}), \
                mock.patch.object(rv.shutil, "which", return_value="/x"), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            rv.review_one("o/r", Path(self.tmp.name), pr, {}, self.hp, mode=mode, login="me", pre=(None, {}, {}))
        return submit, comment, merge, set_state, emails, out.getvalue()

    def test_review_one_withholds_both_verdicts_for_other_author_and_emails_full_review(self):
        for mode in ("review", "self_review"):
            for verdict, body in (
                ("APPROVE", "Looks clean overall.\n\n### Important\n- Check workflow perms.\n\nVERDICT: APPROVE\n"),
                ("REQUEST_CHANGES", "### Blocking\n- Secret leak in workflow.\n\nVERDICT: REQUEST_CHANGES\n"),
            ):
                with self.subTest(mode=mode, verdict=verdict):
                    mp = rv.human_marker_path("o/r", 18)
                    if mp.exists():
                        mp.unlink()
                    submit, comment, merge, set_state, emails, out = self._run_review_one(
                        self.other_pr, [(".github/workflows/ci.yml", 5, 0)], body, mode=mode)
                    submit.assert_not_called()
                    comment.assert_not_called()
                    merge.assert_not_called()
                    self.assertEqual(len(emails), 1)
                    subj, ebody = emails[0]
                    self.assertIn("Review not posted, needs human review", subj)
                    self.assertIn("No review was posted to GitHub", ebody)
                    self.assertIn(body.strip(), ebody)
                    self.assertIn(verdict, ebody)
                    self.assertTrue(json.loads(mp.read_text())["review_withheld"])
                    states = [c.args[2] for c in set_state.call_args_list]
                    self.assertEqual(states, ["in_progress", "needs_human"])
                    self.assertIn(f"not posting {verdict} review on GitHub", out)

    def test_review_one_still_posts_non_human_needed_review_for_other_author(self):
        body = "Minor nit only.\n\nVERDICT: APPROVE\n"
        submit, comment, merge, set_state, emails, _ = self._run_review_one(
            self.other_pr, [("src/a.ts", 5, 0)], body, mode="review")
        submit.assert_called_once()
        comment.assert_not_called()
        merge.assert_not_called()
        self.assertEqual(emails, [])

    def test_main_skips_withheld_pr_on_next_tick_without_force(self):
        import io, sys
        t = Path(self.tmp.name)
        clone = t / "clone"; clone.mkdir()
        repos = t / "repos.json"
        repos.write_text(json.dumps({"github_login": "me", "repos": [
            {"repo": "o/r", "path": str(clone), "review_mode": "review"}]}))
        mp = rv.human_marker_path("o/r", 18); mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"repo": "o/r", "number": 18, "review_withheld": True,
                                  "verdict": "APPROVE", "at": "2026-11-25T10:00:00Z"}))
        def fake_run(cmd, **kw):
            if cmd[:3] == ["gh", "api", "user"]:
                return cp(0, "me\n")
            if cmd[:3] == ["gh", "pr", "view"]:
                return cp(0, '{"reviews":[],"comments":[]}')
            return cp(0, "")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["review.py", "--repos-config", str(repos), "--harnesses-config", str(self.hp)]), \
                mock.patch("fcntl.flock"), \
                mock.patch.object(rv, "run", side_effect=fake_run), \
                mock.patch.object(rv, "list_review_requested_prs", return_value=[self.other_pr]), \
                mock.patch.object(rv, "review_one") as ro, \
                mock.patch("sys.stdout", out):
            rc = rv.main()
        self.assertEqual(rc, 0)
        ro.assert_not_called()
        self.assertIn("skip PR #18 by alice", out.getvalue())
        self.assertIn("review withheld for human review", out.getvalue())


if __name__ == "__main__":
    unittest.main()
