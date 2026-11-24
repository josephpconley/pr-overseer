"""Weekly test-suite review / refactor (`pr-overseer-review --test-refactor`). Added Nov 15, 2026 (Joe).

Per opted-in repo (repos.json entry "test_refactor": true | {...overrides}):

 1. Deterministic signals, BEFORE any model call
    - GitHub Actions runtimes on the default branch (last `ci_days`): per-workflow run time, per-job
      and per-step durations (jobs API started_at/completed_at), test-step trends, plus per-test-file /
      per-slow-test timings parsed from the job logs (vitest / jest / pytest --durations) and from
      JUnit XML artifacts when a workflow uploads them.
    - Local run in a throwaway worktree at origin/<default>: per-test durations (vitest/jest JSON
      reporter, pytest JUnit XML, `node test/x.js` scripts timed one by one), failures re-run once
      (pass on re-run = flaky), skipped / todo / xfail, and coverage where it is cheap: V8
      (NODE_V8_COVERAGE) unique coverage per candidate file (full run minus run-without-that-file),
      pytest-cov contexts for Python.
    - Static scan of each test file (real sleeps, fresh-process spawns, mocks vs assertions,
      skip markers, "retired/obsolete" wording).
    - Impact score: 0.6 * cost (max of CI median and local time, normalized) + 0.4 * low impact
      (no unique coverage, all skipped, retired, no assertions, mock-heavy) + 0.25 if flaky.
      Top `top_n` locally-runnable files become candidates with a suggested REBUILD / DELETE.
 2. Harness step (deep tier models, normal Muse -> Antigravity -> Cursor -> Claude failover with the
    write-enabled fix runners): per candidate REBUILD (faster) / DELETE (low value) / KEEP, with a
    DECISION line + justification. Test files, fixtures and test config only.
 3. Guardrails (all must hold, else the branch is discarded): suite green before and after; only
    test paths changed; <= max_changed_lines; coverage not below before - coverage_tolerance_pct and
    deleted files carried no meaningful unique coverage; any per-file test-count drop must be a
    justified DELETE; local runtime not worse (+5%).
 4. Live only: push overseer/test-refactor-YYYYMMDD, open ONE PR (label test-refactor, created via
    the REST labels API if missing), write reviews/<owner>__<repo>/TESTREFACTOR-PR-N.json. The
    self_review loop reviews it like any PR but NEVER auto-merges it (review.test_refactor_merge_hold)
    until `python3 review.py --release-human-review --repo X --pr N`. Repo skipped while a prior
    test-refactor PR is open, or one was opened in the last 6 days.
 5. One digest email (tiers.json human_review smtp settings) per live run.

Dry-run (default): signals + ranked candidates + suggested actions; no harness, no push, no PR, no
email. --no-push: also runs the harness + guardrails + a local commit in the throwaway worktree and
pushes nothing. --live: everything.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

rv: Any = None   # the running review.py module (bound by run_test_refactor / bind)


def bind(module: Any) -> None:
    global rv
    rv = module


LABEL = "test-refactor"
LABEL_COLOR = "0e8a16"
LABEL_DESC = "Overseer: weekly test-suite refactor (never auto-merged)"
BRANCH_PREFIX = "overseer/test-refactor-"
MARKER = "**Overseer test-refactor**"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "top_n": 5,
    "max_changed_lines": 300,
    "coverage_tolerance_pct": 0.5,
    "runtime_regression_pct": 5.0,
    "ci_days": 21,
    "ci_max_runs": 60,          # runs listed per repo (default branch, completed)
    "ci_jobs_sample": 8,        # most recent runs per workflow whose jobs/steps are fetched
    "ci_log_runs": 2,           # most recent green runs per test workflow whose logs are parsed
    "local_timeout": 2400,      # seconds per suite run
    "install": True,            # install JS deps in the worktree (pnpm/yarn/npm, frozen lockfile)
    "allow_ci_config": False,   # allow .github/workflows edits (deep-risk path; off by default)
    "extra_test_globs": [],     # extra regexes counted as test paths
    "format_command": None,     # e.g. [["pnpm","exec","biome","check","--write","{files}"]]
    "suites": None,             # override autodetect: [{"kind":"vitest","dir":"packages/x"}]
    "min_days_between_prs": 6,
    "min_rebuild_s": 1.0,       # a file must cost >= this (CI or local) to be suggested for REBUILD on speed alone
}
TEST_STEP_RE = re.compile(r"\b(tests?|pytest|vitest|jest|mocha|spec|verify|e2e|contract|coverage|unit)\b", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")


def _p(*a: Any, err: bool = False) -> None:
    print(*a, file=sys.stderr if err else sys.stdout, flush=True)


def _fmt_s(s: float | None) -> str:
    if s is None:
        return "-"
    if s >= 120:
        return f"{s / 60:.1f}m"
    if s >= 10:
        return f"{s:.0f}s"
    return f"{s:.2f}s"


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _p90(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _dur(a: str | None, b: str | None) -> float | None:
    x, y = _parse_ts(a), _parse_ts(b)
    if not x or not y:
        return None
    d = (y - x).total_seconds()
    return d if d >= 0 else None


def trend_pct(durations_oldest_first: list[float]) -> float | None:
    """% change of the newer half's median vs the older half's (None when < 4 samples)."""
    xs = [d for d in durations_oldest_first if d is not None]
    if len(xs) < 4:
        return None
    h = len(xs) // 2
    old, new = statistics.median(xs[:h]), statistics.median(xs[h:])
    if old <= 0:
        return None
    return (new - old) / old * 100.0


# --- config / paths ----------------------------------------------------------------
def repo_config(entry: dict[str, Any], repos_cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    v = entry.get("test_refactor")
    if not v:
        return None
    out = dict(DEFAULTS)
    g = (repos_cfg or {}).get("test_refactor_defaults")
    if isinstance(g, dict):
        out.update({k: x for k, x in g.items() if not k.startswith("_")})
    if isinstance(v, dict):
        out.update({k: x for k, x in v.items() if not k.startswith("_")})
    if not out.get("enabled", True):
        return None
    return out


TEST_PATH_RES = [re.compile(p) for p in (
    r"(^|/)(tests?|__tests__|specs?|e2e|fixtures?|__fixtures__|__mocks__|testdata|test-?utils|test_utils)/",
    r"\.(test|spec)\.[cm]?[jt]sx?(\.snap)?$",
    r"(^|/)__snapshots__/",
    r"(^|/)test_[^/]*\.py$",
    r"_test\.(py|go)$",
    r"(^|/)conftest\.py$",
    r"(^|/)(vitest|jest|playwright)(\.[a-z]+)*\.config\.[cm]?[jt]s$",
    r"(^|/)(vitest|jest)\.(workspace|setup)[^/]*$",
    r"(^|/)(pytest\.ini|\.coveragerc)$",
)]
CI_CONFIG_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")


def is_test_path(path: str, *, allow_ci: bool = False, extra: list[str] | None = None) -> bool:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if any(r.search(p) for r in TEST_PATH_RES):
        return True
    if allow_ci and CI_CONFIG_RE.search(p):
        return True
    return any(re.search(x, p) for x in (extra or []))


def state_dir(repo: str) -> Path:
    return rv.FALLBACK_REVIEWS / repo.replace("/", "__")


def state_path(repo: str) -> Path:
    return state_dir(repo) / "TESTREFACTOR-STATE.json"


def load_state(repo: str) -> dict[str, Any]:
    p = state_path(repo)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(repo: str, st: dict[str, Any]) -> None:
    p = state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    st["runs"] = (st.get("runs") or [])[-26:]
    p.write_text(json.dumps(st, indent=2), encoding="utf-8")


# --- GitHub ------------------------------------------------------------------------
class GhError(RuntimeError):
    pass


def gh_json(path: str, timeout: int = 120) -> Any:
    cp = rv.run(["gh", "api", path], timeout=timeout)
    if cp.returncode != 0:
        raise GhError(rv._one_line(cp.stderr or cp.stdout or "gh api failed", 200))
    return json.loads(cp.stdout or "null")


def gh_bytes(path: str, timeout: int = 300) -> bytes | None:
    try:
        cp = subprocess.run(["gh", "api", path], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cp.stdout if cp.returncode == 0 and cp.stdout else None


def default_branch(repo: str) -> str:
    return (gh_json(f"repos/{repo}") or {}).get("default_branch") or "main"


# --- CI log / artifact parsing (pure) ---------------------------------------------
_VITEST_RUN_RE = re.compile(r"^\s*RUN\s+v[\d.]+\S*\s+(/\S+)")
_VITEST_FILE_RE = re.compile(
    r"^\s*(?:\|[^|]+\|\s+)?[✓✗×↓❯]\s+(?:\|[^|]+\|\s+)?(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s+"
    r"\((\d+) tests?[^)]*\)\s+(\d+(?:\.\d+)?)\s*(ms|s)\s*$")
_VITEST_TEST_RE = re.compile(r"^(\s{2,})[✓✗×]\s+(.+?)\s+(\d+(?:\.\d+)?)\s*(ms|s)\s*$")
_JEST_FILE_RE = re.compile(r"^\s*(PASS|FAIL)\s+(\S+)\s+\((\d+(?:\.\d+)?)\s*(m?s)\b")
_PYTEST_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s+(call|setup|teardown)\s+(\S+::\S+)")
_PYTEST_ROOT_RE = re.compile(r"^rootdir:\s*(/\S+)")


def _secs(v: str, unit: str) -> float:
    x = float(v)
    return x / 1000.0 if unit == "ms" else x


def runner_rel(abs_path: str) -> str:
    """/home/runner/work/<repo>/<repo>/packages/x -> packages/x ('' for the repo root)."""
    m = re.match(r"^/home/runner/work/[^/]+/[^/]+/?(.*)$", abs_path) or re.match(r"^/__w/[^/]+/[^/]+/?(.*)$", abs_path)
    return m.group(1).strip("/") if m else ""


def parse_ci_log(text: str) -> dict[str, Any]:
    """-> {"files": {relpath: secs}, "tests": {"relpath::name": secs}} from one job log."""
    files: dict[str, float] = {}
    tests: dict[str, float] = {}
    pkg = ""
    cur_file: str | None = None
    for raw in text.splitlines():
        line = _TS_RE.sub("", _ANSI_RE.sub("", raw)).rstrip()
        m = _VITEST_RUN_RE.match(line) or _PYTEST_ROOT_RE.match(line)
        if m:
            pkg, cur_file = runner_rel(m.group(1)), None
            continue
        m = _VITEST_FILE_RE.match(line)
        if m:
            rel = f"{pkg}/{m.group(1)}" if pkg else m.group(1)
            files[rel] = files.get(rel, 0.0) + _secs(m.group(3), m.group(4))
            cur_file = rel
            continue
        m = _JEST_FILE_RE.match(line)
        if m and re.search(r"\.(test|spec)\.", m.group(2)):
            rel = f"{pkg}/{m.group(2)}" if pkg else m.group(2)
            files[rel] = files.get(rel, 0.0) + _secs(m.group(3), "s" if m.group(4) == "s" else "ms")
            cur_file = None
            continue
        m = _PYTEST_DUR_RE.match(line)
        if m:
            nodeid = m.group(3)
            f, name = nodeid.split("::", 1)
            rel = f"{pkg}/{f}" if pkg else f
            tid = f"{rel}::{name}"
            tests[tid] = tests.get(tid, 0.0) + float(m.group(1))
            files[rel] = files.get(rel, 0.0) + float(m.group(1))
            continue
        m = _VITEST_TEST_RE.match(line)
        if m and cur_file:
            tests[f"{cur_file}::{m.group(2).strip()}"] = _secs(m.group(3), m.group(4))
    return {"files": files, "tests": tests}


def parse_junit(xml_text: str, root_prefix: str = "") -> dict[str, Any]:
    """JUnit XML -> {"files": {path: secs}, "tests": {"path::name": secs}, "skipped": [...], "failed": [...]}"""
    out: dict[str, Any] = {"files": {}, "tests": {}, "skipped": [], "failed": [], "collect_errors": []}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for tc in root.iter("testcase"):
        name = tc.get("name") or "?"
        f = tc.get("file") or ""
        if not f and not (tc.get("classname") or "") and re.fullmatch(r"[\w.]+", name) and "." in name:
            # pytest module-level entry (collection error or module skip): name is the dotted module path
            mod = f"{root_prefix}/{name.replace('.', '/')}.py".strip("/") if root_prefix else f"{name.replace('.', '/')}.py"
            if tc.find("error") is not None or tc.find("failure") is not None:
                out["collect_errors"].append(mod)
            elif tc.find("skipped") is not None:
                tid = f"{mod}::(module skipped)"
                out["tests"][tid] = 0.0
                out["files"].setdefault(mod, 0.0)
                out["skipped"].append(tid)
            continue
        if not f:
            cls = tc.get("classname") or ""
            if cls and "/" not in cls and not re.search(r"\.[cm]?[jt]sx?$", cls):
                parts = cls.split(".")
                # pytest classname: pkg.module[.Class] -> pkg/module.py (drop a trailing CamelCase class)
                if len(parts) > 1 and parts[-1][:1].isupper():
                    parts = parts[:-1]
                f = "/".join(parts) + ".py"
            else:
                f = cls
        f = f"{root_prefix}/{f}".strip("/") if root_prefix else f
        try:
            t = float(tc.get("time") or 0)
        except ValueError:
            t = 0.0
        tid = f"{f}::{name}"
        out["tests"][tid] = t
        out["files"][f] = out["files"].get(f, 0.0) + t
        if tc.find("skipped") is not None:
            out["skipped"].append(tid)
        if tc.find("failure") is not None or tc.find("error") is not None:
            out["failed"].append(tid)
    return out


def _zip_members(blob: bytes) -> list[tuple[str, bytes]]:
    out = []
    with tempfile.TemporaryFile() as fh:
        fh.write(blob)
        fh.seek(0)
        with zipfile.ZipFile(fh) as z:
            for n in z.namelist():
                out.append((n, z.read(n)))
    return out


# --- CI gather ---------------------------------------------------------------------
def gather_ci(repo: str, branch: str, cfg: dict[str, Any]) -> dict[str, Any]:
    days = int(cfg.get("ci_days") or 21)
    since = (rv.now_utc() - timedelta(days=days)).date().isoformat()
    out: dict[str, Any] = {"branch": branch, "since": since, "days": days, "workflows": [], "jobs": [],
                           "test_steps": [], "files": {}, "tests": {}, "notes": [], "junit": False,
                           "runs_total": 0, "logs_parsed": 0}
    try:
        data = gh_json(f"repos/{repo}/actions/runs?branch={branch}&status=completed&per_page=100"
                       f"&created=%3E%3D{since}", timeout=180)
    except (GhError, json.JSONDecodeError) as e:
        out["notes"].append(f"actions runs unavailable: {e}")
        return out
    runs = [r for r in ((data or {}).get("workflow_runs") or [])
            # Dependabot update jobs (event "dynamic") are not the repo's CI.
            if r.get("event") != "dynamic" and "dependabot" not in str((r.get("actor") or {}).get("login") or "").lower()]
    runs = runs[: int(cfg.get("ci_max_runs") or 60)]
    out["runs_total"] = len(runs)
    if not runs:
        out["notes"].append(f"no completed Actions runs on {branch} since {since}")
        return out
    by_wf: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        by_wf.setdefault(r.get("name") or str(r.get("workflow_id")), []).append(r)
    job_d: dict[tuple[str, str], list[float]] = {}
    step_d: dict[tuple[str, str, str], list[float]] = {}
    log_targets: list[tuple[str, dict[str, Any]]] = []
    for wf, rs in by_wf.items():
        rs.sort(key=lambda r: r.get("run_started_at") or r.get("created_at") or "")   # oldest first
        durs = [d for d in (_dur(r.get("run_started_at") or r.get("created_at"), r.get("updated_at")) for r in rs)
                if d is not None]
        ok = [r for r in rs if r.get("conclusion") == "success"]
        out["workflows"].append({"name": wf, "runs": len(rs), "success": len(ok), "median_s": _median(durs),
                                 "p90_s": _p90(durs), "trend_pct": trend_pct(durs)})
        has_test_step = False
        for r in rs[-int(cfg.get("ci_jobs_sample") or 8):]:
            try:
                jobs = (gh_json(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100") or {}).get("jobs") or []
            except (GhError, json.JSONDecodeError):
                continue
            for j in jobs:
                jd = _dur(j.get("started_at"), j.get("completed_at"))
                if jd is not None and j.get("conclusion") not in ("skipped", "cancelled"):
                    job_d.setdefault((wf, j.get("name") or "?"), []).append(jd)
                for s in j.get("steps") or []:
                    if not TEST_STEP_RE.search(s.get("name") or "") or s.get("conclusion") == "skipped":
                        continue
                    sd = _dur(s.get("started_at"), s.get("completed_at"))
                    if sd is not None:
                        has_test_step = True
                        step_d.setdefault((wf, j.get("name") or "?", s.get("name") or "?"), []).append(sd)
        if has_test_step:
            for r in ok[-int(cfg.get("ci_log_runs") or 2):]:
                log_targets.append((wf, r))
    for (wf, job), ds in job_d.items():
        out["jobs"].append({"workflow": wf, "job": job, "n": len(ds), "median_s": _median(ds), "p90_s": _p90(ds),
                            "max_s": max(ds), "trend_pct": trend_pct(ds)})
    for (wf, job, step), ds in step_d.items():
        out["test_steps"].append({"workflow": wf, "job": job, "step": step, "n": len(ds), "median_s": _median(ds),
                                  "p90_s": _p90(ds), "trend_pct": trend_pct(ds)})
    for k in ("jobs", "test_steps", "workflows"):
        out[k].sort(key=lambda x: -(x["median_s"] or 0))
    # Per-file / per-test timings: logs + JUnit artifacts of the latest green runs of test workflows.
    fsamples: dict[str, list[float]] = {}
    tsamples: dict[str, list[float]] = {}

    def add(parsed: dict[str, Any]) -> None:
        for k2, v in parsed["files"].items():
            fsamples.setdefault(k2, []).append(v)
        for k2, v in parsed["tests"].items():
            tsamples.setdefault(k2, []).append(v)

    for wf, r in log_targets:
        blob = gh_bytes(f"repos/{repo}/actions/runs/{r['id']}/logs")
        if blob:
            try:
                for n, data_ in _zip_members(blob):
                    if "/" not in n and n.endswith(".txt"):   # top-level per-job logs (subdirs repeat them)
                        add(parse_ci_log(data_.decode("utf-8", "replace")))
                out["logs_parsed"] += 1
            except zipfile.BadZipFile:
                out["notes"].append(f"run {r['id']} logs: not a zip")
        try:
            arts = (gh_json(f"repos/{repo}/actions/runs/{r['id']}/artifacts") or {}).get("artifacts") or []
        except (GhError, json.JSONDecodeError):
            arts = []
        for a in arts:
            if a.get("expired") or not re.search(r"junit|test.?results?|test.?report|pytest|vitest|jest",
                                                 a.get("name") or "", re.I):
                continue
            if int(a.get("size_in_bytes") or 0) > 25_000_000:
                continue
            ab = gh_bytes(f"repos/{repo}/actions/artifacts/{a['id']}/zip")
            if not ab:
                continue
            try:
                for n, data_ in _zip_members(ab):
                    if n.endswith(".xml"):
                        pj = parse_junit(data_.decode("utf-8", "replace"))
                        if pj["tests"]:
                            out["junit"] = True
                        add(pj)
            except zipfile.BadZipFile:
                pass
    out["files"] = {k: _median(v) for k, v in fsamples.items()}
    out["tests"] = {k: _median(v) for k, v in tsamples.items()}
    if not out["test_steps"]:
        out["notes"].append(f"no workflow on {branch} has a test step (CI does not run the tests); "
                            "cost comes from the local run only")
    elif not out["junit"]:
        out["notes"].append("no JUnit/test-report artifacts; CI per-test timings come from job logs only "
                            "(vitest lists only tests over its slow threshold)" if out["files"]
                            else "no JUnit artifacts and no per-test timings in the job logs")
    return out


# --- local worktree + suite detection ------------------------------------------------
def worktree_path(repo_path: Path) -> Path:
    return (repo_path.parent / ".reviews-wt" / "TESTREFACTOR").resolve()


def create_worktree(repo_path: Path, branch: str) -> tuple[Path, str]:
    wt = worktree_path(repo_path)
    rv.remove_worktree(repo_path, wt)
    f = rv.run(["git", "fetch", "--force", "origin", f"{branch}:refs/remotes/origin/{branch}"], cwd=repo_path,
               timeout=300)
    if f.returncode != 0:
        raise RuntimeError(f"git fetch origin {branch} failed: {rv._one_line(f.stderr, 200)}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    a = rv.run(["git", "worktree", "add", "--force", "--detach", str(wt), f"origin/{branch}"], cwd=repo_path,
               timeout=180)
    if a.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {rv._one_line(a.stderr or a.stdout, 200)}")
    sha = rv.run(["git", "rev-parse", "HEAD"], cwd=wt, timeout=30).stdout.strip()
    return wt, sha


def package_manager(wt: Path) -> str:
    if (wt / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (wt / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _read_pkg(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


NODE_SCRIPT_RE = re.compile(r"^node\s+(\S+\.[cm]?js)$")


def node_script_files(test_script: str) -> list[str] | None:
    """`node a.test.js && node b.test.js` -> [a.test.js, b.test.js]; anything else -> None."""
    parts = [x.strip() for x in re.split(r"&&|;", test_script or "") if x.strip()]
    files = []
    for part in parts:
        m = NODE_SCRIPT_RE.match(part)
        if not m:
            return None
        files.append(m.group(1))
    return files or None


def detect_suites(wt: Path, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(cfg.get("suites"), list) and cfg["suites"]:
        return [dict(s) for s in cfg["suites"] if isinstance(s, dict) and s.get("kind")]
    suites: list[dict[str, Any]] = []
    for pj in sorted(wt.rglob("package.json")):
        rel = pj.parent.relative_to(wt)
        if "node_modules" in rel.parts or len(rel.parts) > 3 or any(p.startswith(".") for p in rel.parts):
            continue
        d = _read_pkg(pj)
        test = ((d.get("scripts") or {}).get("test") or "").strip()
        dirs = "" if str(rel) == "." else str(rel)
        if "vitest" in test:
            suites.append({"kind": "vitest", "dir": dirs})
        elif re.search(r"\bjest\b", test):
            suites.append({"kind": "jest", "dir": dirs})
        elif dirs == "" and node_script_files(test):
            suites.append({"kind": "node-scripts", "dir": "", "files": node_script_files(test)})
    pyproj = wt / "pyproject.toml"
    if (wt / "pytest.ini").is_file() or (pyproj.is_file() and "[tool.pytest" in pyproj.read_text(errors="replace")) \
            or any((wt / "tests").glob("test_*.py")):
        suites.append({"kind": "pytest", "dir": ""})
    return suites


def suite_env(v8dir: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"CI": "1", "FORCE_COLOR": "0", "NO_COLOR": "1", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"})
    env.pop("NODE_V8_COVERAGE", None)
    if v8dir is not None:
        env["NODE_V8_COVERAGE"] = str(v8dir)
    return env


def install_deps(wt: Path, suites: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    if not cfg.get("install", True) or not any(s["kind"] in ("vitest", "jest") for s in suites):
        return "skipped (not needed)"
    pm = package_manager(wt)
    cmd = {"pnpm": ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"],
           "yarn": ["yarn", "install", "--frozen-lockfile"],
           "npm": ["npm", "ci", "--no-audit", "--no-fund"]}[pm]
    if not shutil.which(cmd[0]):
        return f"FAILED ({cmd[0]} not on PATH)"
    try:
        cp = subprocess.run(cmd, cwd=wt, env=suite_env(), capture_output=True, text=True, timeout=1200)
    except subprocess.TimeoutExpired:
        return "FAILED (install timed out)"
    if cp.returncode != 0:
        return f"FAILED ({' '.join(cmd[:2])}: {rv._one_line((cp.stderr or cp.stdout)[-400:], 200)})"
    return f"ok ({' '.join(cmd)})"


def _js_bin(wt: Path, tool: str) -> list[str]:
    pm = package_manager(wt)
    if pm == "pnpm":
        return ["pnpm", "exec", tool]
    if pm == "yarn":
        return ["yarn", tool]
    return ["npx", "--no-install", tool]


def parse_jest_json(data: dict[str, Any], wt: Path) -> dict[str, Any]:
    """vitest/jest JSON reporter -> {"tests": [...], "files": {rel: secs}}."""
    tests: list[dict[str, Any]] = []
    files: dict[str, float] = {}
    for tr in data.get("testResults") or []:
        name = tr.get("name") or tr.get("testFilePath") or "?"
        try:
            rel = str(Path(name).resolve().relative_to(wt.resolve()))
        except (ValueError, OSError):
            rel = name
        st, en = tr.get("startTime"), tr.get("endTime")
        fdur = ((en - st) / 1000.0 if isinstance(st, (int, float)) and isinstance(en, (int, float)) and en >= st
                else None)
        sumd = 0.0
        ar = tr.get("assertionResults") or []
        for a in ar:
            d = a.get("duration")
            d = float(d) / 1000.0 if isinstance(d, (int, float)) else 0.0
            sumd += d
            stt = (a.get("status") or "").lower()
            stt = {"pending": "skipped", "disabled": "skipped"}.get(stt, stt)
            nm = a.get("fullName") or a.get("title") or "?"
            tests.append({"id": f"{rel}::{nm}", "file": rel, "name": nm, "status": stt, "dur_s": d})
        if tr.get("status") == "failed" and not ar:
            tests.append({"id": f"{rel}::(file failed to load)", "file": rel, "name": "(file failed to load)",
                          "status": "failed", "dur_s": 0.0})
        files[rel] = fdur if fdur is not None else sumd
    return {"tests": tests, "files": files}


# Vitest workers are torn down without a clean exit, so NODE_V8_COVERAGE alone records nothing for them.
# A temporary setup file calls v8.takeCoverage() after each test file (which writes AND resets the
# counters) and renames the dump after a hash of that file's path: per-test-file coverage from ONE run.
# The setup file and a wrapper config (base config + that setupFile) are written into the package dir
# as dotfiles for the run only, then removed.
VITEST_HOOK = "/* pr-overseer test-refactor coverage hook (temporary) */\n" + r"""
import { afterAll, expect } from "vitest";
import v8 from "node:v8";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
afterAll(() => {
  const dir = process.env.NODE_V8_COVERAGE;
  if (!dir) return;
  let tp = "";
  try { tp = expect.getState().testPath || ""; } catch {}
  const pre = `coverage-${process.pid}-`;
  const before = new Set(fs.readdirSync(dir));
  v8.takeCoverage();
  const h = crypto.createHash("sha1").update(tp).digest("hex").slice(0, 16);
  let i = 0;
  for (const f of fs.readdirSync(dir)) {
    if (!before.has(f) && f.startsWith(pre)) {
      fs.renameSync(path.join(dir, f), path.join(dir, `tf-${h}-${process.pid}-${Date.now()}-${i++}.json`));
    }
  }
  fs.writeFileSync(path.join(dir, `tf-${h}.path`), tp);
});
"""
HOOK_NAME = ".overseer-cov-hook.mjs"
WRAP_NAME = ".overseer-vitest.config.mjs"


def _vitest_wrapper(pkg_dir: Path) -> str:
    base = next((pkg_dir / n for n in ("vitest.config.ts", "vitest.config.mts", "vitest.config.js",
                                       "vitest.config.mjs", "vitest.config.cts", "vite.config.ts", "vite.config.mts",
                                       "vite.config.js", "vite.config.mjs") if (pkg_dir / n).is_file()), None)
    hook = json.dumps("./" + HOOK_NAME)
    if base is None:
        return f"export default {{ test: {{ setupFiles: [{hook}] }} }};\n"
    return (f"import base from {json.dumps('./' + base.name)};\n"
            "const extra = (c) => { c = { ...(c || {}) }; c.test = { ...(c.test || {}) };\n"
            f"  c.test.setupFiles = [...[].concat(c.test.setupFiles || []), {hook}]; return c; }};\n"
            "export default typeof base === 'function' ? async (env) => extra(await base(env)) : extra(await base);\n")


def v8_per_testfile(v8dir: str | Path, wt: Path) -> tuple[dict[str, Cov], dict[str, int], Cov]:
    """Hook dumps -> ({test file: Cov}, {source: length}, Cov of un-attributed dumps e.g. spawned processes)."""
    d = Path(v8dir)
    per: dict[str, Cov] = {}
    lengths: dict[str, int] = {}
    if not d.is_dir():
        return {}, {}, {}
    names: dict[str, str] = {}
    for pf in d.glob("tf-*.path"):
        tp = pf.read_text(encoding="utf-8", errors="replace").strip()
        try:
            names[pf.name[3:-5]] = str(Path(tp).resolve().relative_to(wt.resolve()))
        except (ValueError, OSError):
            continue
    tmp = Path(tempfile.mkdtemp(prefix="ovs-v8-"))
    try:
        groups: dict[str, list[Path]] = {}
        rest: list[Path] = []
        for fp in d.glob("*.json"):
            m = re.match(r"^tf-([0-9a-f]{16})-", fp.name)
            if m and m.group(1) in names:
                groups.setdefault(names[m.group(1)], []).append(fp)
            elif not m:
                rest.append(fp)
        for tf, fps in list(groups.items()) + [("", rest)]:
            sub = tmp / (_slug(tf) or "rest")
            sub.mkdir(parents=True, exist_ok=True)
            for fp in fps:
                (sub / fp.name).symlink_to(fp.resolve())
            c, ln = v8_coverage(sub, wt)
            for k, v in ln.items():
                lengths[k] = max(lengths.get(k, 0), v)
            if tf:
                per[tf] = merge_cov([per.get(tf) or {}, c])
            else:
                rest_cov = c
        return per, lengths, (rest_cov if rest else {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_js_suite(wt: Path, suite: dict[str, Any], outdir: Path, tag: str, *, files: list[str] | None = None,
                 exclude: list[str] | None = None, v8dir: Path | None = None, timeout: int = 2400) -> dict[str, Any]:
    d = wt / suite["dir"] if suite["dir"] else wt
    slug = re.sub(r"[^A-Za-z0-9]+", "_", suite["dir"] or "root")
    out = outdir / f"{suite['kind']}-{slug}-{tag}.json"
    out.unlink(missing_ok=True)

    def rel_to_pkg(f: str) -> str:
        return str((wt / f).resolve().relative_to(d.resolve()))

    temp_files: list[Path] = []
    if suite["kind"] == "vitest":
        cmd = _js_bin(wt, "vitest") + ["run", "--reporter=json", f"--outputFile={out}", "--passWithNoTests"]
        if v8dir is not None:
            (d / HOOK_NAME).write_text(VITEST_HOOK, encoding="utf-8")
            (d / WRAP_NAME).write_text(_vitest_wrapper(d), encoding="utf-8")
            temp_files += [d / HOOK_NAME, d / WRAP_NAME]
            cmd += ["--config", WRAP_NAME]
        for e in exclude or []:
            cmd += ["--exclude", rel_to_pkg(e)]
    else:
        cmd = _js_bin(wt, "jest") + ["--json", f"--outputFile={out}", "--passWithNoTests", "--ci"]
        for e in exclude or []:
            cmd += ["--testPathIgnorePatterns", re.escape(rel_to_pkg(e))]
    cmd += [rel_to_pkg(f) for f in files or []]
    if v8dir is not None:
        v8dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        cp = subprocess.run(cmd, cwd=d, env=suite_env(v8dir), capture_output=True, text=True, timeout=timeout)
        rc, tail = cp.returncode, (cp.stderr or cp.stdout or "")[-600:]
    except subprocess.TimeoutExpired:
        rc, tail = -9, "timed out"
    finally:
        for tf in temp_files:
            tf.unlink(missing_ok=True)
        for leftover in d.glob(WRAP_NAME + ".timestamp-*"):
            leftover.unlink(missing_ok=True)
    res: dict[str, Any] = {"suite": suite, "rc": rc, "wall_s": time.monotonic() - t0, "tests": [], "files": {},
                           "error": None}
    if out.is_file():
        try:
            res.update(parse_jest_json(json.loads(out.read_text(encoding="utf-8")), wt))
        except (OSError, json.JSONDecodeError) as e:
            res["error"] = f"bad json report: {e}"
    else:
        res["error"] = f"no json report (exit {rc}): {rv._one_line(tail, 300)}"
    res["ok"] = rc == 0 and not res["error"] and not any(t["status"] == "failed" for t in res["tests"])
    return res


def run_node_scripts(wt: Path, suite: dict[str, Any], outdir: Path, tag: str, *, files: list[str] | None = None,
                     v8root: Path | None = None, timeout: int = 2400) -> dict[str, Any]:
    res: dict[str, Any] = {"suite": suite, "rc": 0, "wall_s": 0.0, "tests": [], "files": {}, "error": None,
                           "v8dirs": {}}
    for f in (files if files is not None else list(suite.get("files") or [])):
        if not (wt / f).is_file():
            continue   # deleted by the refactor
        v8 = (v8root / re.sub(r"[^A-Za-z0-9]+", "_", f)) if v8root is not None else None
        if v8 is not None:
            shutil.rmtree(v8, ignore_errors=True)
            v8.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        try:
            cp = subprocess.run(["node", f], cwd=wt, env=suite_env(v8), capture_output=True, text=True,
                                timeout=min(timeout, 900))
            rc, outp = cp.returncode, (cp.stdout or "") + (cp.stderr or "")
        except subprocess.TimeoutExpired:
            rc, outp = -9, "timed out"
        dt = time.monotonic() - t0
        res["wall_s"] += dt
        res["files"][f] = dt
        oks = [m.strip() for m in re.findall(r"^\s*(?:ok\b|✓|✔|PASS\b)\s*(?:\d+\s*)?-?\s*(.+)$", outp, re.M)]
        for name in oks:
            res["tests"].append({"id": f"{f}::{name}", "file": f, "name": name, "status": "passed",
                                 "dur_s": dt / len(oks)})
        if rc != 0:
            res["rc"] = rc
            res["tests"].append({"id": f"{f}::(script)", "file": f, "name": "(script)", "status": "failed",
                                 "dur_s": dt, "detail": rv._one_line(outp[-400:], 300)})
        elif not oks:
            res["tests"].append({"id": f"{f}::(script)", "file": f, "name": "(script)", "status": "passed",
                                 "dur_s": dt})
        if v8 is not None:
            res["v8dirs"][f] = str(v8)
    res["ok"] = res["rc"] == 0
    return res


def _python_cmd(wt: Path, repo_path: Path) -> list[str]:
    for venv in (wt / ".venv", repo_path / ".venv", repo_path / "venv"):
        py = venv / "bin" / "python"
        if py.is_file():
            return [str(py)]
    if (wt / "uv.lock").is_file() and shutil.which("uv"):
        return ["uv", "run", "--frozen", "python"]
    return [sys.executable or "python3"]


def run_pytest(wt: Path, suite: dict[str, Any], outdir: Path, tag: str, repo_path: Path, *,
               nodeids: list[str] | None = None, coverage: bool = False, timeout: int = 2400) -> dict[str, Any]:
    junit = outdir / f"pytest-{tag}.xml"
    junit.unlink(missing_ok=True)
    py = _python_cmd(wt, repo_path)
    cmd = py + ["-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={junit}", "-o", "junit_family=xunit2"]
    has_cov = coverage and subprocess.run(py + ["-c", "import pytest_cov"], cwd=wt, capture_output=True,
                                          timeout=60).returncode == 0
    if has_cov:
        cmd += ["--cov=.", "--cov-context=test", "--cov-report="]
    cmd += nodeids or []
    t0 = time.monotonic()
    try:
        cp = subprocess.run(cmd, cwd=wt, env=suite_env(), capture_output=True, text=True, timeout=timeout)
        rc, tail = cp.returncode, (cp.stdout or "")[-600:] + (cp.stderr or "")[-300:]
    except subprocess.TimeoutExpired:
        rc, tail = -9, "timed out"
    res: dict[str, Any] = {"suite": suite, "rc": rc, "wall_s": time.monotonic() - t0, "tests": [], "files": {},
                           "error": None}
    if junit.is_file():
        pj = parse_junit(junit.read_text(encoding="utf-8", errors="replace"))
        skipped, failed = set(pj["skipped"]), set(pj["failed"])
        for tid, t in pj["tests"].items():
            res["tests"].append({"id": tid, "file": tid.split("::", 1)[0], "name": tid.split("::", 1)[1],
                                 "status": "failed" if tid in failed else ("skipped" if tid in skipped else "passed"),
                                 "dur_s": t})
        res["files"] = pj["files"]
        if pj.get("collect_errors") or rc in (2, 3, 4) or "error during collection" in tail \
                or "Interrupted:" in tail:
            res["error"] = (f"pytest collection failed (exit {rc}"
                            + (f"; {', '.join(pj['collect_errors'][:3])}" if pj.get("collect_errors") else "")
                            + f"): {rv._one_line(tail, 300)}")
    else:
        res["error"] = f"no junit report (exit {rc}): {rv._one_line(tail, 300)}"
    covjson = outdir / f"cov-{tag}.json"
    if has_cov and (wt / ".coverage").is_file():
        cj = subprocess.run(py + ["-m", "coverage", "json", "--show-contexts", "-o", str(covjson)], cwd=wt,
                            capture_output=True, text=True, timeout=600)
        if cj.returncode == 0 and covjson.is_file():
            res["pycov"] = str(covjson)
        (wt / ".coverage").unlink(missing_ok=True)
    res["ok"] = rc in (0, 5) and not res["error"] and not any(t["status"] == "failed" for t in res["tests"])
    return res


def pytest_unique_coverage(cov_json_path: str) -> tuple[dict[str, int], float | None]:
    """coverage.py JSON with contexts -> ({test_file: unique_lines}, total line %)."""
    try:
        data = json.loads(Path(cov_json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, None
    uniq: dict[str, int] = {}
    for fname, fd in (data.get("files") or {}).items():
        if is_test_path(fname):
            continue
        for _line, ctxs in (fd.get("contexts") or {}).items():
            tfiles = {c.split("::", 1)[0] for c in ctxs if c and "::" in c}
            if len(tfiles) == 1:
                f = next(iter(tfiles))
                uniq[f] = uniq.get(f, 0) + 1
    pct = (data.get("totals") or {}).get("percent_covered")
    return uniq, (float(pct) if pct is not None else None)


# --- V8 coverage ---------------------------------------------------------------------
def covered_intervals(ranges: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    """V8 block ranges (start, end, count; nested, innermost wins) -> merged covered byte intervals."""
    rs = sorted(ranges, key=lambda r: (r[0], -r[1]))
    out: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = []   # (end, count)
    pos = 0

    def emit(a: int, b: int, c: int) -> None:
        if b > a and c > 0:
            if out and out[-1][1] >= a:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))

    for s, e, c in rs:
        while stack and stack[-1][0] <= s:
            ee, cc = stack.pop()
            emit(pos, ee, cc)
            pos = max(pos, ee)
        if stack:
            emit(pos, s, stack[-1][1])
        stack.append((e, c))
        pos = s
    while stack:
        ee, cc = stack.pop()
        emit(pos, ee, cc)
        pos = max(pos, ee)
    return out


def _merge_iv(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for a, b in sorted(iv):
        if merged and merged[-1][1] >= a:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _interval_minus(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    """Bytes in a not covered by b (both sorted + merged)."""
    total, j = 0, 0
    for s, e in a:
        cur = s
        while j < len(b) and b[j][1] <= cur:
            j += 1
        k = j
        while k < len(b) and b[k][0] < e:
            if b[k][0] > cur:
                total += b[k][0] - cur
            cur = max(cur, b[k][1])
            k += 1
        if cur < e:
            total += e - cur
    return total


_CODE_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".gs")


def norm_url(url: str, wt: Path) -> str | None:
    """Script URL -> worktree-relative source path; None for node internals, node_modules, outside paths."""
    if not url or url.startswith(("node:", "internal/", "evalmachine", "data:")):
        return None
    if url.startswith("file://"):
        url = url[7:]
    url = url.split("?", 1)[0]
    if url.startswith("/"):
        try:
            rel = str(Path(url).resolve().relative_to(wt.resolve()))
        except (ValueError, OSError):
            return None
        if "node_modules" in rel.split("/"):
            return None
        return rel
    # vm.runInContext(code, ctx, {filename: "util.js"}) style relative names
    return url if url.endswith(_CODE_EXT) and "node_modules" not in url else None


Cov = dict[str, list[tuple[int, int]]]


def v8_coverage(v8dir: str | Path, wt: Path) -> tuple[Cov, dict[str, int]]:
    """NODE_V8_COVERAGE dir -> ({source path: covered intervals}, {source path: script length}); test files dropped."""
    per: dict[str, list[tuple[int, int, int]]] = {}
    lengths: dict[str, int] = {}
    d = Path(v8dir)
    if not d.is_dir():
        return {}, {}
    cov: Cov = {}
    for fp in d.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for scr in data.get("result") or []:
            rel = norm_url(scr.get("url") or "", wt)
            if not rel or is_test_path(rel):
                continue
            rs = [(int(r.get("startOffset", 0)), int(r.get("endOffset", 0)), int(r.get("count", 0)))
                  for fn in scr.get("functions") or [] for r in fn.get("ranges") or []]
            if not rs:
                continue
            lengths[rel] = max(lengths.get(rel, 0), max(e for _s, e, _c in rs))
            # Each process's script is evaluated on its own (innermost range wins per process), then unioned.
            cov.setdefault(rel, []).extend(covered_intervals(rs))
    return {k: _merge_iv(v) for k, v in cov.items()}, lengths


def merge_cov(maps: list[Cov]) -> Cov:
    out: Cov = {}
    for m in maps:
        for url, iv in m.items():
            out.setdefault(url, []).extend(iv)
    return {k: _merge_iv(v) for k, v in out.items()}


def unique_bytes(full: Cov, without: Cov) -> int:
    return sum(_interval_minus(iv, without.get(url, [])) for url, iv in full.items())


def cov_total(m: Cov) -> int:
    return sum(b - a for iv in m.values() for a, b in iv)


def v8_pct(cov: Cov, lengths: dict[str, int]) -> float | None:
    """Byte coverage % over the scripts in `lengths` (pass the BEFORE lengths so files that stop being
    loaded after a deletion count as uncovered instead of vanishing from the denominator)."""
    denom = sum(lengths.values())
    if not denom:
        return None
    num = sum(min(lengths[u], sum(b - a for a, b in cov.get(u, []))) for u in lengths)
    return 100.0 * num / denom


# --- static scan ---------------------------------------------------------------------
SLEEP_RES = [re.compile(p) for p in (
    r"\bsleep\s*\(\s*\d", r"setTimeout\s*\([^;]{0,80},\s*\d{3,}\s*\)", r"waitForTimeout\s*\(",
    r"\btime\.sleep\s*\(", r"\basyncio\.sleep\s*\(\s*(?:0\.[1-9]|[1-9])", r"\bdelay\s*\(\s*\d{3,}")]
SPAWN_RES = [re.compile(p) for p in (
    r"\b(spawn|spawnSync|execa|execFileSync|execSync|fork)\s*\(", r"child_process",
    r"\bsubprocess\.(run|Popen|check_output)", r"fresh process", r"testcontainers|PostgreSqlContainer|GenericContainer")]
SKIP_RES = [re.compile(p) for p in (
    r"\b(it|test|describe)\.(skip|todo)\b", r"\bx(it|describe|test)\s*\(", r"@pytest\.mark\.(skip|xfail)",
    r"\bpytest\.skip\(", r"@unittest\.skip", r"\b(it|test)\.skipIf\(")]
MOCK_RES = [re.compile(p) for p in (
    r"\bvi\.(mock|fn|spyOn|stubGlobal)\b", r"\bjest\.(mock|fn|spyOn)\b", r"\bmock\.patch\b", r"\bMagicMock\b",
    r"\bmonkeypatch\.", r"\bsinon\.")]
ASSERT_RES = [re.compile(p) for p in (r"\bexpect\s*\(", r"\bassert\w*\b", r"\.should\b",
                                      r"\bt\.(is|deepEqual|true|false)\(")]
RETIRED_RE = re.compile(r"\b(retired|obsolete|deprecated|legacy|no longer (?:used|needed)|dead code|remove me|"
                        r"todo:? remove|refuses to run)\b", re.I)
TEST_DECL_RE = re.compile(r"\b(?:it|test)(?:\.\w+)?\s*\(\s*(['\"`])(.+?)\1|^\s*(?:async\s+)?def\s+(test_\w+)", re.M)


def static_scan(text: str) -> dict[str, Any]:
    names = [m.group(2) or m.group(3) for m in TEST_DECL_RE.finditer(text)]
    return {
        "sleeps": sum(len(r.findall(text)) for r in SLEEP_RES),
        "spawns": sum(len(r.findall(text)) for r in SPAWN_RES),
        "skips": sum(len(r.findall(text)) for r in SKIP_RES),
        "mocks": sum(len(r.findall(text)) for r in MOCK_RES),
        "asserts": sum(len(r.findall(text)) for r in ASSERT_RES),
        "retired": bool(RETIRED_RE.search(text)),
        "decls": len(names),
        "dup_names": sorted({n for n in names if names.count(n) > 1}),
        "lines": text.count("\n") + 1,
    }


# --- scoring (pure) ------------------------------------------------------------------
def score_candidates(local_files: dict[str, float], local_tests: list[dict[str, Any]],
                     ci_files: dict[str, float], ci_tests: dict[str, float],
                     static: dict[str, dict[str, Any]], unique: dict[str, dict[str, Any]],
                     flaky: set[str], min_rebuild_s: float = 1.0) -> list[dict[str, Any]]:
    """Rank test files by cost (max of CI median and local runtime) and low impact."""
    files = set(local_files) | set(ci_files)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for t in local_tests:
        by_file.setdefault(t["file"], []).append(t)
    costs = {f: max(local_files.get(f) or 0.0, ci_files.get(f) or 0.0) for f in files}
    max_cost = max(list(costs.values()) + [0.001])
    out = []
    for f in files:
        ts = [t for t in by_file.get(f, []) if t["name"] != "(script)"]
        st = static.get(f) or {}
        reasons: list[str] = []
        hints: list[str] = []
        low = 0.0
        n = len(ts)
        nskip = sum(1 for t in ts if t["status"] in ("skipped", "todo"))
        if n and nskip == n:
            low += 0.6
            reasons.append(f"all {n} tests skipped/todo")
        elif n and nskip:
            low += 0.15 * nskip / n
            reasons.append(f"{nskip}/{n} tests skipped/todo")
        if st.get("retired"):
            low += 0.4
            reasons.append("retired/obsolete wording in the file")
        u = unique.get(f)
        if u is not None and u.get("unique") is not None:
            ub, tot, unit = u["unique"], u.get("total") or 0, u.get("unit") or "bytes"
            if ub == 0 and st.get("spawns"):
                reasons.append("unique coverage not attributable (its code runs in spawned processes)")
            elif ub == 0 and u.get("covered") == 0:
                reasons.append("covers no non-test code (pure-logic test or coverage not attributable)")
            elif ub == 0:
                low += 0.5
                reasons.append("no unique coverage (all code it runs is also run by other tests)")
            else:
                # Share of what this file executes that nothing else executes (falls back to the suite total).
                own = u.get("covered") or 0
                ratio, of = (ub / own, "of what it covers") if own else ((ub / tot, "of all covered code") if tot
                                                                          else (None, ""))
                if ratio is not None and ratio < 0.01:
                    low += 0.3
                    reasons.append(f"tiny unique coverage ({ub} {unit}, {ratio:.2%} {of})")
                else:
                    reasons.append(f"unique coverage {ub} {unit}" + (f" ({ratio:.1%} {of})" if ratio is not None else ""))
        if st and n and st.get("decls", 0) and st.get("asserts", 1) == 0:
            low += 0.3
            reasons.append("no assertions found")
        if n and st.get("mocks", 0) >= 5 and st.get("asserts", 0) / n < 1.5:
            low += 0.15
            reasons.append(f"mock-heavy ({st['mocks']} mocks, {st.get('asserts', 0)} asserts, {n} tests)")
        if st.get("dup_names"):
            low += 0.1
            reasons.append(f"duplicate test names: {', '.join(st['dup_names'][:3])}")
        low = min(1.0, low)
        is_flaky = f in flaky
        if is_flaky:
            reasons.append("flaky (failed, then passed on re-run)")
            hints.append("make it deterministic (fake timers, no shared state)")
        if st.get("sleeps"):
            hints.append(f"{st['sleeps']} real sleep/timeout call(s): fake timers or event-driven waits")
        if st.get("spawns"):
            hints.append("spawns processes/containers: share one per file or mock the boundary")
        slow: list[tuple[str, float | None, float | None]] = []
        for t in ts:
            if t["dur_s"] >= 0.25:
                slow.append((t["name"], t["dur_s"], ci_tests.get(t["id"])))
        names = {s[0] for s in slow}
        for tid, d in ci_tests.items():
            if tid.startswith(f + "::") and (d or 0) >= 0.25:
                nm = tid.split("::", 1)[1]
                if not any(nm == x or x.endswith(" " + nm) or nm.endswith(" " + x) for x in names):
                    slow.append((nm, None, d))
        slow.sort(key=lambda x: -max(x[1] or 0, x[2] or 0))
        if slow and costs[f] > 0 and n > 1 and max(slow[0][1] or 0, slow[0][2] or 0) >= 0.6 * costs[f]:
            hints.append(f"one test dominates the file ({slow[0][0][:60]})")
        cost_norm = costs[f] / max_cost
        score = 0.6 * cost_norm + 0.4 * low + (0.25 if is_flaky else 0.0)
        if low >= 0.5:
            action = "DELETE"
        elif is_flaky or (costs[f] >= min_rebuild_s and (cost_norm >= 0.15 or st.get("sleeps") or st.get("spawns"))):
            action = "REBUILD"
        else:
            action = "REVIEW"
        out.append({"file": f, "score": round(score, 4), "cost_s": costs[f], "local_s": local_files.get(f),
                    "ci_s": ci_files.get(f), "tests": n, "skipped": nskip, "low_impact": round(low, 3),
                    "flaky": is_flaky, "reasons": reasons, "hints": hints, "slow_tests": slow[:3],
                    "suggested": action, "local": f in local_files})
    out.sort(key=lambda c: (-c["score"], c["file"]))
    return out


def pick_harness_candidates(ranked: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """Top N that ran locally (CI-only files can't be verified by the guardrails) and are worth a look."""
    return [c for c in ranked if c.get("file") and c["local"] and c["suggested"] in ("REBUILD", "DELETE")][:top_n]


# --- guardrails (pure) -----------------------------------------------------------------
DECISION_RE = re.compile(r"^\W*DECISION:\s*\**\s*(.+?)\s*\|\s*(REBUILD|DELETE|KEEP)\s*\|\s*(.+?)\s*$", re.I | re.M)


def parse_decisions(out: str) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for m in DECISION_RE.finditer(out or ""):
        f = m.group(1).strip().strip("`*").strip()
        seen[f] = {"file": f, "action": m.group(2).upper(), "why": m.group(3).strip().replace("\u2014", "-")}
    return list(seen.values())


def summarize_run(results: list[dict[str, Any]]) -> dict[str, Any]:
    tests = [t for r in results for t in r.get("tests") or []]
    files: dict[str, float] = {}
    for r in results:
        files.update(r.get("files") or {})
    per_file: dict[str, int] = {}
    for t in tests:
        if t["name"] == "(script)":
            per_file.setdefault(t["file"], 0)
        elif t["status"] in ("passed", "failed"):
            per_file[t["file"]] = per_file.get(t["file"], 0) + 1
    real = [t for t in tests if t["name"] != "(script)"]
    return {
        "ok": bool(results) and all(r.get("ok") for r in results),
        "test_time_s": sum(files.values()),
        "wall_s": sum(r.get("wall_s") or 0 for r in results),
        "tests": sum(1 for t in real if t["status"] in ("passed", "failed")),
        "passed": sum(1 for t in real if t["status"] == "passed"),
        "failed": [t["id"] for t in tests if t["status"] == "failed"],
        "skipped": [t["id"] for t in real if t["status"] in ("skipped", "todo")],
        "per_file": per_file,
        "files": files,
        "errors": [r["error"] for r in results if r.get("error")],
    }


TEST_FILE_NAME_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+\.(test|spec)\.[cm]?[jt]sx?)$")


