# DEL — Security

## Auth model

- Single admin account (or more, if created), **Argon2id** password hashing via
  `argon2.PasswordHasher` — that is the only hasher; there is no bcrypt fallback
  path. Accounts are created only via `del-admin create-admin` /
  `change-password`; there are no default or hardcoded credentials.
- The unauthenticated routes are exactly: `/login`, `/healthz`, `/favicon.ico`
  (a 301 to the SVG), and the four static assets `/static/app.css`,
  `/static/app.js`, `/static/theme-init.js`, `/static/favicon.svg`. Every other route carries
  `Depends(auth.require_user)` — including `/app-icon/{domain}` (the
  gallery's server-side favicon proxy) and `/palette.json` (the
  command-palette data feed). Unauthenticated requests to protected routes
  redirect to `/login` (303).
- Nothing writes a password to disk. `del-admin` takes it from a `getpass` prompt
  or stdin and persists only the argon2 hash. The
  `/apps/del/config/admin-initial-password.txt` this document used to describe has
  been deleted and is not regenerated.

## Sessions, CSRF, rate limiting

- Session tokens are server-side (a `sessions` table keyed by `token_hash`, not the
  raw token), 12-hour expiry (`session_hours` in `del.toml`).
- Cookies: `HttpOnly`, `Secure`, `SameSite=Lax`. The pre-login CSRF seed cookie
  carries `Secure` too.
- Expired session rows are swept on every successful login. Expiry was always
  enforced at read time, so a stale row was never an auth bypass, but nothing
  deleted them and the table only grew.
- CSRF token required and checked on every mutating (`POST`) request
  (`auth.csrf_token` / `auth.check_csrf`); forms and `app.js` fetches both carry it.
  JSON posts from the Assistant also send `X-CSRF-Token` (same HMAC as the form field).
- Login is rate-limited to 5 attempts per 60 seconds per IP — a plain in-memory
  sliding window (`auth.rate_limited` / `auth.record_attempt`). There is no
  escalating backoff and no persistence: a `del-web` restart clears the counters.

## File permissions

- `/apps/del/database/del.db` (+ `-wal`/`-shm`): mode `0640`. It holds the admin
  argon2 hash and session token hashes, and was previously world-readable.
- `/apps/del/backups/`: mode `0750`.
- `/apps/del/config/secret.key` (the HMAC signing key): mode `0600`.
- `/apps/del/config/ollama-api-key.txt` (Assistant): mode `0600`, refused if group/world readable; gitignored via `config/*-api-key.txt`.
- `/usr/local/lib/del-helper/` and `/etc/del/helper-policy.json`: `root:root`,
  not writable by `bjkai`. See the next section for why that is load-bearing.

## Helper privilege split

`del-web` (user `bjkai`) never runs privileged host operations directly and never
constructs shell strings from user input. Instead it sends a typed operation name +
validated structured arguments as JSON over a unix socket
(`/run/del/helper.sock`, mode `0660`, owner `root:bjkai`) to `del-helper`, a separate
stdlib-only daemon running as `root` (`del_helper.py` ~740 lines plus
`validation.py` ~400, about 1,100 lines together). `del-helper` re-validates every
argument independently of whatever `del-web` claims, and executes exclusively via
subprocess argument arrays (`shell=False`) — never a shell string.

### Where the helper's code and policy actually live

`del-helper.service` runs `/usr/local/lib/del-helper/del_helper.py` with
`/etc/del/helper-policy.json`, both `root:root` and installed by
`scripts/install.sh` with `install -o root -g root`. The repo copies (`helper/*.py`,
`config/helper-policy.json`) are the source you edit; the deployed copies are what
root executes and reads.

This split is the point of the boundary, not a packaging detail. `/apps/del` is
writable by `bjkai`, the account `del-web` runs as — so pointing `ExecStart` at the
working tree would let a compromised web tier rewrite the code root is about to run,
and rewrite the allowlist policy alongside it. **After editing `helper/` or
`config/helper-policy.json`, re-run `scripts/install.sh` (or re-`install` the two
paths by hand) before restarting `del-helper`; a plain restart re-runs the old
deployed copy.**

