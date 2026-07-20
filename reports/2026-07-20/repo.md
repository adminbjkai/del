# DEL — Git/GitHub Repo Hygiene Audit (2026-07-20)

Repo: `/apps/del`, remote `github.com/adminbjkai/del` (public). Read-only audit — no git writes performed.

## 1. `git status --short`

```
 M config/nginx-del.bjk.ai.conf
```

- **Tracked-but-modified:** `config/nginx-del.bjk.ai.conf` (1 file). This is a source/config file that belongs in the repo — recommend committing the diff (orchestrator should review the diff content before adding, but it is a legitimate in-repo config file, not a secret).
- **Untracked files:** none (`git status --short --untracked-files=all` returned nothing beyond the modified file above).
- No files need categorizing into "should be committed" vs "should stay ignored" — there is currently no untracked material in the working tree.

Note: `/apps/del/reports/2026-07-20/` existed as an empty directory at audit time (no files in it yet from other lanes). This audit report itself is the first file placed there. If other verification-report files land in this directory from parallel work, the orchestrator should re-run the sensitive-file grep (step 2 below) against them before `git add`.

## 2. Sensitive-file scan

Command run against tracked files only:

```
git ls-files | grep -iE 'secret|password|\.db$|\.key$|htpasswd|admin-initial|docs-basic|helper-audit|PORT-REGISTRY|server-audit|inventory\.json|del-inventory|/data/audit/|miscwork/'
```

**Output: empty (no matches, grep exit code 1 = no match found).**

Confirmed: nothing sensitive is currently tracked in git.

## 3. `.gitignore` correctness

Current `.gitignore`:

```
.venv/
__pycache__/
*.pyc
database/
backups/
logs/
data/audit/raw-*.txt
config/secret.key
config/admin-initial-password.txt
config/docs-basic-auth-password.txt
fern/node_modules/
.pytest_cache/
data/
docs/server-audit.md
PROGRESS.md
docs/INTERFACES.md
docs/PORT-REGISTRY.md
miscwork/
```

Checked against the required exclusion list:

| Required exclusion | Present? | Note |
|---|---|---|
| `.venv/` | Yes | |
| `database/` | Yes | |
| `backups/` | Yes | |
| `logs/` | Yes | |
| `data/` | Yes | (also `data/audit/raw-*.txt` is redundant given the broader `data/` rule but harmless) |
| `config/secret.key` | Yes | |
| `config/*-password.txt` | **Partial — gap** | `.gitignore` lists the two *specific* filenames (`config/admin-initial-password.txt`, `config/docs-basic-auth-password.txt`) rather than the glob `config/*-password.txt`. Functionally equivalent today since `config/` currently only contains those two `*-password.txt` files (verified via `ls -la config/`), but any *new* `config/*-password.txt` file created in the future would NOT be auto-ignored by the current exact-name entries. Recommend (report only, not fixing) tightening to the glob form for future-proofing. |
| `docs/PORT-REGISTRY.md` | Yes | |
| `docs/server-audit.md` | Yes | |
| `docs/INTERFACES.md` | Yes | |
| `PROGRESS.md` | Yes | |
| `miscwork/` | Yes | |
| `fern/node_modules/` | Yes | |

No missing exclusions among files that currently exist on disk. The one gap is the exact-filename-vs-glob pattern for `*-password.txt` noted above — a latent risk, not a current leak (verified no `*-password.txt` files are tracked, and the two that exist have `0600` perms and are excluded by their exact names).

## 4. Remote sync

```
local HEAD:  29abedefb3a6a7050e043a0b8eae2a3bea267122
remote HEAD: 29abedefb3a6a7050e043a0b8eae2a3bea267122   (git ls-remote origin HEAD)
```

Local HEAD == origin HEAD. Branch `master` tracks `origin/master` and `git status` reports "up to date with 'origin/master'". No unpushed local commits.

## 5. README doc-index accuracy

All 10 docs listed in README's "Documentation index" table exist on disk:

`INSTALL.md`, `OPERATIONS.md`, `SECURITY.md`, `RECOVERY.md`, `UNINSTALL.md`, `docs/ARCHITECTURE.md`, `docs/INTERFACES.md`, `docs/DISCOVERY.md`, `docs/REMOVAL-LIFECYCLE.md`, `docs/server-audit.md` — all present, no broken/missing links from that list.

**Two gaps found:**

1. **Dead link for external cloners:** README links to `docs/server-audit.md`, but that file is in `.gitignore` and is **not tracked** in git. Anyone who clones the public repo will get a 404/missing-file for that README link. This is a pre-existing, known-intentional split (server-audit.md contains host-specific audit data that should stay local) — but the doc-index table doesn't flag it as "local-only," which could confuse external readers. Report only, not fixing.
2. **Orphaned docs — tracked but not indexed in README:** `docs/DEPLOYMENT-CONVENTION.md` and `docs/SYSTEM-STATE.md` are both tracked in git (confirmed via `git ls-files`) but neither appears in the README documentation-index table. These are real, public, in-repo docs that a reader browsing README would not discover.

Correctly-excluded docs (present on disk, correctly gitignored, correctly absent from git tracking): `docs/server-audit.md`, `docs/PORT-REGISTRY.md`, `docs/INTERFACES.md`... wait, `docs/INTERFACES.md` is gitignored per `.gitignore` but README links to it as if in-repo — same dead-link issue as `docs/server-audit.md`. Confirmed via `git ls-files docs/INTERFACES.md` → empty (untracked). `PROGRESS.md` is correctly gitignored and correctly not linked from README.

## 6. `reports/2026-07-20/` directory

At the time of this audit, `/apps/del/reports/2026-07-20/` was **empty** (no files from other verification lanes had landed yet). This report (`repo.md`) is the first file in it. No secret-scan was possible on non-existent files.

**Recommendation for the orchestrator:** once other lanes' report files (code/doc verification reports) land in `reports/2026-07-20/`, re-run the sensitive-file grep pattern from step 2 against `reports/2026-07-20/*.md` specifically before committing, since these reports may reference config paths, ports, or service names. Based on the task description ("code/doc verification reports... no server secrets"), they are expected to be safe to commit, but this could not be independently confirmed here as the files did not yet exist.

## 7. Recommended `git add` file list

Given the current repo state (one modified tracked file, no untracked source files, and this new report):

```
git add config/nginx-del.bjk.ai.conf
git add reports/2026-07-20/repo.md
```

Plus, when other lanes' report files exist in `reports/2026-07-20/` (verified secret-free per step 6's re-scan), add those too, e.g.:

```
git add reports/2026-07-20/*.md
```

**Files that must stay ignored / NOT be added** (already correctly excluded by `.gitignore`, confirmed untracked):
- `config/secret.key`
- `config/admin-initial-password.txt`
- `config/docs-basic-auth-password.txt`
- `docs/server-audit.md`
- `docs/PORT-REGISTRY.md`
- `docs/INTERFACES.md`
- `PROGRESS.md`
- `miscwork/` (entire directory)
- `database/`, `backups/`, `logs/`, `data/`, `.venv/`, `fern/node_modules/`

## VERDICT

**PASS — safe to commit**, with two minor non-blocking documentation gaps to optionally address separately (not a git-hygiene blocker):

- Zero sensitive files tracked in git (confirmed by grep, empty result).
- `.gitignore` covers all required exclusions for files that currently exist; one latent gap (exact-filename vs glob for `*-password.txt`) noted for future-proofing, not currently exploitable.
- Local HEAD is in sync with `origin/master` (no divergence, nothing unpushed).
- README doc-index has two dead links (`docs/server-audit.md`, `docs/INTERFACES.md` — both intentionally gitignored) and two orphaned/undocumented-but-tracked docs (`docs/DEPLOYMENT-CONVENTION.md`, `docs/SYSTEM-STATE.md`). Cosmetic — does not affect git hygiene or security.
- Recommended commit set: `config/nginx-del.bjk.ai.conf` (review diff first) + `reports/2026-07-20/*.md` (re-scan for secrets once other lanes' files exist, per step 6).