def unrun_changed_tests(after: dict[str, Any], changed: list[str], deleted: set[str],
                        wt: Path | None = None) -> list[str]:
    """Changed test files (by name) that still exist but had no passing test in the after-run."""
    ran = {t.get("file") for r in (after.get("results") or []) for t in (r.get("tests") or [])
           if t.get("status") == "passed"}
    out = []
    for p in changed:
        if not TEST_FILE_NAME_RE.search(p) or p in deleted:
            continue
        if wt is not None and not (wt / p).exists():
            continue
        if p not in ran:
            out.append(p)
    return out


def check_guardrails(before: dict[str, Any], after: dict[str, Any], changed: list[str], changed_lines: int,
                     decisions: list[dict[str, str]], cfg: dict[str, Any],
                     cov_before: float | None = None, cov_after: float | None = None,
                     wt: Path | None = None) -> tuple[bool, list[str], dict[str, Any]]:
    fails: list[str] = []
    allow_ci, extra = bool(cfg.get("allow_ci_config")), list(cfg.get("extra_test_globs") or [])
    if not before.get("ok"):
        fails.append("suite was not green before the change")
    if not after.get("ok"):
        fails.append(f"suite not green after the change ({len(after.get('failed') or [])} failing: "
                     f"{', '.join((after.get('failed') or [])[:3])})")
    bad = [p for p in changed if not is_test_path(p, allow_ci=allow_ci, extra=extra)]
    if bad:
        fails.append(f"non-test files changed: {', '.join(bad[:5])}")
    if not changed:
        fails.append("no changes")
    cap = int(cfg.get("max_changed_lines") or 300)
    if changed_lines > cap:
        fails.append(f"{changed_lines} changed lines > cap {cap}")
    tol = float(cfg.get("coverage_tolerance_pct") if cfg.get("coverage_tolerance_pct") is not None else 0.5)
    if cov_before is not None and cov_after is not None and cov_after < cov_before - tol:
        fails.append(f"coverage dropped {cov_before:.2f}% -> {cov_after:.2f}% (tolerance {tol} pts)")
    deleted = {d["file"] for d in decisions if d["action"] == "DELETE"}
    unrun = unrun_changed_tests(after, changed, deleted, wt)
    if unrun:
        fails.append("edited test files did not run locally after the change (cannot verify): "
                     + ", ".join(unrun[:4]))
    unjustified = [f"{f} ({n}->{(after.get('per_file') or {}).get(f, 0)})"
                   for f, n in (before.get("per_file") or {}).items()
                   if (after.get("per_file") or {}).get(f, 0) < n and f not in deleted]
    if unjustified:
        fails.append("test count dropped without a DELETE decision: " + ", ".join(unjustified[:4]))
    bt, at = before.get("test_time_s") or 0.0, after.get("test_time_s") or 0.0
    reg = float(cfg.get("runtime_regression_pct") or 5.0)
    if bt > 0 and at > bt * (1 + reg / 100.0) and at - bt > 0.5:
        fails.append(f"local test time got worse {_fmt_s(bt)} -> {_fmt_s(at)}")
    metrics = {"before_s": bt, "after_s": at, "saved_s": bt - at, "tests_before": before.get("tests"),
               "tests_after": after.get("tests"), "cov_before": cov_before, "cov_after": cov_after,
               "changed_lines": changed_lines, "changed_files": len(changed)}
    return (not fails), fails, metrics