### Allowlisted operations (summary)

`ping`, `list_listeners`, `compose_down`, `container_stop`/`container_rm`,
`image_rm`, `volume_rm`, `network_rm`, `systemd_stop`/`systemd_disable`/`systemd_rm_unit`,
`cron_rm`, `nginx_rm_site`, `nginx_test`, `nginx_test_reload`, `path_delete`,
`path_restore`, `tmux_kill`, `process_term`, `backup_tar`, `volume_backup`,
`file_backup` — **22 operations**, matching `ALLOWED_OPS` in `del_helper.py`.
Nothing outside this fixed list is possible; a compromised `del-web` cannot smuggle
an arbitrary command past the helper. Every operation supports `dry_run` (returns
what would happen without changing anything) and is logged to
`/apps/del/logs/helper-audit.log` regardless of outcome. Live volume deletion
additionally requires a plan option, a per-volume checkbox, and a typed
confirmation phrase at execution time (see docs/REMOVAL-LIFECYCLE.md).

### Where plan integrity is enforced

Plans are signed with an HMAC over the canonical JSON of their steps (key
`/apps/del/config/secret.key`, mode `0600`) when written to the `plans` table, and
`planner.verify_plan()` recomputes and compares it before a job is created and
again before it runs. **That check is entirely web-side.**

The helper has no concept of a plan. `handle_request` reads `op`, `args` and
`dry_run`; it also records `plan_id`, `step_id`, `job_id` and `requested_by` in the
audit line, but it does not — and cannot — verify them, because it never sees the
`plans` table. What bounds a compromised web tier is therefore not the signature
but the fixed op allowlist plus the helper's own independent argument validation
against `/etc/del/helper-policy.json`. Read every "must appear in the approved
plan" phrasing in this repo as a *planner* constraint, not a helper one.

### Helper-side refusals that do not depend on the caller

These are enforced inside the root daemon, because the web tier's equivalents run
inside the process an attacker would already control:

- **`protected_units`** — `systemd_stop`/`systemd_disable`/`systemd_rm_unit` refuse
  any unit matching the policy's glob list: DEL's own `del-*.service`/`del-*.timer`
  first (stopping `del-helper` is the opening move in a helper-code-swap attack),
  then `ssh`/`sshd`, `nginx`, `docker`/`containerd`, `systemd-*`, `cron`, `dbus`,
  `network*`, `polkit`, `rsyslog`, `fail2ban`, `ufw`.
- **`path_restore` confinement** — the destination must resolve under a DEL-managed
  root (the approved deletion roots plus nginx sites, `/etc/systemd/system`,
  `/etc/cron.d`), must not be protected or never-delete, and **must keep the
  backup's basename**. `/etc/systemd/system` stays restorable so unit removal can
  roll back, but only as the unit that was removed — a backup cannot be planted
  under an attacker-chosen name, and a protected unit cannot be recreated.
- **`backup_tar` / `file_backup` source confinement** — the path being read must be
  under a managed root, so the operator-readable backups directory cannot be used
  to exfiltrate `/etc/shadow` or a TLS private key via root.
- **Audit correlation** — every helper request logs `plan_id`, `step_id`, `job_id`
  and `requested_by` alongside the op and args, so a root-level action is
  attributable to a DEL user and job rather than matched to `logs/audit.log` by
  timestamp.

### Protected roots

