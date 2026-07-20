# DEL — Markdown Docs Accuracy Audit (2026-07-20)

Scope: README.md, INSTALL.md, OPERATIONS.md, SECURITY.md, RECOVERY.md, UNINSTALL.md,
docs/ARCHITECTURE.md, docs/DISCOVERY.md, docs/REMOVAL-LIFECYCLE.md,
docs/DEPLOYMENT-CONVENTION.md, docs/SYSTEM-STATE.md. docs/INTERFACES.md and
docs/server-audit.md were read for cross-checking only, not edited (out of scope).
No code, fern/, or config/ files were touched. No secrets printed.

Method: every factual claim below was checked against the live host/actual files —
systemd unit files (installed under `/etc/systemd/system/`, not just `config/`),
`helper/del_helper.py` `ALLOWED_OPS`, `backend/del_app/web/routes.py` route
decorators, `backend/del_app/planner.py` `STAGE_ORDER`, `config/nginx-del.bjk.ai.conf`,
`config/helper-policy.json`, `config/del.toml`, `manifests/del.yaml`, live `ufw
status verbose`, live `ss -ltnp` for the Cockpit socket, `/etc/isbd.env` permissions,
and `docker ps` / `ls` for the apps claimed removed.

## Per-doc claim table

| Doc | Claim checked | Result | Evidence |
|---|---|---|---|
| README.md | Bind 127.0.0.1:8075, unit names, helper socket 0660 root:bjkai, `/opt/del` symlink, DB path, admin CLI subcommands | Verified | `del-web.service`/`del-helper.service` files, `ls -la /opt/del` → symlink to `/apps/del`, `del-admin` argparse subparsers match exactly |
| README.md | Doc index completeness | **Fixed** | Index was missing docs/DEPLOYMENT-CONVENTION.md, docs/SYSTEM-STATE.md, docs/PORT-REGISTRY.md — added rows |
| README.md | Miscwork inventory export (`/miscwork.html`, `/inventory`) undocumented | **Fixed** | Not mentioned anywhere before; added a Quick Facts row (nginx conf confirms both routes, basic-auth gated same as `/docs`) |
| INSTALL.md | install.sh steps (venv check, migrate, systemd install order, health poll, nginx site, HTTPS check) | Verified | Matches `scripts/install.sh` line-by-line |
| INSTALL.md | `/etc/letsencrypt/live/bjk.ai/{fullchain,privkey}.pem` present | Verified | `sudo ls` confirms both symlinks exist and resolve |
| OPERATIONS.md | 3 unit names, restart order (helper before del-web), restart/backoff timers | Verified | del-web `RestartSec=3`, del-helper `RestartSec=2`, del-docs `RestartSec=10` (via `systemctl cat del-docs.service`) |
| OPERATIONS.md | `gen-registry.py`/`gen-registry.sh` regenerate `docs/PORT-REGISTRY.md` | Verified | scripts exist and match description |
| OPERATIONS.md | Miscwork inventory export not mentioned | **Fixed** | Added a short section describing the export, its live URL, and that it's a manual/point-in-time snapshot (`miscwork/extract.py`, `inline_build.py` confirmed to build it) |
| SECURITY.md | 22-op helper allowlist, exact op names | Verified | `ALLOWED_OPS` set in `helper/del_helper.py` = 22 ops, names match doc's list exactly |
| SECURITY.md | Protected roots list, approved deletion roots list | Verified | Byte-for-byte match against `config/helper-policy.json` `protected_roots` / `approved_deletion_roots` |
| SECURITY.md | Cockpit bound to 127.0.0.1:9091 only | Verified | `ss -ltnp` shows `cockpit-tls` listening on `127.0.0.1:9091` only; `cockpit.socket.d/listen.conf` confirms override from default 9090 |
| SECURITY.md | `/etc/isbd.env` mode 640, owner root:bjkai | Verified | `ls -la /etc/isbd.env` → `-rw-r----- root bjkai` |
| SECURITY.md | Hardening flags for all 3 units | Verified | Matches installed unit files exactly |
| RECOVERY.md | DB restore, helper-socket troubleshooting, nginx `.bak` rollback, venv rebuild steps | Verified | Paths/commands consistent with install.sh backup naming and RuntimeDirectory=del in del-helper.service |
| UNINSTALL.md | 7-step manual removal (units, nginx site, htpasswd, tree, `/run/del`, DNS) | Verified | Matches actual unit/nginx/manifest file locations; `/opt/del` confirmed a symlink only (step 5 wording accurate) |
| docs/ARCHITECTURE.md | Stack table, deployment units, process/privilege diagram, helper op table, code layout tree, data model, confidence scoring | Verified | Discovery module list matches `backend/del_app/discovery/*.py` exactly; code layout tree matches `backend/del_app/` contents; op table matches `ALLOWED_OPS` |
| docs/DISCOVERY.md | Source module table, `scan_roots`, resource types, evidence/confidence table, correlation rules, port-registry description | Verified | `scan_roots` in `config/del.toml` = `["/apps","/data/apps","/opt","/srv","/var/www"]`, matches doc exactly |
| docs/REMOVAL-LIFECYCLE.md | 9-stage lifecycle, `_HALTING_STAGES`, volume double-confirmation, protected roots, resumable jobs | Verified | `jobs.py` `_HALTING_STAGES` = `{backup, quiesce, remove_runtime, remove_host, remove_files, validate}`; planner emits ops per stage exactly as described (backup/quiesce/remove_runtime/remove_host/remove_files/validate); Analyze/Preview/Report are the pre/post wrapper stages around the 6 job-engine stages, consistent with `jobs.py`'s own docstring `analyze→preview→backup→quiesce→remove→validate→report` |
| docs/DEPLOYMENT-CONVENTION.md | Manifest schema, `del.yaml` example (incl. `del-docs.service` in `systemd_units`), port range note, isbd script behavior, ufw/Cockpit cross-refs | Verified | `manifests/del.yaml` matches the example verbatim; `isbd` script confirmed to source `/etc/isbd.env` and PATCH IONOS as described |
| docs/SYSTEM-STATE.md | Directory/docker/nginx/port counts, shared-resource map, needs-attention list | Verified where feasible (host counts trusted as an authored point-in-time audit); one material staleness found and fixed |
| docs/SYSTEM-STATE.md | §5 needs-attention list still showed `liam`, `twenty`, `zabbix`, `OpenPdf` as open issues | **Fixed (contradiction)** | Bottom-of-doc "Removed 2026-07-20" section already stated these 4 apps were fully removed via DEL; live-checked `/apps/{OpenPdf,liam,twenty,zabbix}` (all gone), nginx sites-enabled (all gone), `docker ps -a` (no containers) — confirmed genuinely removed. Removed the 4 apps from the exec summary and the §5 table (item #1 dead-upstream list: 8→6 entries; item #2 OpenPdf and item #5 liam rows deleted, remaining items renumbered 1–11), and added a pointer to §6 |
| docs/SYSTEM-STATE.md | ufw wording "policy DROP" | **Fixed (precision)** | `ufw status verbose` reports `Default: deny (incoming)`, not the literal string "DROP" (iptables target vs. ufw's own vocabulary) — reworded to match ufw's actual output |
| docs/SYSTEM-STATE.md | Cockpit "confirmed resolved... now 127.0.0.1:9091 only" | Verified | Matches live `ss -ltnp` |

## Path-existence check

Ran a script extracting every backtick-quoted absolute path from all 11 owned docs
and checking `os.path.exists()`, excluding templated (`<name>`) and URL-route tokens
(`/docs`, `/_next`, `/login`, `/healthz`, `/inventory` — these are Nginx/HTTP routes,
not filesystem paths, and were separately verified against
`config/nginx-del.bjk.ai.conf` and `backend/del_app/web/routes.py`).

Result: **all real filesystem paths exist**, except `/data`, which is referenced as
a protected/scan/deletion root in DISCOVERY.md, REMOVAL-LIFECYCLE.md, and
SECURITY.md — this is consistent with `config/del.toml` (`scan_roots`) and
`config/helper-policy.json` (`protected_roots`/`approved_deletion_roots`), which
also list `/data` even though the directory doesn't currently exist on this host;
it's a reserved/future root, not a doc error, so left as-is.

Also ran a link-resolution check on every relative Markdown link in the 11 docs
(`[text](path)`) — all resolve to existing files, no broken links.

## Fixes made (summary)

1. **README.md** — added missing doc-index rows for `docs/DEPLOYMENT-CONVENTION.md`,
   `docs/SYSTEM-STATE.md`, `docs/PORT-REGISTRY.md`; added a Quick Facts row for the
   miscwork inventory export (`/miscwork.html`, `/inventory`).
2. **OPERATIONS.md** — added a "Whole-server inventory export" section documenting
   the live `/miscwork.html`/`/inventory` routes and how the export is rebuilt
   (manual, `miscwork/extract.py` + build scripts, no scheduled job).
3. **docs/SYSTEM-STATE.md** — resolved a real contradiction: the executive summary
   and §5 needs-attention table still listed `liam`, `twenty`, `zabbix`, and
   `OpenPdf` as open issues, while the doc's own "Removed 2026-07-20" section said
   they'd already been fully removed via DEL. Live-verified the removal (no
   directories, no nginx sites, no containers) and updated the exec summary + §5
   table accordingly, renumbering items 1–11.
4. **docs/SYSTEM-STATE.md** — reworded "ufw active, policy DROP" to "ufw active,
   default policy deny (incoming)" to match `ufw status verbose`'s actual output
   rather than an invented iptables-target term.

## VERDICT

**4 fixes** across README.md, OPERATIONS.md, and docs/SYSTEM-STATE.md (one of which
— the stale needs-attention list — was a genuine factual contradiction, not just
missing cross-referencing). All other checked claims (ports, unit names, the 22-op
helper allowlist, all web routes, nginx locations, protected/deletion roots,
cockpit/ufw/isbd facts, the 9-stage removal lifecycle, manifest schema, and every
filesystem path and internal doc link) were verified accurate against live
code/config/host state with no drift found.