def deleted_unique_failures(decisions: list[dict[str, str]], uniq: dict[str, dict[str, Any]], wt: Path,
                            tol_pct: float) -> list[str]:
    """A DELETEd file that no longer exists must not have carried > tol_pct of its suite's covered code uniquely."""
    out = []
    for d in decisions:
        u = uniq.get(d["file"])
        if d["action"] != "DELETE" or not u or (wt / d["file"]).exists():
            continue
        ub, tot = u.get("unique") or 0, u.get("total") or 0
        if ub and (not tot or ub / tot * 100.0 > tol_pct):
            out.append(f"deleted {d['file']} carried unique coverage ({ub} {u.get('unit') or 'bytes'}"
                       + (f", {ub / tot:.2%}" if tot else "") + ")")
    return out


# --- local measurement orchestration -------------------------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "root")[-80:]


def measure(wt: Path, repo_path: Path, suites: list[dict[str, Any]], outdir: Path, tag: str,
            cfg: dict[str, Any], *, v8: bool = True, pycov: bool = True) -> dict[str, Any]:
    """Run every suite once (+ one re-run of failing files: pass on re-run = flaky)."""
    results = []
    flaky: set[str] = set()
    still_failing: list[str] = []
    v8maps: list[Cov] = []
    v8_files: dict[str, Cov] = {}
    lengths: dict[str, int] = {}
    pycov_json = None
    timeout = int(cfg.get("local_timeout") or 2400)
    for s in suites:
        v8dir = outdir / f"v8-{tag}-{_slug(s.get('dir') or '')}" if v8 else None
        if v8dir is not None:
            shutil.rmtree(v8dir, ignore_errors=True)
        if s["kind"] in ("vitest", "jest"):
            r = run_js_suite(wt, s, outdir, tag, v8dir=v8dir, timeout=timeout)
            if v8dir is not None and s["kind"] == "vitest":
                per, ln, rest = v8_per_testfile(v8dir, wt)
                v8_files.update(per)
                v8maps.extend(list(per.values()) + [rest])
                for k, v in ln.items():
                    lengths[k] = max(lengths.get(k, 0), v)
                shutil.rmtree(v8dir, ignore_errors=True)
        elif s["kind"] == "node-scripts":
            r = run_node_scripts(wt, s, outdir, tag, v8root=v8dir, timeout=timeout)
            for tf, d in (r.get("v8dirs") or {}).items():
                c, ln = v8_coverage(d, wt)
                v8_files[tf] = c
                v8maps.append(c)
                for k, v in ln.items():
                    lengths[k] = max(lengths.get(k, 0), v)
        elif s["kind"] == "pytest":
            r = run_pytest(wt, s, outdir, tag, repo_path, coverage=pycov, timeout=timeout)
            pycov_json = r.get("pycov") or pycov_json
        else:
            continue
        failing_files = sorted({t["file"] for t in r["tests"] if t["status"] == "failed"})
        if failing_files and not r.get("error"):
            if s["kind"] in ("vitest", "jest"):
                rr = run_js_suite(wt, s, outdir, tag + "-rerun", files=failing_files, timeout=timeout)
            elif s["kind"] == "node-scripts":
                rr = run_node_scripts(wt, s, outdir, tag + "-rerun", files=failing_files, timeout=timeout)
            else:
                rr = run_pytest(wt, s, outdir, tag + "-rerun", repo_path,
                                nodeids=[t["id"] for t in r["tests"] if t["status"] == "failed"], timeout=timeout)
            re_failed = {t["file"] for t in rr["tests"] if t["status"] == "failed"}
            re_passed = {t["file"] for t in rr["tests"] if t["status"] == "passed"}
            # a re-run that ran nothing (e.g. pytest exit 5 on a bogus node id) proves nothing
            re_failed |= {f for f in failing_files if f not in re_passed}
            if rr.get("error"):
                re_failed |= set(failing_files)
            for f in failing_files:
                (still_failing.append(f) if f in re_failed else flaky.add(f))
            if not re_failed:
                for t in r["tests"]:
                    if t["status"] == "failed":
                        t["status"], t["flaky"] = "passed", True
                r["tests"] = [t for t in r["tests"] if not (t["name"] == "(script)" and t.get("flaky")
                                                            and any(x["file"] == t["file"] and x["name"] != "(script)"
                                                                    for x in r["tests"]))]
                r["ok"] = True
        results.append(r)
    summ = summarize_run(results)
    summ.update({"results": results, "flaky": sorted(flaky), "still_failing": still_failing,
                 "v8": merge_cov(v8maps) if v8maps else {}, "v8_len": lengths, "v8_files": v8_files})
    if pycov_json:
        summ["pycov"] = pycov_json
    return summ


