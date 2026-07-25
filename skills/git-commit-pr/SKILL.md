---
name: git-commit-pr
description: >
  Use when committing, branching, or opening a pull request in this repo.
  Enforces small, single-concern PRs and atomic commits so reviewers have an
  easy job: branch off main, one purpose per branch/PR, conventional-commit
  messages, and never mix a new feature, bug fix, cleanup, and refactor in one
  PR. Also covers how to split work that is already mixed.
---

# Committing & opening PRs

Optimize for the **reviewer**. A good PR is small, does exactly one thing, and
can be understood in a single sitting. The author does the work of organizing so
the reviewer does not have to untangle it.

## Golden rule: one concern per PR

Never mix these in a single PR (or a single commit):

- **feature** — new behavior
- **fix** — a bug fix
- **refactor** — behavior-preserving restructuring
- **cleanup / style** — formatting, renames, comments, lint fixes
- **chore** — deps, CI, tooling, config

If a task naturally spans several of these, produce **several PRs** (or at least
several clearly-separated commits), landed in a sensible order — usually
cleanup/refactor first, then the feature/fix on top of the clean base.

## Before you start

1. **Only commit or push when the user asks.** Do not commit as a side effect of
   finishing an edit.
2. **Branch off `main`** (never commit directly to `main`). Pull latest first.
3. Decide the *one* concern this branch delivers before writing code. If you
   discover an unrelated problem mid-task, note it — don't fix it here.

## Branch naming

`<type>/<short-kebab-summary>`, where type is one of
`feat` `fix` `refactor` `chore` `docs` `test`. Examples:

```
feat/s3-catalog-query
fix/gunw-cycle-parse
refactor/consolidate-search
chore/add-ci-pytest
```

## Commit hygiene

- **Atomic**: each commit compiles, passes tests, and expresses one logical
  change. A reviewer should be able to read the history commit-by-commit.
- **Conventional-commit subject**, imperative mood, <= ~72 chars:
  `type(scope): summary` — e.g. `fix(filenames): keep v1.0 version suffix`.
- **Keep the message concise and high level.** State the intent, not a play-by-play
  of the diff. Prefer a single subject line; add a short body only when the *why*
  isn't obvious. Do not enumerate every file or change — the diff already shows
  what changed. No multi-paragraph essays, no bullet-list changelogs of each edit.
- End the message with the trailer the harness requires:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Do not bundle whitespace/format churn with logic changes — that hides the real
  diff. Format-only changes go in their own `chore`/`style` commit.

## Pre-flight gate (run BEFORE opening a PR — mandatory)

Do not open a PR until these pass locally. This is a gate, not a suggestion: a
red result blocks the PR. Fix the issues (or split the branch) and re-run — never
open a PR "to let CI find the problems."

Run, in order:

```bash
pre-commit run --all-files    # ruff (lint) + black (format) + mypy + hooks
pytest                        # full test suite (unit + integration + regression)
```

In this repo the tooling lives in the `nisar-db-env` conda env
(`pytest`, `pre-commit`, `ruff`, `black`, `mypy` are installed there). The CI
[`ci.yml`](../../.github/workflows/ci.yml) runs the same two checks, so a clean
pre-flight means CI is green.

This gate is also enforced automatically: after `pre-commit install`, the hooks
run lint/format/type on every commit and the **full test suite on `git push`**
(the `pre-push` stage in [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml)).
A failing push is blocked — so run pushes from the project env, and don't bypass
the hook with `--no-verify`.

Rules for the gate:

- If `pre-commit` **modifies files** (its fixers auto-fix), review those changes,
  stage them into the *appropriate* single-concern commit (format churn belongs
  in its own `chore`/`style` commit — see Commit hygiene), and re-run until clean.
- If a lint/type error is pre-existing and unrelated to your change, do **not**
  silently absorb it into your PR — raise it separately.
- State the pre-flight result in the PR description (e.g. "pre-commit: clean,
  pytest: 86 passed").

## PR hygiene

- **Small**: aim for a reviewable diff (rough target a few hundred changed lines,
  excluding generated files). If it's bigger, split it.
- **Single-purpose title**, same conventional-commit form as commits.
- **Description** answers: *what* changed, *why*, *how to verify* (commands/tests),
  and any follow-ups deliberately left out of scope.
- **Pre-flight is green** (see above) and its result is stated in the PR.
- PR body ends with the harness trailer:
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

## When the working tree is already mixed

If you've ended up with feature + fix + cleanup all uncommitted:

1. **Prefer splitting by file** — commit each concern's files separately:
   `git add <files-for-concern-A> && git commit -m "..."`, repeat.
2. Park the rest while you shape one commit: `git stash push -- <paths>` or
   `git restore --staged <paths>`.
3. For **multiple concerns tangled in the same file**, `git add -p` / `git stash
   -p` are the normal tools — but they are **interactive and not available in
   this non-interactive harness**. Options here: (a) edit the file down to just
   one concern, commit, then re-apply the rest; or (b) tell the user this hunk
   needs an interactive split and let them do it. Don't jam the tangle into one
   commit to save effort.

## Anti-patterns (reject these)

- "Misc fixes and improvements" — meaningless to a reviewer; split it.
- A refactor that also sneaks in a behavior change.
- A feature PR that reformats 20 unrelated files.
- One giant commit at the end of a multi-step task.
- Opening a PR before the pre-flight gate (`pre-commit` + `pytest`) passes.
- Committing/pushing without being asked, or pushing straight to `main`.
