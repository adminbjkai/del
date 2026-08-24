# Remediation Plan — DEL Host Audit (2026-07-21)

Companion to `AUDIT-REPORT.md`. Nothing in this plan has been executed — this audit was read-only throughout. Section A items are low-risk and mechanical enough to auto-apply once approved; Section B items involve a real decision (start vs. remove an app, or a change with side effects) and need the owner's call first.

---

## A. SAFE to auto-apply

These are mechanical, low-risk, and reversible. Each includes the exact command(s).

### A1. Remove the orphaned boxbox.bjk.ai nginx site
**What:** Disable and remove the leftover nginx site for the deleted BoxBox app.
**Why:** `/apps/boxbox` no longer exists and nothing listens on :8345; the site currently 502s for any visitor and is pure leftover from this session's deletion.
**Command:**
```bash
sudo rm /etc/nginx/sites-enabled/boxbox.bjk.ai
sudo nginx -t
sudo systemctl reload nginx
# after confirming no issues:
sudo rm /etc/nginx/sites-available/boxbox.bjk.ai
```
**Risk:** low. `nginx -t` before reload catches any syntax issue; reload (not restart) causes no dropped connections to other sites.

### A2. Remove the stale nginx.conf.bak
**What:** Delete (or move to a backups directory) `/etc/nginx/nginx.conf.bak`.
**Why:** Dated 2023-05-30, drifted from the live config, not loaded by nginx under any circumstance — pure hygiene.
**Command:**
```bash
sudo rm /etc/nginx/nginx.conf.bak
# or, to keep a copy: sudo mv /etc/nginx/nginx.conf.bak ~/backups/nginx.conf.bak.2023-05-30
```
**Risk:** none — file is inert.

### A3. Delete the 25 ufw rules with zero matching listener
**What:** Remove stale `ufw ALLOW` rules for ports: 55, 3001, 3004, 3052, 5000, 5005, 5006, 5007, 5008, 5174, 5555, 7005, 7071, 8097, 8098, 8100, 8101, 8123, 8443, 8800, 8922, 8923, 9001, 9090, 9100.
**Why:** Nothing listens on any of these ports (checked against full `ss -ltnp`/`ss -lunp`, all bind addresses). They are standing invitations — if anything ever binds one of these ports later (new container, misconfigured service), it becomes instantly reachable from Anywhere with zero additional firewall change.
**Command (per rule, using numbered listing to avoid renumbering mistakes):**
```bash
sudo ufw status numbered      # find each rule's current number
sudo ufw delete <rule-number> # repeat per rule, re-running `status numbered` after each delete since numbers shift
```
**Risk:** low, but not zero — flagged here as "safe to auto-apply" only in the sense that removing a rule for a port nothing is using cannot break a currently-working flow. Recommend applying one at a time and re-verifying `ss -ltnp`/`ss -lunp` show nothing bound before each delete, since ufw rule numbers shift after every deletion.

### A4. Move/delete the 7.56GB stale backup buried in /apps/netdata
**What:** Move `/apps/netdata/apps-backup-2026-03-12.tar.gz` (7.56GB) off the live app tree, or delete if superseded.
**Why:** netdata itself is not running; this file accounts for nearly all of the directory's 7.4G footprint and is pure disk waste sitting inside an inactive app's folder.
**Command:**
```bash
mkdir -p ~/backups/apps-archive
mv /apps/netdata/apps-backup-2026-03-12.tar.gz ~/backups/apps-archive/
# or, if a newer full-/apps backup already supersedes it: rm /apps/netdata/apps-backup-2026-03-12.tar.gz
```
**Risk:** none, as long as it's moved (not deleted) unless the owner confirms a newer backup supersedes it.