def unique_coverage(base: dict[str, Any], files: list[str]) -> dict[str, dict[str, Any]]:
    """Per test file: covered non-test code that no OTHER test file covers (from the per-file V8 dumps of the
    baseline run, or pytest-cov contexts). No extra suite runs."""
    out: dict[str, dict[str, Any]] = {}
    maps: dict[str, Cov] = base.get("v8_files") or {}
    if maps:
        total = cov_total(base.get("v8") or merge_cov(list(maps.values())))
        for f in files:
            if f in maps:
                others = merge_cov([m for g, m in maps.items() if g != f])
                out[f] = {"unique": unique_bytes(maps[f], others), "total": total, "unit": "bytes",
                          "covered": cov_total(maps[f])}
    if base.get("pycov"):
        uniq, _pct = pytest_unique_coverage(base["pycov"])
        for f in files:
            if f.endswith(".py"):
                out[f] = {"unique": uniq.get(f, 0), "total": None, "unit": "lines"}
    return out


def coverage_pct(summ: dict[str, Any], lengths: dict[str, int] | None = None) -> float | None:
    if summ.get("pycov"):
        return pytest_unique_coverage(summ["pycov"])[1]
    if summ.get("v8"):
        return v8_pct(summ["v8"], lengths if lengths is not None else (summ.get("v8_len") or {}))
    return None