Never deletable, even if referenced in an approved plan:
`/`, `/bin`, `/boot`, `/dev`, `/etc`, `/home`, `/lib`, `/lib64`, `/opt`, `/proc`,
`/root`, `/run`, `/sbin`, `/srv`, `/sys`, `/tmp`, `/usr`, `/var`, `/apps`, `/data`,
and `/apps/del` itself. `path_delete` additionally requires the target to
canonicalize (via `realpath`, no symlink escape) at least one component deep under
an approved deletion root (`/apps`, `/data`, `/srv`, `/var/www`, `/home/bjkai`,
`/etc/nginx/sites-{available,enabled}`, `/etc/systemd/system`, `/etc/cron.d`), to be
absent from the `never_delete` list, and not to be a mountpoint. (The planner
separately only emits paths it derived from an approved plan, but as above, the
helper cannot check that.)

`/home/bjkai` stays on the approved list despite being a home directory: the
`code-server` unit bind-mounts it, so DEL needs to be able to clean up projects
under it the same way it does under `/apps`. This is a known trade-off, not an
oversight — nothing else in `/home` is in scope.

**TOCTOU defense.** `path_delete` and `systemd_rm_unit` both call
`recheck_realpath()` immediately before the destructive subprocess call,
re-resolving the original path/unit-file and refusing if it no longer matches
the realpath computed at validation time. This closes the window between "the
path was validated" and "the path was deleted" during which an attacker with
filesystem control could swap a symlink to point somewhere else.

DEL itself is recorded as `protected=1` in the `applications` table (see
`manifests/del.yaml`, `notes: "Protected application — must never be removable
through DEL"`), and the planner refuses to build a removal plan for it — DEL cannot
remove itself by design, not just by policy.

## Documentation site (`/docs`, `/_next`)

The rendered Fern documentation site (`del-docs.service`, a `fern-api docs dev`
process on 127.0.0.1:8072/8073) is exposed at `https://del.bjk.ai/docs` **without
HTTP basic auth** (open documentation by operator choice, 2026-07-26). Those
paths bypass the app entirely and are proxied straight to the docs dev server.
The docs site serves only static/rendered documentation — it has no access to
the DEL database, helper socket, or app session cookies. The **app UI** at `/`
still requires DEL session login.

The whole-server inventory export at `/miscwork.html` (and `/inventory`) is the
**only** location in the vhost with an `auth_basic` directive — it is HTTP
basic-auth protected via `/etc/nginx/.del-docs-htpasswd` because it contains a full
host inventory dump, not public docs. Despite the file's name, `/docs` does not use
it.

## What is never logged

Environment variable **values** are stripped at the discovery-source layer
(`fs_src.py` and others record `.env` variable *names* only); secrets are never
logged to `helper-audit.log`, the app logs, the `audit_log` table, or job step
output (`output_sanitized`). Callers of `auditlog.audit()` are responsible for
pre-sanitizing details before they're recorded. The Assistant API key is never
logged; assistant audit rows store scope and conversation id, not the prompt text.

## Related host hardening (outside DEL itself)

DEL's docs surface a few server-wide security facts that shape how it correlates
and reports on other apps, even though DEL doesn't own or manage these directly
— see `docs/DEPLOYMENT-CONVENTION.md` for the full house standard:

- **Cockpit** (the systemd/services web UI for non-Docker apps) is bound to
  `127.0.0.1:9091` only — not reachable directly from the internet. The sole
  path in is `https://cockpit.bjk.ai`, an nginx vhost behind HTTP basic auth —
  the same pattern as DEL's inventory export at `/miscwork.html`.
- **`nginx sites-enabled` must contain only symlinks** to `sites-available`
  files — nginx's `include sites-enabled/*;` has no filename filter, so any
  stray regular file or backup left in `sites-enabled` is parsed as a vhost on
  the next reload and can take down every site on the box. Config backups
  belong outside `sites-enabled` (DEL's own `nginx_rm_site`/`file_backup`
  helper ops already do this correctly, writing to `/apps/del/backups`).
- **The `isbd` DNS-update script's IONOS API credential** lives in
  `/etc/isbd.env` (mode `640`, owner `root:bjkai`), sourced by the script at
  `/usr/local/bin/isbd` — not embedded in the script itself.

## Threat model