### A5. Consolidate the ~1.9GB of loose backup/zip files at /apps root
**What:** Move the 14 loose backup/zip/bundle files at `/apps/` root (paperbanana_backup.zip, Cap.zip, nanobanana.zip, boxy-backup-20260703.bundle, bjk-flix-*.zip, etc. — full list in `lane_filesystem.md`) into a dedicated archive location.
**Why:** Disk hygiene; several duplicate a still-live app directory of the same name.
**Command:**
```bash
mkdir -p ~/backups/apps-archive
mv /apps/paperbanana_backup.zip /apps/paperbanana_repo.zip /apps/Cap.zip \
   /apps/netdata-backup-2026-03-12.tar.gz \
   /apps/2026-03-31T07-41-43.147Z-openclaw-backup.tar.gz \
   /apps/nanobanana.zip /apps/ocrai.zip /apps/b64pdf.zip /apps/b64pdf2.zip \
   /apps/cap42.zip /apps/boxy-backup-20260703.bundle \
   /apps/bjk-flix-androidtv.zip /apps/bjk-flix-appletv-tvos.zip /apps/bjk-flix-pixel-android.zip \
   ~/backups/apps-archive/
```
**Risk:** none — purely archival move, nothing reads these files from `/apps` root at runtime.

---

## B. NEEDS USER DECISION

These require a judgment call (start vs. remove, or a change with real side effects). Do not auto-apply.

### B1. [HIGH] Fix kanbu's crash-looping API — Docker Compose project-name collision with karakeep
**What:** kanbu and karakeep both live in directories literally named `docker` (`/apps/kanbu/docker`, `/apps/karakeep/docker`), so Compose merged them into one project (`docker`), and kanbu's `postgres` service was never created — its API has been crash-looping (RestartCount 518+) since deployment.
**Decision needed:** confirm you want kanbu's backend actually working (vs. leaving it as static-frontend-only), since fixing this means stopping/recreating containers.
**Command (once approved):**
```bash
# Stop the misconfigured merged project's kanbu-owned containers
docker stop docker-api-1
docker rm docker-api-1
# Redeploy kanbu under its own explicit project name so it gets its own postgres service
cd /apps/kanbu/docker
docker compose -f docker-compose.selfhosted.yml -f docker-compose.prod.yml -p kanbu up -d
# Then clean up the leftover karakeep-labeled duplicates still in the old 'docker' project
docker stop docker-web-1 docker-meilisearch-1 docker-chrome-1
docker rm docker-web-1 docker-meilisearch-1 docker-chrome-1
# (karakeep's real containers, karakeep-web-1/-meilisearch-1/-chrome-1, are untouched and keep running)
```
**Risk:** medium — kanbu.bjk.ai will have brief downtime during the recreate; verify the new `kanbu-web` container is created and nginx's `proxy_pass http://127.0.0.1:8340` still resolves to it before considering this done.

### B2. [HIGH] Re-register semalist.bjk.ai with PM2 before the next reboot
**What:** `pm2-root.service`'s persisted `dump.pm2` is empty; semalist.bjk.ai is running live but will not survive a reboot.
**Decision needed:** none really — this is a pure availability fix — but it's a root-level PM2 write action against a live production daemon, so flagging for a deliberate, confirmed run rather than blind automation.
**Command (as root):**
```bash
sudo pm2 list                 # confirm semalist's current pm2 id/name (may show 0 apps — see below)
# If semalist doesn't appear in `pm2 list` despite being alive, it may need to be re-imported:
sudo pm2 start /apps/semalist/<entrypoint or ecosystem file> --name semalist   # only if not already tracked
sudo pm2 save                 # persists whatever pm2 currently supervises to dump.pm2
cat /root/.pm2/dump.pm2       # verify semalist now appears
```
**Risk:** low, but do this deliberately and verify `dump.pm2` actually contains a semalist entry afterward — if `pm2 save` alone doesn't pick up a daemon-orphaned process, a `pm2 start` may be needed first, which could very briefly restart the process.

### B3. [MEDIUM] Bring blinko-postgres back and reconcile its restart policy
**What:** `blinko-postgres` exited 31 hours ago; the live container's restart policy (`no`) no longer matches what `/apps/blinko/docker-compose.deploy.yml` specifies (`restart: always`).
**Decision needed:** confirm blinko's data is fine to reattach (no reason to think otherwise — clean exit code 0) before restarting the DB the whole app depends on.
**Command:**
```bash
docker start blinko-postgres
# Reconcile so the restart policy actually matches the compose file going forward:
cd /apps/blinko
docker compose -f docker-compose.deploy.yml up -d postgres
# Then verify app functionality (login, note save) since blinko-website's healthcheck doesn't check DB connectivity
```
**Risk:** low — verify blinko app functionality afterward since its own healthcheck won't tell you if this worked.