# --- prompt / git helpers ------------------------------------------------------------
def build_prompt(repo: str, branch: str, cands: list[dict[str, Any]], ci: dict[str, Any], cfg: dict[str, Any]) -> str:
    lines = []
    for i, c in enumerate(cands, 1):
        slow = "; ".join(f"{n[:80]} (local {_fmt_s(l)}, CI {_fmt_s(cc)})" for n, l, cc in c["slow_tests"]) or "-"
        lines.append(f"{i}. {c['file']} -- suggested {c['suggested']}; cost {_fmt_s(c['cost_s'])} "
                     f"(local {_fmt_s(c['local_s'])}, CI median {_fmt_s(c['ci_s'])}); {c['tests']} tests "
                     f"({c['skipped']} skipped)\n   signals: {'; '.join(c['reasons']) or '-'}\n"
                     f"   hints: {'; '.join(c['hints']) or '-'}\n   slowest tests: {slow}")
    steps = "\n".join(f"- {s['workflow']} / {s['job']} / {s['step']}: median {_fmt_s(s['median_s'])}"
                      for s in (ci.get("test_steps") or [])[:5]) or "- (no CI test steps found)"
    cap = int(cfg.get("max_changed_lines") or 300)
    return f"""You are refactoring the TEST SUITE of {repo} (a throwaway checkout of origin/{branch}). This is the
weekly test-suite review: make the suite faster and leaner without losing meaningful coverage. The overseer measured
everything below before calling you; afterwards it re-runs the whole suite and coverage and discards your work if any
guardrail fails.

Do NOT commit, push, create branches or call gh/GitHub. Do not run installs. Edit files in place only.
{rv.NO_SELF_MENTION_RULE}

Hard rules (any violation discards the whole change):
- Edit ONLY test files, fixtures/helpers under test directories, and test runner config (vitest/jest/pytest config).
  NEVER edit production/source code, package manifests, lockfiles or CI workflows.
- At most {cap} changed lines in total. Prefer 2-4 solid improvements over many shallow ones.
- The suite must stay green. Never weaken an assertion just to make something faster.
- DELETE only tests whose behavior is genuinely covered elsewhere, obsolete/retired, or permanently skipped. If unsure,
  KEEP or REBUILD. "No unique coverage" means no unique JS/TS/Python code executed; tests of SQL, migrations,
  database contracts, config or concurrency can still be the only check of that behavior, so verify by reading the
  other tests before deleting, and cite the test(s) that cover it in the justification.
- REBUILD = same behavior checked, faster: fake timers instead of real sleeps, one expensive fixture/process/container
  per file instead of per test, mock slow I/O at the boundary, parametrize duplicates, split a test that checks too much.
  A REBUILD keeps every test case (the file's test count must not drop). If you remove ANY test cases from a file,
  even redundant ones while speeding up the rest, its decision MUST be DELETE (a partial delete): name the removed
  cases and the tests that still cover them. A test-count drop under REBUILD or KEEP discards the whole change.
- Keep the repo's style and test-layout conventions (read AGENTS.md / CONTRIBUTING.md if present). No em dashes.

CI runtime context (GitHub Actions, {branch}, last {ci.get('days')} days):
{steps}

Ranked candidates (cost = max(local, CI) runtime; signals = why they look slow or low-impact):
{chr(10).join(lines)}

Output (plain text):
1. For EVERY candidate above, exactly one line:
DECISION: <path> | REBUILD|DELETE|KEEP | <one sentence: what changed and why it is safe>
2. A short summary of the edits.
3. Final line, exactly one of:
FIX_STATUS: APPLIED
FIX_STATUS: NO_CHANGES
FIX_STATUS: CANNOT_FIX
"""


