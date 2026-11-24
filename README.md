# PR overseer — everything lives here: `~/workspace/ai/pr-overseer`

| What | Where |
|---|---|
| Script | `~/workspace/ai/pr-overseer/review.py` (also on PATH as `pr-overseer-review` → `~/bin/pr-overseer-review` symlink) |
| Repos to watch | `~/workspace/ai/pr-overseer/repos.json` |
| Harness priority/cooldowns | `~/workspace/ai/pr-overseer/harnesses.json` |
| Model tiers + classifier gate | `~/workspace/ai/pr-overseer/tiers.json` (see "Model tiers" below) |
| Stub AGENTS/CLAUDE/GEMINI/REVIEW.md | `~/workspace/ai/pr-overseer/stubs/` (written into throwaway PR worktrees only) |
| Review mirrors | `~/workspace/ai/pr-overseer/reviews/<owner>__<repo>/PR-<N>.md` (Dependabot triage: `DEPENDABOT-PR-<N>.md`) |
| Logs | `~/workspace/ai/pr-overseer/logs/` — cron output in `logs/cron.log`; per-run logs via `--log logs/<name>.log` |
| Cron | `crontab -e` (weekdays every 15 min 5am–9pm, plus 10pm) |
| Weekly test-suite refactor | `testrefactor.py` via `pr-overseer-review --test-refactor` (cron Sunday 9pm `test-refactor-run.sh`, log `logs/test-refactor.log`; see "Weekly test-suite review" below) |

Run: `pr-overseer-review` (or `cd ~/workspace/ai/pr-overseer && ./review.py`); `--dry-run` to list only. Dependabot: `pr-overseer-review --dependabot` (dry-run unless `--live`).
PR worktrees are temporary, under each clone's sibling `.reviews-wt/`.

---

# Local PR overseer

