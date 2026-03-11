---
name: bub-upstream-backport
description: |
  Project-specific workflow for maintaining this ximenzun/bub fork: first fast-forward
  local and origin main to bubbuild/bub upstream/main, then review new upstream/main
  commits after the recorded cursor and selectively port them into dev with PRs targeting dev.
---

# Bub Upstream Backport

Use this skill only in this repository.

## Repo Contract

- `origin` is `ximenzun/bub`
- `upstream` is `bubbuild/bub`
- integration target is `origin/dev`
- local `main` should mirror `upstream/main`
- the reviewed upstream cursor is stored in branch `backport/upstream-main-reviewed`

Do not use `dev..main` to decide what is new. The cursor must track the last reviewed
`upstream/main` commit, because manual ports into `dev` break one-to-one commit mapping.

## Workflow

Run the workflow in this order:

1. Ensure the working tree is clean.
2. Switch to `main` and fast-forward it to `upstream/main`.
3. Push refreshed `main` to `origin/main`.
4. Switch back to `dev` and review new upstream commits after the cursor.
5. Create a dedicated backport branch from `dev`.
6. Cherry-pick or manually port selected upstream changes.
7. Verify, open a PR to `dev`, then move the cursor forward.

Do not skip step 2. The backport review should always happen after local `main` has been
synchronized with the latest upstream state.

## Step 0: Preflight

If the working tree is dirty, stop and either commit or stash before touching `main`.

Refresh remotes first:

```bash
git fetch upstream origin
```

## Step 1: Sync Main To Upstream

```bash
git switch main
git fetch upstream origin
git merge --ff-only upstream/main
git push origin main
```

If `git merge --ff-only upstream/main` fails, stop and inspect why `main` diverged. This
workflow assumes `main` is only a mirror of upstream.

## Step 2: Prepare The Reviewed Cursor

List the cursor branch:

```bash
git branch --list backport/upstream-main-reviewed
```

If it does not exist yet and the previously reviewed upstream stop-point is still the
2026-03-11 batch, initialize it to:

```bash
git branch -f backport/upstream-main-reviewed 8e9a0465b2926714432d64c204f0698becdcdc88
git push origin backport/upstream-main-reviewed --force
```

After initialization, move this cursor only forward.

## Step 3: Review New Upstream Commits

Switch back to `dev` before review and implementation:

```bash
git switch dev
git pull --ff-only origin dev
git log --reverse --oneline backport/upstream-main-reviewed..upstream/main
```

Check the overall scope:

```bash
git diff --stat backport/upstream-main-reviewed..upstream/main
git diff --name-only backport/upstream-main-reviewed..upstream/main
```

For each candidate commit:

```bash
git show --stat <commit>
git merge-tree $(git rev-parse <commit>^) dev <commit> | sed -n '1,220p'
```

## Step 4: Decide Cherry-Pick vs Manual Port

Default heuristics:

1. Prefer direct `cherry-pick` for isolated docs, tests, or low-overlap infrastructure files.
2. Prefer manual porting when the commit touches shared runtime layers such as:
   - `src/bub/builtin/*`
   - `src/bub/framework.py`
   - `src/bub/hookspecs.py`
   - `src/bub/channels/*`
   - tool contracts, tape format, or dependency-sensitive logic
3. If a commit is intentionally skipped, record that decision and still treat it as reviewed
   before advancing the cursor.

## Step 5: Apply The Backport Batch

Create a branch from `dev`:

```bash
git switch dev
git pull --ff-only origin dev
git switch -c feat/backport-<topic>
```

Then either cherry-pick:

```bash
git cherry-pick <commit>
```

Or port manually and commit once the batch is coherent.

## Step 6: Verify

```bash
uv run ruff check .
uv run pytest -q
uv run mypy src
```

If `mypy` reports only known pre-existing failures outside the backport scope, call that out
explicitly in the PR body instead of blocking the batch.

## Step 7: Open The PR

Always target `dev`:

```bash
git push -u origin <branch>
gh pr create --repo ximenzun/bub --base dev --head <branch>
```

In the PR summary, separate:

- directly cherry-picked upstream commits
- manually ported upstream behavior
- intentionally skipped upstream commits

## Step 8: Advance The Cursor

After the review decision is complete, move the cursor to the last upstream commit that has
been fully reviewed:

```bash
git branch -f backport/upstream-main-reviewed <last-reviewed-upstream-commit>
git push origin backport/upstream-main-reviewed --force
```

Advance the cursor only after the review decision is complete. Do not advance it merely
because an exploratory branch exists.