def changed_files(wt: Path) -> tuple[list[str], int]:
    rv.run(["git", "add", "-A", "--", ".", ":(exclude).reviews", ":(exclude).reviews-tmp"], cwd=wt, timeout=60)
    names = [x for x in rv.run(["git", "diff", "--cached", "--name-only"], cwd=wt, timeout=60).stdout.splitlines() if x]
    lines = 0
    for row in rv.run(["git", "diff", "--cached", "--numstat"], cwd=wt, timeout=60).stdout.splitlines():
        a, d = (row.split("\t") + ["0", "0"])[:2]
        lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return names, lines


def run_format(wt: Path, cfg: dict[str, Any], files: list[str]) -> list[str]:
    cmds = cfg.get("format_command")
    if not cmds or not files:
        return []
    if isinstance(cmds[0], str):
        cmds = [cmds]
    live = [f for f in files if (wt / f).is_file()]
    notes = []
    for c in cmds:
        if "{files}" in c and not live:
            continue
        argv = [x for a in c for x in (live if a == "{files}" else [a])]
        try:
            cp = subprocess.run(argv, cwd=wt, env=suite_env(), capture_output=True, text=True, timeout=600)
            notes.append(f"{' '.join(c[:4])}: exit {cp.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            notes.append(f"{' '.join(c[:4])}: {e.__class__.__name__}")
    return notes


# --- PR / labels -----------------------------------------------------------------------
def open_test_refactor_prs(repo: str) -> list[dict[str, Any]] | None:
    cp = rv.run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
                 "--json", "number,title,url,headRefName,labels,createdAt"], timeout=120)
    if cp.returncode != 0:
        return None
    out = []
    for pr in json.loads(cp.stdout or "[]"):
        labels = {(l.get("name") or "").lower() for l in pr.get("labels") or [] if isinstance(l, dict)}
        if LABEL in labels or (pr.get("headRefName") or "").startswith(BRANCH_PREFIX):
            out.append(pr)
    return out


def ensure_label(repo: str) -> str:
    cp = rv.run(["gh", "api", "-X", "POST", f"repos/{repo}/labels", "-f", f"name={LABEL}", "-f", f"color={LABEL_COLOR}",
                 "-f", f"description={LABEL_DESC}"], timeout=60)
    if cp.returncode == 0:
        return "created"
    blob = (cp.stdout or "") + (cp.stderr or "")
    if "already_exists" in blob or "already exists" in blob.lower():
        return "exists"
    return f"failed ({rv._one_line(blob, 160)})"


def record_path(repo: str, number: int) -> Path:
    return state_dir(repo) / f"TESTREFACTOR-PR-{number}.json"