Reviews open PRs across repos listed in `repos.json`: in `review` mode **only PRs where you are a requested reviewer** (`review-requested:@me`); in `self_review` mode **all open non-draft PRs authored by `github_login`** (no review request needed) **plus PRs that request your review**. Dependabot PRs are skipped here; they get a separate daily `--dependabot` triage (see below).
Uses Muse → Antigravity (`agy`, Google's Gemini agent CLI) → Cursor Agent → Claude Code (priority in `harnesses.json`), with
cooldown + failover on quota-like errors.

**Models now come from `tiers.json` per PR tier (see "Model tiers" below).** Fallback pins when a tier has no entry / tiers.json is missing: Muse → `muse-wonder-1.0` (`MUSE_REVIEW_MODEL`), Antigravity → `gemini-4-hydrogen-medium` (`ANTIGRAVITY_MODEL`, Gemini 4 Hydrogen, pinned Nov 13, 2026). Cursor → `grok-4.7-high`, Claude → `claude-opus-5-5` (`_FALLBACK_MODELS`).

**Antigravity flags** (prompt must be the argument right after `-p`, stdin is ignored, so a prompt-file pointer is used like Muse):
- review: `agy -p <pointer> --model gemini-4-hydrogen-medium --mode plan --sandbox --settings <session-only JSON> --print-timeout 870s --output-format text`. The session-only settings allow only read-only git/text inspection commands (`_AGY_READONLY_CMDS`: `git diff/log/show/status/grep/blame/ls-files/rev-parse/cat-file/merge-base/rev-list/ls-tree/merge-tree/for-each-ref/…`, `cat`, `head`, `tail`, `ls`, `grep`, `rg`, `wc`, `find`, `sed -n`, `tree`, `file`, `stat`, `diff`, `cut`, `tr`, `jq`, `sort`, `uniq`, `nl`, etc.) and deny `write_file(*)` and `edit_file(*)`; no `--dangerously-skip-permissions`, so anything else is auto-denied in headless mode. Don't add `--disable-slash-commands` (agy then ignores `--mode`).
- fix: same but `--mode accept-edits` (file edits auto-approved; tests/builds/other commands denied).
- agy exits 0 with empty stdout when a denied tool kills the turn (`jetski: no output produced …`); `_agy_normalize()` turns that (and any permission-denial abort) into exit 1 + empty stdout so the chain cleanly fails over to the next harness (the fix runner also resets the throwaway worktree first). When `gh pr diff` fails on oversized PRs (>20k lines), `fetch_pr_diff()` falls back to a local `git diff --stat` + `git diff <base>...HEAD` inside the worktree so harnesses receive the diff directly.

- Writes `<clone>/.reviews/PR-<N>.md` and mirrors under `reviews/`.
- Then posts that review on GitHub from the harness `VERDICT` line:
  **APPROVE** (with feedback body) if no blockers, or **REQUEST_CHANGES** if any blockers.
- Merging is per repo via `review_mode` in `repos.json` (see below). Default is never merge.
- Writes missing `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` / `REVIEW.md` stubs into the **throwaway PR worktree only**
  (review, fix and Dependabot paths); the live main clone is never touched (changed Nov 13, 2026). Rules:
  - no guide at all → stub `AGENTS.md`, `CLAUDE.md` (`@AGENTS.md`), `GEMINI.md` (`@AGENTS.md`);
  - real `CLAUDE.md` but no `AGENTS.md` → **no** stub `AGENTS.md` (Muse reads `AGENTS.md` first and only falls back
    to `CLAUDE.md` when it's absent, so a stub would hide the real guide); `GEMINI.md` becomes a `@CLAUDE.md` shim for agy;
  - `AGENTS.md` present → only missing `CLAUDE.md` / `GEMINI.md` pointers are added; `REVIEW.md` stub whenever missing.
  - The fix path stages with `git add -A` plus `:(exclude,top)<stub>` pathspecs for every stub it created, then unstages
    any that slipped through, so stubs never reach a fix commit. The agy fix-failover `git clean` keeps them too.
- Prompt context (`read_context`): root `AGENTS.md`, `REVIEW.md`, `CLAUDE.md`, `GEMINI.md` (deduped, so a
  `CLAUDE.md → AGENTS.md` symlink is read once), plus the nearest nested `AGENTS.md` / `CLAUDE.md` above each file in
  the PR diff (monorepo packages). Pure `@AGENTS.md` pointers, overseer stubs and symlinks escaping the worktree are
  skipped; nested guides are capped at 8 files / 24k chars total.
- Checks out each PR head into `../.reviews-wt/PR-<N>` (explicit `git worktree add`),
  runs the harness read-only there (no Claude/Cursor/Muse/Antigravity `-w` flags), then removes the worktree.

## Usage

```bash
# List matching PRs only
pr-overseer-review --dry-run

# Review all matching review-requested PRs
pr-overseer-review

# One repo / one PR
pr-overseer-review --repo your-org/your-repo --pr 300

# One specific PR by URL, whatever its state (see "Review one specific PR by URL")
pr-overseer-review https://github.com/your-org/your-repo/pull/300
```

Edit `repos.json`, `harnesses.json`, and `tiers.json` anytime.

## Config files (gitignored)

`repos.json`, `harnesses.json`, and `tiers.json` are not committed, so the local repo list, GitHub login, and harness cooldowns never enter git history. Copy the examples to the repo root (drop the `.example` suffix) and edit them:

```bash
cp examples/repos.example.json repos.json
cp examples/harnesses.example.json harnesses.json
cp examples/tiers.example.json tiers.json
```

## Model tiers + classifier gate (`tiers.json`, added 2026-11-13)
Every PR the overseer is about to review (after the Dependabot skip, the already-reviewed skip, the no-harness
pre-check and the CI-pending defer) gets a tier, and each harness then runs with that tier's `--model`:

| Harness | everyday | deep |
|---|---|---|
| muse | `muse-spark-1.3` | `muse-wonder-1.0` |
| antigravity | `gemini-4-hydrogen-medium` | `gemini-4-neon-medium` |
| cursor | `grok-4.7-high` | `grok-4.7-high` |
| claude | `claude-sonnet-5-5` | `claude-opus-5-5` |

(All ids smoke-tested 2026-11-13 in the same read-only mode the review runner uses. `muse-ember-1.0` still fails
`--disable-write` runs, so it is not in a tier.) Harness order/failover/cooldowns are unchanged; only the model differs.

**Reasoning level (2026-11-18): everything runs at `high`** (not xhigh/max) **except Antigravity, which runs at `medium` (2026-11-24)**, including the classifier, fix passes, test-refactor and
Dependabot triage (they reuse these runners):

| Harness | How the level is set |
|---|---|
| muse | `--reasoning-effort high` on every `muse exec` (`MUSE_EFFORT`; Muse's default is already high) |
| antigravity | `-medium` model ids (`gemini-4-hydrogen-medium`, `gemini-4-neon-medium`), classifier included |
| cursor | `-high` model id (`grok-4.7-high`) |
| claude | `--effort high` on every `claude -p` (`CLAUDE_EFFORT`); ids stay `claude-sonnet-5-5` / `claude-opus-5-5` |

How the tier is picked (first match wins):
1. **Per-repo override**: `"tier": "everyday"|"deep"` on a `repos.json` entry (none set today).
2. **Deterministic rules** (`tiers.json` → `rules`): deep labels (`security`, `breaking`, …); any file matching
   `deep_globs` (auth/permissions/secrets, `.github/workflows`, infra/terraform/k8s/helm,
   Dockerfiles, schema/prisma/models.py, payment/billing/stripe/invoice); a dependency-manifest **major** version bump
   (package.json, pyproject, requirements, Cargo, go.mod, Gemfile); ≥ 800 changed lines or ≥ 30 files → **deep**.
   Otherwise everyday labels, docs/config-only files (`everyday_globs`, ≤ 600 lines) or a small diff
   (≤ 40 lines and ≤ 3 files) → **everyday**.
   **Migrations are tier 1** (since 2026-11-13, "migrations are safer"): migration/alembic/migrate paths, `*.sql`
   and the `migration` label are *not* deep rules. A migration PR goes through the normal flow: everyday if
   small, otherwise the classifier, whose prompt says a routine migration alone is not a reason for deep (it can
   still pick deep for a clearly destructive one, e.g. dropping data live code still reads). Schema files
   (`**/schema*`, `*.prisma`, `models.py`) are still deep.
3. **Classifier model** for everything in between: `agy` read-only (`--mode plan --sandbox`, same read-only settings
   as reviews) with `gemini-4-hydrogen-medium`, in a throwaway empty dir, given title, body, labels, file list with +/-
   and the first 12k chars of the diff. It must answer `TIER: everyday|deep` + `REASON: …`.
   *Potential drop-in:* an open-source `qwen3-coder` model (e.g., via local Ollama or an OpenAI-compatible endpoint)
   fits well here — the classifier requires no tool calls, runs on bounded inline context (≤12k diff / ≤2k body),
   returns a strict two-line `TIER:` / `REASON:` response, and fails closed to `deep` on any error without spending
   `agy` quota.
4. **Any failure** (timeout, quota — which also cools agy down —, agy cooling down, unparseable answer, PR stats
   unreadable) → `default_tier_on_failure` = **deep**. A classifier problem never weakens a review.

Rules/LLM results are cached per head SHA in `reviews/<owner>__<repo>/TIER-PR-<N>.json`, so the auto-fix of a PR
and retries of the same head reuse the tier (no second classifier call); new commits re-classify. Fallbacks aren't
cached. Each entry stores a `rules_fp` fingerprint of the rules, classifier model and prompt version
(`CLASSIFIER_PROMPT_VERSION`). A mismatch, or a pre-fingerprint entry, re-classifies, so rule edits apply to PRs
already classified. Fixes use the same tier's models. Dependabot triage uses `dependabot_tier` (everyday) with no classifier call.
`--dry-run` shows the rules result only (`tier (dry-run): …`, or `llm-pending` when it would ask the classifier).

Log line per reviewed PR (no leading `!`, so idle cron ticks stay quiet):
`    tier=deep source=rules reason=.github/workflows/deploy.yml matches .github/workflows/**`, then
`    harness: muse model=muse-wonder-1.0 …`. The review artifact header records `model=` and `tier=`; the posted
GitHub review/comment body is unchanged (there was no attribution footer to extend).

Turn off: `"enabled": false` in tiers.json (pre-tiers pins), or `classifier.enabled: false` (rules, then
`default_tier`). Missing/invalid tiers.json → `warning:` line and pre-tiers behavior. Tests: `python3 test_tiers.py`.

## Needs-human-review ping (`tiers.json` → `human_review`, added 2026-11-13)

For deep PRs that are **really risky or really large**, the normal frontier (deep-tier) review still runs and
is posted as usual. In addition, right after the review is posted (and before any self_review merge), the
overseer pings you **once per PR** through `human_review.channels` (default `["email"]`, email-only since
2026-11-13):

- `"email"` (default): emails `email_to` the rule that fired, size, verdict and the PR link. When the merge is
  held, the subject starts `[overseer] Merge held, needs human review:` and the body gives the release command.
- `"label"` (opt-in): adds the `needs-human-review` label (created if missing; added via the REST labels API,
  because `gh pr edit` fails on the Projects-classic GraphQL deprecation).
- `"comment"` (opt-in): one PR comment starting `**Overseer: needs human review**` that @mentions `mention`.

Labels and comments already on GitHub from earlier runs are left alone.

**Trigger:** tier is `deep` **and** at least one of `human_review.rules` fires (evaluated independently of
the tier rules' first-hit order):

| rule | fires when |
|---|---|
| `labels` | a PR label in `human_review.labels` (default `security`, `breaking`, `breaking-change`; not `deep-review` or `migration`) |
| `globs` | any file matches `rules.deep_globs` (auth/secrets/crypto/permissions, `.github/workflows`, infra/terraform/k8s/helm/Dockerfile/docker-compose, schema/prisma/`models.py`, payment/billing/stripe/invoice); override with `human_review.globs` |
| `major_bump` | a dependency manifest major-version bump |
| `size` | `>= min_lines` (1500) changed lines **or** `>= min_files` (50) files |

Migrations alone never flag (no migration path, `*.sql` or `migration` label trigger since 2026-11-13). A
migration PR still flags if it also hits another trigger, e.g. it is very large.

Never flagged: deep from the LLM classifier or a classifier failure, the 800-line / 30-file deep rule alone,
`everyday` PRs (incl. a repos.json `tier: everyday` override), Dependabot, the address-feedback fix path, and
`--dry-run` (it prints `human-review (dry-run): WOULD FLAG → …` / `no (…)` and touches nothing).

**Dedup:** skipped if `reviews/<owner>__<repo>/HUMAN-PR-<N>.json` exists. An existing `needs-human-review`
label or a prior marker comment by `github_login` (from the label/comment era) also counts as already flagged:
a record is written, no new ping. The record stores what happened (`label`/`comment`: `off (channels)` by
default; `email`: `sent …` / `skipped …` / `failed …`; `merge_held`). The marker prefix is deliberately not
`**Overseer self-review`, so the already-reviewed gate and the self_review verdict parser ignore it.

**Failures never block the review:** ping errors are logged (`! human-review email: failed …`) and the
review/merge flow continues. If the GitHub already-flagged check fails, the email is still sent (the local
record prevents a repeat); with the opt-in comment channel the comment is skipped to avoid a duplicate.

**Email** is a pluggable step chosen by `human_review.email_transport`. Empty `email_to` → logs `skipped`
and no-ops. Every outcome (`sent …` / `skipped (…)` / `failed (…)`) is logged and recorded in the HUMAN record,
and it never blocks the review.

- `"smtp"` (jc-system76 default since 2026-11-13): built-in `smtplib` sender. Connects to `smtp_host:smtp_port`
  (default `smtp.gmail.com:587`) with STARTTLS and logs in as `smtp_user` (default `email_to`). The password is
  read **at send time** from `password_file` (default `~/.config/pr-overseer/gmail-app-password`). The file is
  refused (logged, email skipped) unless it is owned by you with mode `600` or tighter. All whitespace is
  stripped, so Google's `abcd efgh ijkl mnop` grouping is fine. The password is never logged or printed. For
  Gmail, use an app password (https://myaccount.google.com/apppasswords; needs 2-Step Verification), not your
  account password.
- `"sendmail"`: an RFC 822 message is piped to `email_command` (default `["/usr/sbin/sendmail", "-t", "-oi"]`).
  On jc-system76 postfix is installed but unconfigured (no `/etc/postfix/main.cf`; sendmail exits 75), which
  is why the smtp transport exists.

One-time setup on jc-system76 (`~/.config/pr-overseer` already exists, mode 700):

```bash
( umask 077; read -rsp 'App password: ' pw; echo; printf '%s\n' "$pw" > ~/.config/pr-overseer/gmail-app-password; unset pw )
chmod 600 ~/.config/pr-overseer/gmail-app-password
cd ~/workspace/ai/pr-overseer && python3 review.py --test-email
```

`--test-email` sends ONE test email to `email_to` through the configured transport and prints `OK: sent …` or
`FAILED: …` (exit 0/1). It takes no lock and runs no reviews. If the password file is missing or too open, it
prints these setup steps instead of sending.

**Merge hold (`human_review.hold_merge`, default `true`), based on the local HUMAN record:** in `self_review`
repos a flagged PR is **not** auto-merged, even on APPROVE with green CI. The log says
`merge held: needs-human-review (<why>); release: … then …`. The hold applies when:

- the PR was flagged (or found already flagged on GitHub) during this review;
- a `HUMAN-PR-<N>.json` record exists without `cleared_at`;
- legacy/manual: no record, but the `needs-human-review` label is on the PR;
- the record or GitHub labels can't be read when they're needed (fail closed).

**To release** (in `~/workspace/ai/pr-overseer`):

```bash
python3 review.py --release-human-review --repo <owner/repo> --pr <N>   # sets cleared_at; GitHub untouched
python3 review.py --repo <owner/repo> --pr <N> --force                  # re-review → merges through the CI gate
```

Or merge it yourself on GitHub. `--release-human-review` creates a record if none exists (e.g. a PR labeled by
hand). Once released, a label still on the PR no longer holds the merge.
**Legacy path:** for a PR whose record says the label was `added`/`present`, removing the label on GitHub
also releases it (the next `--force` re-review stamps `cleared_at` and merges).
A held PR is not retried by cron: it already has its self-review comment, so it waits for you.
`--dry-run` shows `[merge held: needs-human-review (…)]` in the merge plan and `WOULD FLAG → ping + merge held
[channels: email]`, and never writes records.
`hold_merge: false` → flagged PRs merge as before (ping only).

Note (comment channel only): the comment is posted with the overseer's gh login. If that is the same account as `mention`, GitHub
doesn't notify you of your own @mention. The label and email are the real pings in that case.

Turn off: `"human_review": {"enabled": false}`. Missing block → the code defaults above (enabled, no email).
Tests: `python3 test_human_review.py`.

### Soft approval for flagged PRs (added 2026-11-18)

When a PR is flagged for human review (the trigger above; evaluated before the review is posted, independent of the
once-per-PR marker, so `--force` re-reviews stay soft) and the verdict is **APPROVE**, the overseer does **not** submit a
formal GitHub approval. Instead it posts a **COMMENT-type review** starting
`**Overseer soft approval — VERDICT: APPROVE (pending human review)**` that says a human reviewer must give the actual
approval. `self_review` own PRs already get a comment; they keep the `**Overseer self-review — VERDICT: APPROVE**` header
(the duplicate-review skip and the merge gate parse it) plus a one-line soft-approval note, and the merge stays held
(`hold_merge`). REQUEST_CHANGES, unflagged PRs and Dependabot PRs behave as before. A soft approval is a COMMENTED review by
`github_login`, so the already-reviewed gate skips the PR like any other reviewed PR. `--dry-run` says
`an APPROVE verdict would be a SOFT approval`. Code: `soft_approval_reason()`, `submit_github_review(soft_why=…)`,
`post_review_comment(soft_why=…)`.

### No self-mentions in PR titles / descriptions (added 2026-11-18)

The overseer never mentions itself (or harness/model/bot attribution) in a PR title or description:

- **Prompts:** `NO_SELF_MENTION_RULE` is part of the address-feedback, Dependabot-fix and test-refactor harness prompts.
- **Deterministic guard:** every `gh pr create` / `gh pr edit` goes through `gh_pr_create()` / `gh_pr_edit()` in `review.py`,
  which run `sanitize_pr_text()` first (`scrub_pr_body` drops lines naming "overseer" or carrying "generated/automated by …"
  and tool co-author lines; `scrub_pr_title` cuts mentions out of a single line). A test fails if a raw `"pr", "create"` /
  `"pr", "edit"` call appears anywhere else.
- **Weekly test-refactor PRs** no longer carry the `**Overseer test-refactor**` marker, the harness/model line or the
  merge-hold instructions (those stay in the local log/digest email). Fix-commit messages lose the "Automated by the PR
  overseer" prose; the `Overseer-Fix-By:` / `Co-authored-by:` trailers stay because latest-commit harness detection reads them.
- **Not changed:** review *comment* headers (`**Overseer self-review …`, `**Overseer dependabot …`, `**Overseer: needs human review**`)
  are comments, not descriptions, and the duplicate-review skip depends on them. The branch name `overseer/test-refactor-*`
  and the `test-refactor` label description are also untouched (detection / existing label).

Tests: `python3 test_selfmention_softapproval.py`.

## Review one specific PR by URL (added 2026-11-18)

```
pr-overseer-review https://github.com/OWNER/REPO/pull/123            # positional
pr-overseer-review --url https://github.com/OWNER/REPO/pull/123/files # --url works too
pr-overseer-review OWNER/REPO#123                                     # shorthand
pr-overseer-review --dry-run <url>                                    # preview, changes nothing
pr-overseer-review --no-wait <url>                                    # exit if cron holds the lock
```

A trailing `/files`, `/commits/<sha>`, `#issuecomment-…` or `?query` is fine. Non-github hosts and malformed refs are rejected
with a clear error (exit 2). The repo name is matched case-insensitively against `repos.json`.

It runs a **fresh review of that PR now, whatever its state**, and posts it the way the repo's `review_mode` dictates (a
self_review repo's own PR: comment + the normal merge policy, including soft approval / human-review hold / `merge_on_approve`;
anything else: a normal review, never a merge). Labels are set as usual.

| | `--repo R --pr N --force` | `<url>` (or `--url`, `OWNER/REPO#N`) |
|---|---|---|
| already-reviewed / fix-cap / fix-state skips | bypassed | bypassed |
| review-requested / authorship filters, drafts | bypassed (drafts allowed) | bypassed (drafts allowed) |
| CI still running on a would-merge PR | **defers** to the next tick | reviews anyway; the merge still needs green CI (blocked otherwise) |
| cached tier for this head SHA | used | **ignored** (fresh classification) |
| Dependabot PR | skipped | reviewed, **never merged** by this path |
| closed / merged PR | reviewed | **refused** (exit 1) |
| repo not in `repos.json` | error | cloned with `gh repo clone` into `~/.cache/pr-overseer/clones/<owner>__<repo>` and reviewed in `review` mode (never merges); `--dry-run` only says it would clone |
| another run holds the lock | exits (0) | waits up to 20 min, printing a message (`--no-wait` exits with 1; `--lock-wait N` overrides) |
| exit code | 0 | 1 if no review was produced (closed PR, no harness available, lock timeout, review failed) |

Unchanged: harness order, cooldown failover and the "never review with the model that made the latest commit" rule still apply
(so a URL run can still end with `no harness available`). A URL can't be combined with `--dependabot`, `--test-refactor`,
`--sync-labels`, `--address-feedback` or `--release-human-review`.

## PR state labels (added 2026-11-18)

Each PR carries at most one of these labels, so its basic state is visible on GitHub. The names are neutral (nothing
mentions the overseer, in line with the no-self-mention rule), share the `review: ` prefix and are created on demand
(`gh label create --force`, once per repo per run).

| Label | Set when |
|---|---|
| `review: in progress` | a review has started (after the CI-pending defer and the worktree setup). If the review is deferred or fails, it is removed and the previous state label is put back |
| `review: approved` | the review verdict is APPROVE and nothing is flagged or held |
| `review: changes requested` | the review verdict is REQUEST_CHANGES |
| `review: needs human` | the APPROVE was a soft approval (flagged for human review), or the merge is held (needs-human-review / test-refactor hold) |
| `review: fixing` | the address-feedback fix was pushed; the next tick re-reviews it |
| `review: fix limit reached` | automatic fixing gave up (`max_fix_attempts` used, or the fix said NO_CHANGES / CANNOT_FIX) |

Setting a state removes the other `review: ` labels (add first, then remove) and never touches other labels. Labels use the REST
API (`gh api repos/R/issues/N/labels`). Any label failure is a warning (`! labels: …`) and never blocks a review, fix or merge. Dependabot PRs are never labeled
(the Dependabot path doesn't use labels at all). `needs-human-review` is the older, separate flag and is unchanged.

**Config** (`repos.json`): top-level `"labels": true` is the default for every repo; a per-repo `"labels": false` opts a repo out
(the per-repo key wins either way). With no `labels` key anywhere, labels are off.

**Dry-run:** `--dry-run` prints `labels (dry-run): would set …` per PR and changes nothing.

**Backfill:** `pr-overseer-review --sync-labels [--repo R] [--pr N]` derives the state of each open PR from its review history
(latest verdict, fix attempts in `FIX-PR-N.json`, unreleased `HUMAN-PR-N.json`) and prints what it would change; add `--live`
to apply. PRs with no overseer verdict yet, or waiting on a re-review after new commits, are left alone.

## Weekly test-suite review / refactor (`--test-refactor`, added 2026-11-15)

Once a week (cron, Sunday 9pm) the overseer looks at each opted-in repo's test suite, finds the
**longest-running and least-impactful tests**, and opens **one PR** that rebuilds (makes faster) or deletes
them. Code lives in `testrefactor.py`; `review.py --test-refactor` runs it under the normal `.overseer.lock`.

Opt in per repo in `repos.json`:

```json
{ "repo": "your-org/your-repo", "path": "/path/to/clone", "review_mode": "self_review",
  "test_refactor": true }
```

or an object overriding the defaults in `testrefactor.DEFAULTS`: `top_n` (5), `max_changed_lines` (300),
`coverage_tolerance_pct` (0.5 pts), `runtime_regression_pct` (5), `ci_days` (21), `ci_max_runs` (60),
`ci_jobs_sample` (8), `ci_log_runs` (2), `local_timeout` (2400s per suite), `min_rebuild_s` (1.0),
`install` (true), `allow_ci_config` (false), `extra_test_globs`, `format_command` (argv lists, `{files}` =
changed test files, run before the guardrails), `suites` (override autodetect), `min_days_between_prs` (6).
A top-level `test_refactor_defaults` object applies to every opted-in repo (see
`examples/repos.example.json`).

### What it measures (deterministic, before any model runs)

1. **GitHub Actions runtimes** on the default branch for the last `ci_days` (Dependabot update runs excluded):
   per-workflow run time (median / p90 / trend = newer half vs older half), per-job and per-test-step
   durations from the jobs API (`started_at`/`completed_at`), and per-test-file / slow-test timings parsed
   from the logs of the latest green runs (vitest file + slow-test lines, jest `PASS (x s)`, pytest
   `--durations`). JUnit XML artifacts (names like junit / test-results / test-report) are used when a
   workflow uploads them.
2. **Local run** in a throwaway worktree at `origin/<default>` (`../.reviews-wt/TESTREFACTOR`, removed after):
   deps installed from the lockfile (`pnpm install --frozen-lockfile --prefer-offline` etc.), then each
   suite with per-test durations — vitest/jest JSON reporter per package, pytest JUnit XML (+ pytest-cov
   contexts if installed), or `node test/x.js` scripts timed one by one. Failing files are re-run once:
   pass on re-run = **flaky**. Skipped / todo / xfail are listed.
3. **Coverage, per test file, from one run**: `NODE_V8_COVERAGE` plus a temporary vitest setup file that calls
   `v8.takeCoverage()` after each test file (written as dotfiles into the package for the run only, removed
   after). Unique coverage = covered non-test code that no other test file covers. The overall figure is
   byte coverage of non-test code (relative metric; used for the before/after guardrail).
4. **Static scan** of each test file: real sleeps, spawned processes/containers, mocks vs assertions,
   skip markers, duplicate names, "retired/obsolete" wording.
5. **Score** = 0.6 × cost (max of CI median and local time, normalized) + 0.4 × low impact (no unique
   coverage, all skipped, retired, no assertions, mock-heavy, duplicates) + 0.25 if flaky. Suggested
   action: DELETE (low impact ≥ 0.5), REBUILD (flaky, or ≥ `min_rebuild_s` and slow/sleeps/spawns), else
   REVIEW. CI-only files (e.g. contract tests that need secrets) are reported but not handed to the harness.

### Refactor + guardrails (`--no-push` / `--live`)

The top `top_n` REBUILD/DELETE files go to a **deep-tier** write harness (same Muse → Antigravity → Cursor →
Claude failover and fix runners as address-feedback). It must print `DECISION: <path> | REBUILD|DELETE|KEEP |
<why>` per file and may edit only test files, test fixtures/helpers and test runner config. The overseer then
re-runs everything and **discards the branch** unless all hold: suite green before and after; only test paths
changed (`.github/workflows` only with `allow_ci_config`); ≤ `max_changed_lines`; coverage ≥ before −
`coverage_tolerance_pct`; a deleted file carried no meaningful unique coverage; any per-file test-count drop
is a DELETE; local test time not worse than +`runtime_regression_pct`. The commit uses the repo's hooks (no
`--no-verify`) with `Overseer-Fix-By: <harness>` so that harness never reviews it.

### Output

- `--live`: pushes `overseer/test-refactor-YYYYMMDD`, opens one PR (body = table of tests rebuilt/deleted
  with before/after local timings, CI median, unique coverage, justifications; guardrail results; CI job
  context), adds label `test-refactor` (created via the REST labels API if missing), and writes
  `reviews/<owner>__<repo>/TESTREFACTOR-PR-N.json`. Skipped while a previous test-refactor PR is open or one
  was opened < `min_days_between_prs` ago. Then **one digest email** (tiers.json `human_review` smtp
  settings) with each repo's slowest CI jobs/tests, flaky tests, coverage, candidates, PR links or why skipped.
- **Never auto-merged.** The self_review loop reviews the PR like any other but `test_refactor_merge_hold()`
  holds the merge (independent of `hold_merge`): local record without `cleared_at` → hold; no record but an
  `overseer/test-refactor-*` branch → hold. Release, then review + merge:

  ```bash
  python3 review.py --release-human-review --repo OWNER/NAME --pr N   # alias: --release-test-refactor
  python3 review.py --repo OWNER/NAME --pr N --force
  ```
- State / artifacts: `reviews/<owner>__<repo>/TESTREFACTOR-STATE.json` (last PR, run results) and
  `TESTREFACTOR-<timestamp>.md` (decisions + harness output) for no-push/live runs.

### Running it

```bash
python3 review.py --test-refactor                      # dry-run, all opted-in repos: signals + ranked candidates
python3 review.py --test-refactor --repo X --dry-run   # one repo
python3 review.py --test-refactor --repo X --no-push   # + harness, guardrails, local commit; nothing pushed
python3 review.py --test-refactor --live               # what cron runs
```

Cron: `0 21 * * 0 .../test-refactor-run.sh` (same node/pnpm PATH as cron-run.sh). It logs to
`logs/test-refactor.log` only when the run acted or warned (digest sent, PR opened, `!` line, exit ≠ 0);
`prune-logs.sh` keeps 35 days of that log.

Limitations: CI per-test timing comes from logs unless a workflow uploads JUnit XML (vitest only prints
tests over its slow threshold); unique coverage is V8 byte coverage of transformed code (relative, not
line %) and cannot see code that runs in spawned child processes; jest suites get timings but no
per-file coverage.

## Harness pick: prefer a non-author
Before running a harness, the overseer reads the PR's commits and title/body (`gh pr view --json title,body,commits`)
for authoring-tool fingerprints and avoids that tool as reviewer:
- **claude**: `Co-Authored-By: Claude … <noreply@anthropic.com>`, co-author login `claude`, "Generated with Claude Code"
- **cursor**: `Co-authored-by: Cursor <cursoragent@cursor.com>`, `Made-with: Cursor`, cursor.com/agents links / "Open in Cursor"
- **muse**: `muse-code` / login `muse` / muse@meta.ai identities, "made with Muse"
- **antigravity**: `antigravity-commits@google.com` (agy's commit identity) / login `antigravity` / `gemini[bot]` / "Gemini 4 Hydrogen" identities, "made with Antigravity/Gemini"

Greptile/CodeRabbit body blocks are ignored; a bare word like "Claude" or a human named Claude doesn't count.
It picks the first available harness (normal muse → antigravity → cursor → claude order) that isn't an author. If every
non-author is cooling down or already failed for this PR, it falls back to normal order and still reviews.
Each pick is logged, e.g. `harness pick: cursor (skipped muse — PR commits look Muse-authored (3/3 commits))`.
`--dry-run` also prints the pick. Doesn't change review_mode or merge behavior.

### Hard rule: never review with the model that made the latest commit
Separately from the soft preference above, the overseer looks at **only the PR's latest commit** (`gh api repos/O/R/commits/<sha>` + `gh pr view` commits) and **excludes** that harness from the pick outright, failing over among the rest (muse → antigravity → cursor → claude order, cooldowns respected). Signals, strongest first:
1. `Overseer-Fix-By: <muse|antigravity|cursor|claude>` trailer (written by the address-feedback fixer, below)
2. commit author / committer login, name or email (e.g. `cursoragent@cursor.com`, `noreply@anthropic.com`, login `claude`/`cursoragent`/`muse`)
3. `Co-authored-by:` / other trailers (`Co-authored-by: Cursor <cursoragent@cursor.com>`, `Co-Authored-By: Claude … <noreply@anthropic.com>`, `Made-with: Cursor`, Muse identities)
4. message text ("Generated with Claude Code", "made with Muse", …)

Unknown/human latest commit → nobody excluded. Every review logs `latest commit <sha> "<subject>": made by cursor → excluded from review pick (…)` (or `no model/provider signals → no exclusion`), and the pick line starts with `excluded cursor (latest commit);`. If the only harnesses left are the excluded one(s) (others cooling down/failed), the PR is **deferred** to the next tick (`defer:` line, no `!`) rather than self-reviewed.

## `review_mode` (per repo in `repos.json`)
| Value | Meaning |
|---|---|
| `"review"` (default; also used if missing/unknown) | Proper review. Posts APPROVE or REQUEST_CHANGES on GitHub. **Never merges.** |
| `"self_review"` | Repos where you also review your own PRs. Candidates are the union of **open non-draft PRs authored by `github_login`** (no review request needed) and **PRs that request your review**. For your own PRs: posts the review as a PR comment (GitHub won't let you approve your own PR), then on **APPROVE** — if `merge_on_approve` is true (default) — runs `gh pr merge --<merge_method> --admin --delete-branch`. REQUEST_CHANGES, drafts, and `merge_on_approve: false` never merge. Other people's PRs in the repo get a normal review, no merge (Dependabot PRs are skipped here; see **Dependabot** below). |

Optional `merge_method`: `squash` (default), `merge`, or `rebase` — use whatever the repo requires (some orgs only allow squash).

Optional `merge_on_approve` (self_review only, default `true`): set `false` to still self-review your own PRs (comment only) and still review PRs that request your review, but **never auto-merge**. Use this in shared orgs where someone else merges. `merge_method` of `none` / `off` / `false` / `no` is the same switch; Dependabot triage still uses `squash` (or an explicit squash/merge/rebase method) in that case. Per-repo `address_feedback: false` turns off auto-push fixes on those repos.

### CI gate — no merge without ALL checks green (fail closed)
Every merge (self_review APPROVE and Dependabot SAFE_MERGE) goes through `admin_merge()`, which first runs `ci_merge_gate()`: it re-reads the PR from GitHub right before merging and only merges when the PR is open, non-draft, not conflicting, its head SHA equals the SHA that was reviewed, and **every** check in `statusCheckRollup` passed (SUCCESS / NEUTRAL / SKIPPED). Pending, failing, cancelled, unknown, a status-fetch error, or **no checks at all** → no merge. The merge is pinned with `--match-head-commit <sha>`, so a push after the gate can't slip in. `dependabot_ignore_checks` does NOT apply to the merge gate.
- self_review (when `merge_on_approve` is true): if CI is still running, the PR is deferred (no harness, nothing posted) and retried next tick; if CI is failing/absent the review is still posted but the PR is not merged (`! not merged: CI gate blocked merge: …` in cron.log). Because an APPROVED PR is auto-reviewed once, merge it by hand (or `--force` re-review) after fixing CI. When `merge_on_approve` is false the review is posted immediately (no CI defer, no merge).
- `--dry-run` prints the live gate result for each self_review merge candidate, or `comment only, no merge (merge_on_approve=false)` when merging is disabled. Added 2026-11-04 after a self_review PR was admin-merged with a failing CI check.
`--dry-run` prints each repo's mode and what would happen per PR.

## Cron logging
Cron runs `cron-run.sh`, which logs to `logs/cron.log` (timestamped, with start/end banners) only when a run had action — exit≠0, reviews written, or a `!`/warning line; idle scans write nothing.
`prune-logs.sh` runs daily at 4:30am: drops cron.log lines older than 7 days (test-refactor.log: 35 days) and deletes other log files 7+ days old. Preview with `DRY=1 ./prune-logs.sh`.
Pre-timestamp history is in `logs/cron.log.pre-timestamps` (auto-deleted after 7 days).


### Each PR is auto-reviewed at most once (except self_review REQUEST_CHANGES — see below)

In **all modes**, before any harness runs the overseer checks GitHub (`gh pr view N --json reviews,comments`). It skips the PR (`skip PR #N ... → already has your review` or `... overseer self-review comment`) when either is true:

- `github_login` already has a **submitted** review on it (APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED; PENDING drafts don't count), or
- `github_login` already has an issue comment starting with `**Overseer self-review` (the marker `self_review` posts on own PRs, which can't get a formal review).

New commits after a review do **not** trigger another review — **except** on your own `self_review` PRs whose latest overseer verdict is REQUEST_CHANGES (next section). If the GitHub check fails, the PR is skipped (fail closed) and retried on the next tick. A PR is also only handled once per run. To force a re-review: `pr-overseer-review --repo owner/name --pr N --force`. Skip lines have no leading `!`, so idle cron ticks stay out of `logs/cron.log`.

### self_review: address REQUEST_CHANGES feedback (added 2026-11-04)
For **your own PRs in `self_review` repos only** (never `review` repos, never Dependabot), the gate looks at your **latest** overseer verdict (your submitted reviews + `**Overseer self-review — VERDICT: …**` comments):

| Latest verdict | Commits since it? | Action |
|---|---|---|
| none | – | review (as before) |
| APPROVE | any | skip (no re-spam; `--force` to re-review) |
| REQUEST_CHANGES | yes | **re-review** (always; no push) → APPROVE merges via the CI gate as usual |
| REQUEST_CHANGES | no | **fix** if `address_feedback` is on, else skip |

"Commits since" = PR head ≠ the `<!-- overseer-reviewed-sha: … -->` marker in the comment (new comments carry it), else any commit dated after the verdict.

**Fix** (`fix_one`): fresh PR-head worktree → harness chain in **write mode** (claude `--permission-mode acceptEdits`, cursor `agent -f`, muse exec without `--disable-write`, agy `--mode accept-edits`; normal muse → antigravity → cursor → claude order + cooldown failover), prompt = the RC review body + "only fix blockers, don't commit/push, say CANNOT_FIX if unfixable" → `git add -A` (excl. `.reviews`) → commit `overseer: address review feedback on PR #N` with `Overseer-Fix-By: <harness>` + `Co-authored-by:` trailers → re-check the remote branch hasn't moved → `git push origin HEAD:refs/heads/<branch>` (**never force**; fork PRs skipped). Nothing posted on GitHub by the fix itself. Next tick: CI pending → defer; then re-review (fixer is excluded as reviewer by the latest-commit rule) → APPROVE + green CI → merge; RC → another fix if under the cap.
- Cap: `max_fix_attempts` (default **2**) per PR, and only **one** attempt per RC verdict if it produced no changes / CANNOT_FIX. State: `reviews/<owner>__<repo>/FIX-PR-<N>.json`; fix log: `FIX-PR-<N>-<ts>.md`. A pushed fix shows in cron.log as a written artifact.
- Switch: top-level `"address_feedback"` in `repos.json` (per-repo `"address_feedback"` overrides). **Currently `false`** → cron only re-reviews RC PRs that got new commits; it never pushes. Set `true` to let cron push fixes.

```bash
pr-overseer-review --dry-run                                         # shows review / rereview / fix / skip per PR + exclusions
pr-overseer-review --address-feedback --repo your-org/your-repo --pr 72 --dry-run   # plan only
pr-overseer-review --address-feedback --repo your-org/your-repo --pr 72 --no-push  # run fixer, commit in temp worktree, don't push
pr-overseer-review --address-feedback --repo your-org/your-repo --pr 72             # LIVE: fix + push (ignores the switch; cap applies unless --force)
pr-overseer-review --no-address-feedback                             # this run: never fix
```
`--max-fix-attempts N` overrides the cap for a run. `--force` (without `--address-feedback`) still means "re-review now".

### Dependabot

The normal `review` / `self_review` loops (cron, every 15 min) still **skip** Dependabot PRs (`skip PR #N ... excluded from review loop (use --dependabot)`).

Dependabot PRs have their own **daily** path, run on a schedule (or manually):

```bash
pr-overseer-review --dependabot --list-only --max-dependabot 100   # candidates + CI/mergeable, no harness
pr-overseer-review --dependabot --max-dependabot 20                 # DRY-RUN (default): harness triage, prints would-merge/would-comment, posts nothing
pr-overseer-review --dependabot --max-dependabot 20 --live          # acts: merges SAFE ones, comments on the rest
pr-overseer-review --dependabot --repo your-org/your-repo --pr 96  # one PR
pr-overseer-review --dependabot --address-feedback --repo your-org/your-repo --pr 96  # MANUAL fix of a failing Dependabot PR (see below)
```

- **Manual Dependabot fix** (`--dependabot --address-feedback --repo X --pr N`, added 2026-11-16; never run by cron, never with `--live`): when CI on a Dependabot PR is failing, a write-enabled harness (tier from the classifier, e.g. deep for a major bump) gets the failing GitHub Actions job logs and must keep the bump and adapt code/config/lockfile. The overseer then runs its own clean install + `typecheck`/`lint`/`test`/`build` (whichever scripts exist) in each package dir the PR touched and only commits and pushes to the Dependabot branch if all pass (`--no-push` to try it locally; `--force` to run on a PR whose CI is not failing). Pushing to the branch makes Dependabot stop rebasing it. Never merges; the attempt is recorded in `reviews/<owner>__<repo>/FIX-PR-N*.`
- Scope: every repo in `repos.json`, **any `review_mode`**; open non-draft PRs by `app/dependabot`, oldest first. A repo with `"dependabot": false` in repos.json is skipped (per-repo opt-out, since Nov 18, 2026). `--max-dependabot` (default 20) caps PRs acted on per run (harness runs + conflict comments; CI-pending PRs are deferred and don't count).
- Same harness chain as reviews (muse → antigravity → cursor → claude, cooldown/failover; only Muse/Antigravity pin `--model`), run read-only in a PR-head worktree. The prompt asks it to read the diff + release notes, grep current usage, and end with `RATIONALE: …` / `DEPENDABOT_VERDICT: SAFE_MERGE | NEEDS_CODE | HOLD`. No parseable verdict → HOLD.
- **SAFE_MERGE** + ALL CI checks green (no CI = HOLD; see **CI gate**) + not conflicting → `gh pr merge --<merge_method> --admin --delete-branch`, then an audit comment. This applies in `review` repos too (Dependabot-only exception to "review never merges"). If CI is failing or the merge fails it is downgraded to HOLD.
- **NEEDS_CODE / HOLD** (and CONFLICTING PRs, which skip the harness) → PR comment `**Overseer dependabot — VERDICT: …**` @-mentioning CODEOWNERS for the changed files (read from `origin/<base>`, last matching rule wins; `.github/CODEOWNERS`, `CODEOWNERS`, `docs/CODEOWNERS`). No CODEOWNERS / no match → `@github_login`.
- CI still running → deferred to the next run (no harness, nothing posted).
- Skips PRs already reviewed by `github_login` or carrying an `**Overseer self-review` / `**Overseer dependabot` comment from you (`--force` overrides). Comments are never deleted.
- Optional per-repo `"dependabot_ignore_checks": ["QA Plan"]` in `repos.json` ignores check names (substring match) in the triage prompt/labels only (the merge gate still requires every check green) — e.g. a required "QA Plan" check that fails on every Dependabot PR. Not set by default.
- Artifacts: `reviews/<owner>__<repo>/DEPENDABOT-PR-<N>.md` (dry-run too). Waits up to 30 min for `.overseer.lock` (`--lock-wait`) so it doesn't lose to a cron tick; cron ticks skip while it runs.
- Not in crontab. Optional local cron line: `15 6 * * 1-5 ~/bin/pr-overseer-review --dependabot --max-dependabot 20 --live --log ~/workspace/ai/pr-overseer/logs/dependabot.log`

- Concurrency: cron-run.sh skips if a prior tick holds `.cron.lock` (flock); review.py also takes `.overseer.lock`, so manual runs never overlap cron. Cron passes `--max-reviews 3` (at most 3 PRs reviewed per tick); manual runs without the flag review all.

### No-harness pre-check (no wasted runs) — 2026-11-05
Before any worktree / prompt / harness work, each PR's **eligible harness set** is computed (`eligible_harnesses()`: muse → antigravity → cursor → claude order, minus `cooldown_until` in harnesses.json, minus the latest-commit exclusion, minus binaries not on PATH; the fix path uses the same check against the write-mode runners). Empty set → one quiet line `skip PR #N … → no eligible review harness now (…); not counted toward --max-reviews, retry after HH:MM` (no `!`, so idle ticks don't touch cron.log) and the PR is retried on a later tick. The cooldown expiry times are the backoff: a deferred PR costs only the authorship `gh` lookups per tick until a harness frees up.
`--max-reviews` now counts only PRs that reached a real harness result (non-quota output, a real failure, or a timeout) or wrote an artifact; CI-pending defers and quota-failover exhaustion (`(no harness result; not counted toward --max-reviews)`) don't eat the per-tick budget. Dependabot (`--dependabot`) logic is unchanged. Backup of the pre-change files: `.bak-20261105-preskip/`.

## Quota detection (fixed 2026-11-06)
`looks_like_quota()` no longer scans the review body. A clean exit with a `VERDICT:` line or ≥500 chars of output is never treated as a quota hit; otherwise only stderr (plus short stdout banners) is matched. Previously a PR whose topic mentioned "quota"/"rate limit" gave muse/cursor phantom 15-min cooldowns. Test: `python3 test_quota_detect.py`.
