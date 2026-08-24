# DEL — Uninstall

DEL is a protected application: it refuses to plan or execute its own removal
through its own UI/job engine (`protected=1` in its own `applications` record, and
the planner rejects the app slug `del` outright). **There is no button or API call
in DEL that removes DEL.** To remove DEL entirely, do it manually, on the host,
outside of DEL — exactly like removing any other host-managed systemd + nginx
service.

## Manual removal steps

1. **Stop and disable all three units:**
   ```bash
   sudo systemctl disable --now del-web.service
   sudo systemctl disable --now del-helper.service
   sudo systemctl disable --now del-docs.service
   ```
2. **Remove the unit files:**
   ```bash
   sudo rm -f /etc/systemd/system/del-web.service
   sudo rm -f /etc/systemd/system/del-helper.service
   sudo rm -f /etc/systemd/system/del-docs.service
   sudo systemctl daemon-reload
   ```
3. **Remove the nginx site** (this one file's `server{}` block holds the app
   location, plus the `/docs` and `/_next` documentation-site locations —
   removing it removes both):
   ```bash
   sudo rm -f /etc/nginx/sites-enabled/del.bjk.ai
   sudo rm -f /etc/nginx/sites-available/del.bjk.ai
   sudo nginx -t
   sudo systemctl reload nginx
   ```
   (Run `nginx -t` before reloading — if it fails, something else references the
   removed file; fix that before reloading.)
4. **Remove the inventory export's basic-auth password file.** Despite the name,
   this file protects only `/miscwork.html` (and its `/inventory` alias); `/docs`
   has no `auth_basic` directive at all:
   ```bash
   sudo rm -f /etc/nginx/.del-docs-htpasswd
   ```
   (The plaintext copy at `/apps/del/config/docs-basic-auth-password.txt` is
   removed along with the rest of the project tree in step 6.)
5. **Remove the deployed privileged helper and its policy.** These live outside
   `/apps/del` on purpose — root executes them, so they are installed `root:root`
   where the web user cannot write them — and are therefore *not* covered by
   removing the project tree:
   ```bash
   sudo rm -rf /usr/local/lib/del-helper
   sudo rm -rf /etc/del                # holds only helper-policy.json
   ```
6. **Remove the application tree and its `/opt` symlink** (this includes the
   Fern docs sources under `/apps/del/fern/`, which back `del-docs.service`):
   ```bash
   sudo rm -f /opt/del                # symlink only, not a copy of the tree
   rm -rf /apps/del                   # the actual project root, including database/, backups/, logs/, manifests/, fern/
   ```
   Take a final `del-admin backup-db` snapshot and copy anything you want to keep
   out of `/apps/del/backups` **before** this step — it deletes the backups
   directory too.
7. **Remove the runtime socket directory** (usually cleaned up automatically when
   the helper unit stops, since it's `RuntimeDirectory=del` under tmpfs, but
   confirm):
   ```bash
   sudo rm -rf /run/del
   ```
8. Optionally remove the `del.bjk.ai` DNS record in IONOS if it's no longer needed
   (out of scope for host-level cleanup, done in the IONOS control panel).

No other application, container, compose project, systemd unit, or cron entry is
touched by these steps — DEL's install was additive-only, and removal is the exact
inverse: the three units, the one nginx site, the deployed helper under
`/usr/local/lib/del-helper` + `/etc/del`, and the one directory tree.