def pr_body(repo: str, branch: str, base_sha: str, cands: list[dict[str, Any]], decisions: list[dict[str, str]],
            metrics: dict[str, Any], before: dict[str, Any], after: dict[str, Any], ci: dict[str, Any],
            hid: str, model: str, summary: str, uniq: dict[str, dict[str, Any]]) -> str:
    dec = {d["file"]: d for d in decisions}
    rows = ["| Test file | Action | Local before | Local after | CI median | Tests | Unique coverage | Why |",
            "|---|---|---|---|---|---|---|---|"]
    for c in cands:
        d = dec.get(c["file"]) or {"action": "KEEP", "why": "(no decision line)"}
        b = (before.get("files") or {}).get(c["file"])
        a = (after.get("files") or {}).get(c["file"])
        nb = (before.get("per_file") or {}).get(c["file"], c["tests"])
        na = (after.get("per_file") or {}).get(c["file"], 0)
        u = uniq.get(c["file"])
        us = "-" if not u else f"{u.get('unique')} {u.get('unit') or 'bytes'}"
        after_s = _fmt_s(a) if a is not None else ("deleted" if d["action"] == "DELETE" else "-")
        rows.append(f"| `{c['file']}` | {d['action']} | {_fmt_s(b)} | {after_s} | {_fmt_s(c['ci_s'])} | {nb} -> {na} "
                    f"| {us} | {rv.scrub_pr_title(d['why'], '-').replace('|', '/')[:300]} |")
    cb, ca = metrics.get("cov_before"), metrics.get("cov_after")
    cov = (f"{cb:.2f}% -> {ca:.2f}% (V8 byte coverage of non-test code, or pytest-cov line %)"
           if cb is not None and ca is not None else "not measured")
    ci_lines = "\n".join(f"- {j['workflow']} / {j['job']}: median {_fmt_s(j['median_s'])}, p90 {_fmt_s(j['p90_s'])}"
                         + (f", trend {j['trend_pct']:+.0f}%" if j.get("trend_pct") is not None else "")
                         for j in (ci.get("jobs") or [])[:5]) or "- (no Actions data)"
    steps = "\n".join(f"- test step {s['job']} / {s['step']}: median {_fmt_s(s['median_s'])}"
                      for s in (ci.get("test_steps") or [])[:3])
    saved = metrics.get("saved_s") or 0
    # No overseer / harness / model attribution anywhere in the PR text (Joe, Nov 18, 2026); the merge-hold
    # instructions live in the local log and digest email instead.
    return f"""Weekly test-suite review: the slowest and least-impactful tests, ranked from GitHub Actions
runtimes, a timed local run and coverage, then rebuilt or deleted where safe.

## Changes

{chr(10).join(rows)}

## Guardrails (all passed)

- Suite green before and after (local run, base {base_sha[:10]})
- Local test time: {_fmt_s(metrics.get('before_s'))} -> {_fmt_s(metrics.get('after_s'))} ({'saved ' + _fmt_s(saved) if saved >= 0 else 'slower by ' + _fmt_s(-saved)})
- Tests: {metrics.get('tests_before')} -> {metrics.get('tests_after')} (drops only in DELETE files)
- Coverage: {cov}
- Only test files changed: {metrics.get('changed_files')} files, {metrics.get('changed_lines')} lines

## CI runtime context (last {ci.get('days')} days on {ci.get('branch')})

{ci_lines}
{steps}

## Harness summary

{rv.scrub_pr_body(summary)[:2500]}
"""


# --- reporting -------------------------------------------------------------------------
def _tr(x: dict[str, Any]) -> str:
    return f", trend {x['trend_pct']:+.0f}%" if x.get("trend_pct") is not None else ""


def print_ci_summary(ci: dict[str, Any]) -> None:
    _p(f"  CI: {ci.get('runs_total', 0)} completed runs on {ci.get('branch')} since {ci.get('since')}; "
       f"{ci.get('logs_parsed', 0)} run logs parsed; junit artifacts: {'yes' if ci.get('junit') else 'no'}")
    for w in (ci.get("workflows") or [])[:6]:
        _p(f"    workflow {w['name']}: {w['runs']} runs ({w['success']} green), median {_fmt_s(w['median_s'])}, "
           f"p90 {_fmt_s(w['p90_s'])}{_tr(w)}")
    for j in (ci.get("jobs") or [])[:5]:
        _p(f"    job {j['workflow']} / {j['job']}: median {_fmt_s(j['median_s'])}, p90 {_fmt_s(j['p90_s'])}, "
           f"max {_fmt_s(j['max_s'])} (n={j['n']}){_tr(j)}")
    for s in (ci.get("test_steps") or [])[:5]:
        _p(f"    test step {s['job']} / {s['step']}: median {_fmt_s(s['median_s'])}, p90 {_fmt_s(s['p90_s'])}{_tr(s)}")
    for f, d in sorted((ci.get("files") or {}).items(), key=lambda kv: -(kv[1] or 0))[:6]:
        _p(f"    CI test file {f}: {_fmt_s(d)}")
    for t, d in sorted((ci.get("tests") or {}).items(), key=lambda kv: -(kv[1] or 0))[:6]:
        _p(f"    CI slow test {t[:150]}: {_fmt_s(d)}")
    for n in ci.get("notes") or []:
        _p(f"    note: {n}")


def print_candidates(ranked: list[dict[str, Any]], n: int) -> None:
    _p(f"  Ranked candidates (cost x low impact; top {n}):")
    for i, c in enumerate(ranked[:n], 1):
        _p(f"    {i}. {c['suggested']:<7} {c['file']}  score={c['score']:.2f} cost={_fmt_s(c['cost_s'])} "
           f"(local {_fmt_s(c['local_s'])}, CI {_fmt_s(c['ci_s'])}) tests={c['tests']}"
           + ("" if c["local"] else "  [CI-only: not run locally, so not handed to the harness]"))
        if c["reasons"]:
            _p(f"         signals: {'; '.join(c['reasons'])}")
        if c["hints"]:
            _p(f"         rebuild hints: {'; '.join(c['hints'])}")
        for name, l, cc in c["slow_tests"]:
            _p(f"         slow test: {name[:110]} (local {_fmt_s(l)}, CI {_fmt_s(cc)})")


def _write_artifact(repo: str, rec: dict[str, Any], harness_out: str) -> None:
    d = state_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"TESTREFACTOR-{rv.now_utc().strftime('%Y%m%dT%H%M%SZ')}.md"
    p.write_text(f"# Test refactor: {repo}\n\n- Result: {rec.get('result')}\n- Harness: {rec.get('harness')} "
                 f"({rec.get('model')})\n- Metrics: {json.dumps(rec.get('metrics'))}\n\n## Decisions\n\n"
                 + "\n".join(f"- {x['action']} {x['file']}: {x['why']}" for x in rec.get("decisions") or [])
                 + f"\n\n## Harness output\n\n{harness_out}\n", encoding="utf-8")
    _p(f"  saved {p}")