### B4. [MEDIUM] shows.service — investigate then restart or decommission
**What:** `shows.service` is enabled but has been dead since 2026-07-04 (killed by SIGTERM; `Restart=on-failure` doesn't cover a clean TERM).
**Decision needed:** was this an intentional stop (in which case, decommission: disable the unit and remove the nginx site) or an unnoticed crash (in which case, restart it and consider `Restart=always`)?
**Command:**
```bash
# Investigate first:
journalctl -u shows.service --since "2026-07-04 22:00" --until "2026-07-04 22:35"
# If it should be running again:
sudo systemctl start shows.service
# Optionally harden against future manual/OOM stops:
sudo systemctl edit shows.service   # add Restart=always under [Service]
# If it should be retired instead:
sudo systemctl disable --now shows.service
sudo rm /etc/nginx/sites-enabled/shows.bjk.ai   # only after confirming decommission
```
**Risk:** low either way; the ambiguity is the "why did it stop" question, not the mechanics.

### B5. [MEDIUM] htmls.bjk.ai — replace the stray unsupervised process with its real systemd unit
**What:** `htmls-webapp.service` exists and is disabled; `htmls.bjk.ai` is actually being served by a stray, manually-started `node server.js` (PID 3584797) with no crash recovery.
**Decision needed:** confirm the disabled unit is functionally equivalent to what the stray process is running before cutting over (same code path, same env vars/PORT).
**Command:**
```bash
# Compare first — confirm the unit would start the same app the same way:
cat /etc/systemd/system/htmls-webapp.service
ps -o cmd,cwd -p 3584797
# Once confirmed equivalent:
kill 3584797                                   # stop the stray process
sudo systemctl enable --now htmls-webapp.service
# Verify htmls.bjk.ai still loads correctly afterward
```
**Risk:** medium — there will be a brief gap between killing the stray process and the systemd unit binding the port; do this in a low-traffic window and verify immediately after.

### B6. [MEDIUM] Flussonic direct-port exposure (bypasses nginx/TLS)
**What:** Port 8050 (Flussonic, backing flussonic.bjk.ai) is reachable both through nginx and directly, unencrypted, on `0.0.0.0:8050`.
**Decision needed:** does Flussonic need a wildcard bind for some other reason (e.g. RTMP ingest on a different port needs it, or an external client connects directly)? If not:
**Command:**
```bash
# Option 1 (preferred): rebind the Flussonic process/config to 127.0.0.1 for its HTTP port, restart it
# (exact config location depends on Flussonic's own config file — inspect before editing)
# Option 2 (if it must stay wildcard-bound): narrow the firewall instead
sudo ufw delete allow 8050
sudo ufw allow from <trusted-cidr> to any port 8050
```
**Risk:** low, but verify nothing external depends on direct :8050 access before narrowing/rebinding.

### B7. [MEDIUM] Restrict Samba and NoMachine to trusted source IPs (match SSH's posture)
**What:** Samba (139/445) and NoMachine (4000 tcp+udp) are `ufw ALLOW Anywhere`, unlike SSH which is already locked to 2 specific IPs.
**Decision needed:** confirm which source IPs/CIDRs should retain access (presumably the same as SSH, or a LAN/Tailscale CIDR) — this will cut off any client currently connecting from an untrusted IP.
**Command:**
```bash
sudo ufw delete allow Samba
sudo ufw allow from 72.80.59.32 to any app Samba
sudo ufw allow from 174.197.134.148 to any app Samba
sudo ufw delete allow 4000
sudo ufw allow from 72.80.59.32 to any port 4000 proto tcp
sudo ufw allow from 72.80.59.32 to any port 4000 proto udp
sudo ufw allow from 174.197.134.148 to any port 4000 proto tcp
sudo ufw allow from 174.197.134.148 to any port 4000 proto udp
```
**Risk:** low technically, but will lock out any device connecting from outside those IPs (e.g. LAN clients) — confirm the actual intended source set with the owner before applying, since it's plausibly LAN devices using Samba, not just the two SSH source IPs.

### B8. [MEDIUM] Rebuild the stale miscwork.html inventory export
**What:** `del-inventory.template.html` was edited (cropping fix, de-pilled chips, clickable domain links) but there's no script that renders template → `del-inventory.html`, so the live page at `del.bjk.ai/miscwork.html` is missing all three fixes.
**Decision needed:** none functionally — this is a straightforward fix — but it's a code change (new script) to the DEL codebase, so flagging for review rather than silent auto-apply.
**Command (after writing `/apps/del/miscwork/render_template.py` per the audit's recommendation):**
```bash
cd /apps/del/miscwork
python3 extract.py                # del.db -> inventory.json
python3 render_template.py         # inventory.json + del-inventory.template.html -> del-inventory.html
python3 build_workbook.py          # inventory.json -> del-inventory.xlsx
python3 inline_build.py            # del-inventory.html -> miscwork.html (ag-grid inlined)
```
**Risk:** low — but write and test `render_template.py` in isolation before wiring it into the regular chain; also update `OPERATIONS.md`'s "Whole-server inventory export" section to name this new step.

### B9. [LOW] Decide fate of 6 down-but-on-disk apps (dockhand, fizzy, netdata, notecapai, trflix + shows already covered in B4)
**What:** These app directories exist with compose files/units on disk but nothing is running; their nginx routes 502.
**Decision needed:** per app — restart it, or formally decommission (stop enabling, remove nginx site, archive directory)?
**Command (per app, once decided):**
```bash
# To bring back (example for dockhand):
cd /apps/dockhand && docker compose -f docker-compose.yaml up -d
# To decommission instead:
sudo rm /etc/nginx/sites-enabled/dockhand.bjk.ai && sudo nginx -t && sudo systemctl reload nginx
mv /apps/dockhand ~/backups/apps-archive/dockhand-decommissioned-2026-07-21
```
**Risk:** low, but `notecapai` has 35 uncommitted git changes since 2026-07-06 — do not archive/delete it without confirming that work is either committed or genuinely disposable first.

### B10. [LOW] Decide fate of openphotos.bjk.ai (pre-existing orphan, same shape as boxbox)
**What:** Enabled nginx site, proxies to :8777, `/apps/openphotos` doesn't exist, nothing listens.
**Decision needed:** confirm this app was intentionally removed previously (not part of this session) before removing the site — same command pattern as A1, but not marked auto-safe since it precedes this session's known deletion list and wasn't explicitly requested.
**Command:**
```bash
sudo rm /etc/nginx/sites-enabled/openphotos.bjk.ai
sudo nginx -t && sudo systemctl reload nginx
sudo rm /etc/nginx/sites-available/openphotos.bjk.ai
```
**Risk:** low.

### B11. [LOW] DEL codebase: pm2 app-detection gap and systemd parser bug
**What:** (a) pm2-managed apps (semalist, trp, 17imgshare per `docs/SYSTEM-STATE.md`) are structurally invisible to DEL's Apps list; (b) `systemd_src.py`'s batched `systemctl show` parser misattributes `ExecStart` across adjacent SysV-shimmed units.
**Decision needed:** these are code changes to DEL itself — worth scheduling as normal development work, not an audit auto-fix.
**Recommendation:** (a) write `manifests/semalist.yaml` and `manifests/trp.yaml` per the already-existing but never-actioned recommendation in `SYSTEM-STATE.md`, and/or add a `pm2_src.py` discovery source using `pm2 jlist`; (b) change `systemd_src.py`'s `_show_units()` to query `ExecStart` per-unit instead of trusting positional accumulation in the batched call.
**Risk:** none to the running system — purely a code-quality fix in DEL's own scanner, do as normal dev work.

### B12. [INFO — confirm intent, no urgent fix] A few items needing only a yes/no from the owner
- **AionUi's stray `*:3000` listener** (electron-forge dev port, ufw-restricted to 1 IP) — confirm intentional or drop the ufw rule.
- **automation-stack_metabase_data volume** with no running metabase container — confirm Metabase was intentionally decommissioned (then volume can be removed) or should be started.
- **anisette / nextcloud-aio-talk wide binds** (0.0.0.0) — confirm these are intentional given their function (client discovery / WebRTC TURN).
- **fireshare.bjk.ai / colanode-api.bjk.ai** disabled sites — confirm they're meant to stay parked (no action needed if so).

---

## Summary counts

- Section A (safe to auto-apply): 5 items
- Section B (needs user decision): 12 items (2 high, 6 medium, 3 low, 1 info-only confirm-batch)