The original full attacker/vector/mitigation table lives in `docs/server-audit.md`
§14, which is gitignored and present on the deployment host only. Summary:

| Vector | Mitigation |
|---|---|
| Internet → Nginx → auth bypass | TLS termination + security headers (incl. CSP and HSTS with `includeSubDomains`) at Nginx; `del-web` bound to 127.0.0.1 only; a session required on every route except `/login`, `/healthz`, `/favicon.ico` and the `/static/*` assets (`app.css`, `app.js`, `theme-init.js`, `favicon.svg`, `assistant.css`, `assistant.js`); rate-limited login |
| Compromised web session → arbitrary host command | Fixed 22-op helper allowlist with independent re-validation bounds the blast radius regardless of what `del-web` is tricked into requesting |
| Compromised web tier → rewrite what root runs | Helper code and policy are deployed `root:root` outside `/apps/del`; `protected_units` refuses to stop `del-helper` itself |
| Path traversal / symlink escape | `realpath` canonicalization + protected-root refusal + approved-root confinement on every path argument, including backup *reads* and restore *writes* |
| Command/argument injection | subprocess arg-arrays only, `shell=False`, everywhere |
| Secrets exposure | env values stripped at collection; never logged or stored; DB and backups no longer world-readable |
| Assistant → Ollama Cloud | Inventory metadata only (names, paths, tags, evidence); no env values, file contents or secrets; read-only (no helper, no planner); key file 0600 |
| Server-side request forgery via `/app-icon/` | The proxy accepts only hostnames that are enabled Nginx sites in the latest scan, requires a session, fetches `https://{host}/favicon.ico` only, caps the body at 256 KiB, and checks content type |

## Hardening notes

- `del-web.service`: `NoNewPrivileges=yes`, `PrivateTmp=yes`,
  `ProtectKernelModules=yes`, `ProtectKernelTunables=yes`, `ProtectControlGroups=yes`.
- `del-helper.service` runs as root by necessity (systemctl/nginx/docker/rm require
  it) and compensates with the operation allowlist rather than a sandboxed
  `systemd` profile; it still sets what it safely can:
  `ProtectKernelModules=true`, `ProtectClock=true`, `RestrictSUIDSGID=true`,
  `RestrictRealtime=true`, `LockPersonality=true`, `MemoryDenyWriteExecute=true`,
  `SystemCallArchitectures=native`.
- **Response headers**, all set in `config/nginx-del.bjk.ai.conf` with `always`:
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  no-referrer`, `Strict-Transport-Security: max-age=31536000; includeSubDomains`,
  and a real `Content-Security-Policy`:

  ```
  default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self';
  frame-ancestors 'none'; base-uri 'self'; object-src 'none'
  ```

  Two deliberate relaxations: `style-src 'unsafe-inline'`, because a handful of
  templates still carry inline `style=""` attributes (tightening it means moving
  those to classes first); and `img-src data:`, for the inline SVG data URI used in
  some views. Everything else is `'self'` — the client-side tables are a vanilla
  JS/CSS implementation (AG Grid Community is vendored under `/static/vendor/`, no CDN), there are
  no CDN scripts or web fonts, and app icons are proxied through `/app-icon/`
  rather than loaded from a third-party origin, so no external asset origin is
  needed. `script-src 'self'` with no `'unsafe-inline'` is also why the pre-paint
  dark/light theme switch is its own external file, `/static/theme-init.js`,
  rather than an inline `<script>` in `base.html`.
  Note that nginx's `add_header` is not inherited into a `location` block that
  defines its own; none of the blocks in this vhost do, but adding one means
  repeating these directives.
- `/apps/del/config/secret.key` (HMAC signing key) is mode `0600` — keep it that
  way.
- `del-docs.service`: `NoNewPrivileges=true`, runs as `bjkai`. It has **no** basic
  auth in front of it and no systemd sandbox profile beyond that flag; the
  justification is that it serves only static/rendered documentation and holds no
  access to the DEL database, helper socket, or session cookies.