# --- per-repo flow ---------------------------------------------------------------------
def process_repo(entry: dict[str, Any], cfg: dict[str, Any], *, mode: str, harnesses_path: Path,
                 force: bool = False) -> dict[str, Any]:
    """mode: dry-run | no-push | live. Returns a digest record."""
    repo = entry["repo"]
    repo_path = Path(entry["path"]).expanduser()
    rec: dict[str, Any] = {"repo": repo, "mode": mode, "result": None, "at": rv._now_z()}
    _p(f"\n== {repo} [test-refactor, {mode}] ==")
    if not repo_path.is_dir():
        rec["result"] = f"skipped: missing local path {repo_path}"
        _p(f"  ! {rec['result']}", err=True)
        return rec
    st = load_state(repo)
    if mode == "live" and not force:
        prs = open_test_refactor_prs(repo)
        if prs is None:
            rec["result"] = "skipped: could not list open PRs (fail closed)"
            _p(f"  ! {rec['result']}", err=True)
            return rec
        if prs:
            rec["result"] = f"skipped: last test-refactor PR still open ({', '.join(p['url'] for p in prs)})"
            rec["open_prs"] = [p["url"] for p in prs]
            _p(f"  skip: {rec['result']}")
            return rec
        last = _parse_ts(st.get("last_pr_at"))
        gap = int(cfg.get("min_days_between_prs") or 6)
        if last and rv.now_utc() - last < timedelta(days=gap):
            rec["result"] = f"skipped: a test-refactor PR was opened {last.date()} (< {gap} days ago)"
            _p(f"  skip: {rec['result']}")
            return rec
    try:
        branch = default_branch(repo)
    except (GhError, json.JSONDecodeError) as e:
        rec["result"] = f"skipped: repo metadata unavailable ({e})"
        _p(f"  ! {rec['result']}", err=True)
        return rec
    ci = gather_ci(repo, branch, cfg)
    rec["ci"] = {k: ci.get(k) for k in ("jobs", "test_steps", "workflows", "notes", "junit", "runs_total")}
    rec["ci_tests"] = ci.get("tests")
    print_ci_summary(ci)
    wt: Path | None = None
    outdir = Path(tempfile.mkdtemp(prefix="ovs-testrefactor-"))
    try:
        try:
            wt, base_sha = create_worktree(repo_path, branch)
        except RuntimeError as e:
            rec["result"] = f"failed: {e}"
            _p(f"  ! {rec['result']}", err=True)
            return rec
        _p(f"  worktree: {wt} @ origin/{branch} {base_sha[:10]}")
        suites = detect_suites(wt, cfg)
        _p(f"  suites: {', '.join(s['kind'] + ':' + (s['dir'] or '.') for s in suites) or 'none detected'}")
        if not suites:
            rec["result"] = "skipped: no test suite detected"
            return rec
        inst = install_deps(wt, suites, cfg)
        _p(f"  install: {inst}")
        if inst.startswith("FAILED"):
            rec["result"] = f"failed: dependency install {inst}"
            _p(f"  ! {rec['result']}", err=True)
            return rec
        before = measure(wt, repo_path, suites, outdir, "before", cfg)
        cov_b = coverage_pct(before)
        rec["local"] = {k: before.get(k) for k in ("ok", "test_time_s", "wall_s", "tests", "passed", "skipped",
                                                   "flaky", "still_failing", "errors")}
        rec["local"]["coverage"] = cov_b
        _p(f"  local: {'green' if before['ok'] else 'RED'}; {before['tests']} tests, test time "
           f"{_fmt_s(before['test_time_s'])} (wall {_fmt_s(before['wall_s'])}); skipped/todo {len(before['skipped'])}; "
           f"flaky files {len(before['flaky'])}; coverage {f'{cov_b:.2f}%' if cov_b is not None else 'n/a'}"
           + (f"; still failing: {', '.join(before['still_failing'][:4])}" if before["still_failing"] else ""))
        for e in before.get("errors") or []:
            _p(f"    ! suite error: {rv._one_line(e, 240)}", err=True)
        for t in before["skipped"][:8]:
            _p(f"    skipped: {t[:150]}")
        for f in before["flaky"]:
            _p(f"    flaky: {f}")
        for tid in before["failed"][:8]:
            _p(f"    failing: {tid[:150]}")
        static: dict[str, dict[str, Any]] = {}
        for f in set(before["files"]) | set(ci.get("files") or {}):
            p = wt / f
            if p.is_file():
                try:
                    static[f] = static_scan(p.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        local_tests = [t for r in before["results"] for t in r["tests"]]
        ci_files = ci.get("files") or {}
        ci_tests = ci.get("tests") or {}
        prelim = score_candidates(before["files"], local_tests, ci_files, ci_tests, static, {}, set(before["flaky"]),
                                  float(cfg.get("min_rebuild_s") or 1.0))
        targets = [c["file"] for c in prelim if c["local"]]
        uniq = unique_coverage(before, targets)
        _p(f"  unique coverage measured for {len(uniq)}/{len(targets)} test file(s)"
           + ("" if uniq else " (no usable V8/pytest-cov data: coverage-based impact unavailable)"))
        ranked = score_candidates(before["files"], local_tests, ci_files, ci_tests, static, uniq, set(before["flaky"]),
                                float(cfg.get("min_rebuild_s") or 1.0))
        top_n = int(cfg.get("top_n") or 5)
        print_candidates(ranked, top_n + 3)
        cands = pick_harness_candidates(ranked, top_n)
        rec["candidates"] = [{k2: c[k2] for k2 in ("file", "suggested", "cost_s", "local_s", "ci_s", "tests",
                                                    "reasons", "hints", "score", "local")} for c in ranked[:top_n + 3]]
        if not before["ok"]:
            rec["result"] = "skipped refactor: suite not green before the change (guardrail)"
            _p(f"  {rec['result']}")
            return rec
        if not cands:
            rec["result"] = "skipped: no REBUILD/DELETE candidates"
            _p(f"  {rec['result']}")
            return rec
        if mode == "dry-run":
            rec["result"] = (f"dry-run: would hand {len(cands)} file(s) to a deep-tier write harness: "
                             + ", ".join(f"{c['suggested']} {c['file']}" for c in cands))
            _p(f"  dry-run: no harness, nothing pushed. {rec['result']}; then guardrails; live would push "
               f"{BRANCH_PREFIX}{datetime.now().strftime('%Y%m%d')} and open a PR labeled {LABEL} (merge held)")
            return rec
        elig, why, _nf = rv.eligible_harnesses(harnesses_path, runners=rv.FIX_RUNNERS)
        if not elig:
            rec["result"] = f"deferred: no eligible harness ({why})"
            _p(f"  ! {rec['result']}", err=True)
            return rec
        rv.set_active_tier(rv.load_tiers(), "deep")
        res = rv.run_harness_chain(wt, build_prompt(repo, branch, cands, ci, cfg), harnesses_path,
                                   runners=rv.FIX_RUNNERS)
        if res is None:
            rec["result"] = "deferred: no harness produced a result"
            _p(f"  ! {rec['result']}", err=True)
            return rec
        hid, cp = res
        model = rv._model_label(hid)
        rv.set_active_tier(None, None)
        out = (cp.stdout or "").strip() or (cp.stderr or "").strip()
        shutil.rmtree(wt / ".reviews-tmp", ignore_errors=True)
        decisions = parse_decisions(out)
        rec.update({"harness": hid, "model": model, "decisions": decisions})
        for d in decisions:
            _p(f"    decision: {d['action']:<7} {d['file']}: {rv._one_line(d['why'], 160)}")
        names, _ = changed_files(wt)
        allow_ci = bool(cfg.get("allow_ci_config"))
        for n in run_format(wt, cfg, [x for x in names if is_test_path(x, allow_ci=allow_ci)]):
            _p(f"    format: {n}")
        names, nlines = changed_files(wt)
        _p(f"  harness {hid} model={model}: {len(names)} files, {nlines} lines changed")
        after = measure(wt, repo_path, suites, outdir, "after", cfg)
        cov_a = coverage_pct(after, before.get("v8_len") or None)
        ok, fails, metrics = check_guardrails(before, after, names, nlines, decisions, cfg, cov_b, cov_a, wt=wt)
        tol = float(cfg.get("coverage_tolerance_pct") if cfg.get("coverage_tolerance_pct") is not None else 0.5)
        extra = deleted_unique_failures(decisions, uniq, wt, tol)
        if extra:
            ok, fails = False, fails + extra
        rec["metrics"] = metrics
        _p(f"  guardrails: {'PASS' if ok else 'FAIL'}; local test time {_fmt_s(metrics['before_s'])} -> "
           f"{_fmt_s(metrics['after_s'])}; tests {metrics['tests_before']} -> {metrics['tests_after']}; coverage "
           f"{'n/a' if cov_b is None or cov_a is None else f'{cov_b:.2f}% -> {cov_a:.2f}%'}")
        for f in fails:
            _p(f"    ! guardrail: {f}", err=True)
        if not ok:
            rec["result"] = "discarded: " + "; ".join(fails)
            _write_artifact(repo, rec, out)
            return rec
        n_re = sum(1 for d in decisions if d["action"] == "REBUILD")
        n_del = sum(1 for d in decisions if d["action"] == "DELETE")
        subject = f"test: weekly test-suite refactor (rebuild {n_re}, delete {n_del})"
        summary = re.sub(r"^\W*FIX_STATUS:.*$", "", out, flags=re.M).replace("\u2014", "-").strip()
        msg = (f"{subject}\n\nWeekly test-suite review. Local test time "
               f"{_fmt_s(metrics['before_s'])} -> {_fmt_s(metrics['after_s'])}; tests {metrics['tests_before']} -> "
               f"{metrics['tests_after']}.\n\n"
               + "\n".join(f"- {d['action'].lower()} {d['file']}: {rv.scrub_pr_title(d['why'], '-')[:200]}"
                            for d in decisions)
               + f"\n\n{rv.FIX_TRAILER}: {hid}\nCo-authored-by: {rv._FIX_COAUTHOR.get(hid, hid)}\n").replace("\u2014", "-")
        rv.run(["git", "add", "-A", "--", ".", ":(exclude).reviews", ":(exclude).reviews-tmp"], cwd=wt, timeout=60)
        cm = rv.run(["git", "commit", "-q", "-F", "-"], cwd=wt, input_text=msg, timeout=1800)
        if cm.returncode != 0:
            rec["result"] = f"discarded: git commit failed ({rv._one_line(cm.stderr or cm.stdout, 300)})"
            _p(f"  ! {rec['result']}", err=True)
            _write_artifact(repo, rec, out)
            return rec
        new_sha = rv.run(["git", "rev-parse", "HEAD"], cwd=wt, timeout=30).stdout.strip()
        new_branch = f"{BRANCH_PREFIX}{datetime.now().strftime('%Y%m%d')}"
        _p(f"  committed {new_sha[:10]} on {base_sha[:10]}")
        if mode != "live":
            rec["result"] = f"no-push: would push {new_sha[:10]} -> {new_branch} and open a {LABEL} PR (merge held)"
            _p(f"  {rec['result']}")
            _write_artifact(repo, rec, out)
            return rec
        ps = rv.run(["git", "push", "origin", f"HEAD:refs/heads/{new_branch}"], cwd=wt, timeout=600)
        if ps.returncode != 0:
            rec["result"] = f"failed: git push ({rv._one_line(ps.stderr, 300)})"
            _p(f"  ! {rec['result']}", err=True)
            return rec
        bf = outdir / "pr-body.md"
        bf.write_text(rv.scrub_pr_body(pr_body(repo, branch, base_sha, cands, decisions, metrics, before, after, ci,
                                               hid, model, summary, uniq)), encoding="utf-8")
        # Always through the guard: title/body are scrubbed of overseer/harness mentions.
        pc = rv.gh_pr_create(repo, base=branch, head=new_branch, title=subject,
                             body=bf.read_text(encoding="utf-8"))
        if pc.returncode != 0:
            rec["result"] = (f"failed: gh pr create ({rv._one_line(pc.stderr or pc.stdout, 300)}); "
                             f"branch {new_branch} was pushed")
            _p(f"  ! {rec['result']}", err=True)
            return rec
        url = ((pc.stdout or "").strip().splitlines() or [""])[-1]
        m = re.search(r"/pull/(\d+)", url)
        number = int(m.group(1)) if m else None
        rp = {"repo": repo, "number": number, "url": url, "branch": new_branch, "head": new_sha, "harness": hid,
              "model": model, "decisions": decisions, "metrics": metrics, "merge_held": True, "at": rv._now_z()}
        if number is not None:
            # Hold record first: from here on the self_review loop never auto-merges this PR.
            rpath = record_path(repo, number)
            rpath.parent.mkdir(parents=True, exist_ok=True)
            rpath.write_text(json.dumps(rp, indent=2), encoding="utf-8")
            lab = ensure_label(repo)
            ap = rv.run(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={LABEL}"],
                        timeout=120)
            rp["label"] = "added" if ap.returncode == 0 else f"failed ({rv._one_line(ap.stderr, 160)}; create: {lab})"
            rpath.write_text(json.dumps(rp, indent=2), encoding="utf-8")
        st["last_pr_at"], st["last_pr"] = rv._now_z(), url
        rec.update({"result": f"PR opened {url}", "pr": url})
        _p(f"  github: opened {url} (label {rp.get('label')}; merge held until --release-human-review)")
        _write_artifact(repo, rec, out)
        return rec
    finally:
        rv.set_active_tier(None, None)
        if wt is not None:
            _p(f"  removing worktree {wt}")
            rv.remove_worktree(repo_path, wt)
        shutil.rmtree(outdir, ignore_errors=True)
        if mode != "dry-run":
            st.setdefault("runs", []).append({"at": rec.get("at"), "mode": mode, "result": rec.get("result")})
            save_state(repo, st)


def digest_text(recs: list[dict[str, Any]], mode: str) -> str:
    when = datetime.now().astimezone().strftime("%a %b %d %Y %H:%M %Z")
    out = [f"Weekly test-suite review ({mode}), {when}", ""]
    for r in recs:
        out.append(f"== {r['repo']} ==")
        out.append(f"Result: {r.get('result')}")
        ci = r.get("ci") or {}
        for j in (ci.get("jobs") or [])[:3]:
            out.append(f"  slow CI job: {j['workflow']} / {j['job']} median {_fmt_s(j['median_s'])}, "
                       f"p90 {_fmt_s(j['p90_s'])}{_tr(j)}")
        for t, d in sorted((r.get("ci_tests") or {}).items(), key=lambda kv: -(kv[1] or 0))[:3]:
            out.append(f"  slow CI test: {t[:120]} {_fmt_s(d)}")
        loc = r.get("local") or {}
        if loc:
            cov = loc.get("coverage")
            out.append(f"  local: {'green' if loc.get('ok') else 'RED'}, {loc.get('tests')} tests, "
                       f"{_fmt_s(loc.get('test_time_s'))} test time, coverage "
                       f"{f'{cov:.2f}%' if cov is not None else 'n/a'}; skipped {len(loc.get('skipped') or [])}; "
                       f"flaky: {', '.join(loc.get('flaky') or []) or 'none'}")
        for c in (r.get("candidates") or [])[:5]:
            out.append(f"  candidate: {c['suggested']} {c['file']} (cost {_fmt_s(c['cost_s'])}; "
                       f"{'; '.join(c['reasons'][:2]) or 'slow'})")
        m = r.get("metrics")
        if m:
            cov = (f"{m['cov_before']:.2f}% -> {m['cov_after']:.2f}%" if m.get("cov_before") is not None
                   and m.get("cov_after") is not None else "n/a")
            out.append(f"  change: test time {_fmt_s(m['before_s'])} -> {_fmt_s(m['after_s'])}, tests "
                       f"{m['tests_before']} -> {m['tests_after']}, coverage {cov}")
        if r.get("pr"):
            out.append(f"  PR (merge held until --release-human-review): {r['pr']}")
        out.append("")
    out.append("Release a held test-refactor PR: python3 review.py --release-human-review --repo OWNER/NAME --pr N")
    return "\n".join(out) + "\n"


def run_test_refactor(args: Any, entries: list[dict[str, Any]], repos_cfg: dict[str, Any], harnesses_path: Path,
                      module: Any) -> int:
    bind(module)
    mode = "live" if args.live else ("no-push" if args.no_push else "dry-run")
    targets = [(e, repo_config(e, repos_cfg)) for e in entries]
    targets = [(e, c) for e, c in targets if c]
    _p(f"Test-suite review/refactor: {mode.upper()}"
       + (" (no harness, no push, no PR, no email)" if mode == "dry-run" else "")
       + (" (harness + guardrails + local commit; nothing pushed)" if mode == "no-push" else "")
       + f"; repos: {', '.join(e['repo'] for e, _ in targets) or 'none opted in (repos.json test_refactor)'}")
    recs = []
    for e, c in targets:
        try:
            recs.append(process_repo(e, c, mode=mode, harnesses_path=harnesses_path, force=bool(args.force)))
        except Exception as ex:  # noqa: BLE001 - one repo never kills the run
            _p(f"  ! test-refactor error for {e['repo']}: {ex!r}", err=True)
            recs.append({"repo": e["repo"], "result": f"error: {ex!r}"})
    digest = digest_text(recs, mode)
    opened = [r["pr"] for r in recs if r.get("pr")]
    if mode == "live" and recs:
        hr = rv.human_review_config(rv.load_tiers())
        status = rv.send_human_review_email(hr, f"[overseer] Weekly test-suite review: {len(opened)} PR(s) opened",
                                            digest)
        _p(f"\nDigest email: {status}")
    else:
        _p(f"\n--- digest (not emailed in {mode}) ---\n{digest}")
    if opened:
        _p("Test-refactor PRs opened:")
        for u in opened:
            _p(f"  {u}")
    _p("Done (test-refactor).")
    return 0
