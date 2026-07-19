# Processes / Ports / Sessions Audit — 2026-07-19

Source data: `/apps/del/data/audit/processes.json` (read-only inventory; cmdlines redacted).

## Counts

| Metric | Value |
|---|---|
| Listening sockets (TCP+UDP) | 276 |
| Distinct listening ports | 192 |
| Owner: systemd services | 140 sockets (41 distinct units) |
| Owner: docker (docker-proxy / container netns procs) | 125 sockets |
| Owner: system (sshd, pid-1 misc) | 3 sockets |
| Owner: manual (no systemd unit, not docker) | 8 sockets |
| Long-running (>1h) non-container interpreters | 38 |
| tmux sessions | 1 (`cai-0`, 1 window/1 pane, detached, bash at `/apps`) |
| screen sessions | 0 (screen not installed; /run/screen empty) |

## Publicly exposed ports (bound 0.0.0.0 / :: / *) — 39 sockets, 24 distinct ports

| Port | Proto | Owner | What |
|---|---|---|---|
| 22 | tcp | ssh.service | sshd |
| 80, 443 | tcp | nginx.service | main reverse proxy |
| 139, 445 | tcp | smbd.service | **Samba exposed on all interfaces** — verify firewall; SMB should not be internet-facing |
| 1935, 8050 | tcp | flussonic.service | Flussonic streamer (RTMP + HTTP) |
| 3000, 9000 | tcp | aionui-webui.service | AionUi electron-forge dev-style server running as **root**, publicly bound |
| 3015 | tcp | sema-shopping.service | next start (root) |
| 3019 | tcp | semashop.service | next start (root) |
| 3020 | tcp | pm2-root.service | next-server /apps/semalist (root pm2) |
| 3478 | tcp+udp | docker: nextcloud-aio-talk | TURN/STUN (expected public) |
| 3901, 3911 | tcp | boxy-docs.service | fern docs dev server, public |
| 4000 | tcp+udp | nxserver.service | NoMachine NX |
| 5353 | udp | multiple: nxserver, adb (manual), docker netmuxd | mDNS — 3 different processes bound; adb one is manual (pid 195052, cwd /apps/glmflix) |
| 6556 | tcp | checkmk-agent.socket | Checkmk agent (systemd socket activation) |
| 6969 | tcp | docker: anisette | anisette server public — check if intended |
| 9091 | tcp | cockpit.service | Cockpit web console public |
| 21118, 21119, 37617 | tcp/udp | rustdesk.service | RustDesk relay/rendezvous |
| 41641 | udp | tailscaled.service | Tailscale WireGuard (expected) |

Notable: only 8 of the 125 docker sockets are public (nextcloud-talk 3478, anisette 6969, netmuxd mdns); everything else docker publishes on 127.0.0.1 — good pattern. The public risk surface is mostly **systemd units**: Samba, Cockpit, NoMachine, dev-style Next/fern/electron-forge servers running as root.

## Manual / unclassified processes (no systemd unit, not docker)

| Port | PID | Process | cwd | Note |
|---|---|---|---|---|
| 3456 (127.0.0.1) | 263585 | node | /apps/many-notes | **nohup orphan (ppid 1), up 671 h** — actually `claude-code-router` CLI, not many-notes |
| 5037 + 5353 + udp 57375 | 195052 | adb server | /apps/glmflix | manually started adb daemon; 5353 is public mDNS |
| 8087 (127.0.0.1) | 1178013 | gunicorn | /apps/17imgshare | manual gunicorn, no unit — behind nginx presumably |
| 8195 (127.0.0.1) | 3584797 | node server.js | /apps/htmls | **nohup orphan, up 787 h** |
| 19988 (127.0.0.1) | 3080178 | playwriter-ws-server | /apps/gongyu | manual Playwright websocket server |

## Long-running non-container interpreters (38 total)

- Most belong to legitimate systemd units (jsonp, txtshr, 17tube, vncend, bdl-bjk, claudeclaw-web, xtr-dashboard, aionui-webui x10 procs, appletv/astv-remote, n50-runner, boxy-docs, flussonic helper, openclaw-proxy, notex-image-bridge, sema/semashop).
- **nohup-style orphans (ppid 1, no unit):**
  - pid 3584797 — `node server.js` in /apps/htmls, 787 h, listens 127.0.0.1:8195.
  - pid 263585 — `claude-code-router` start, cwd /apps/many-notes, 671 h, listens 127.0.0.1:3456.
  - pid 923744 — `npx fern docs dev --port 3901` in /apps/boxy, 36 h — **duplicate of boxy-docs.service** (pid 4012899 runs the same command under systemd); the orphan likely lost the port race or is stale.
- 2 bare shells run under tmux/session leaders (bash in /apps, 1069 h and 65 h) — idle interactive shells.
- user@1000.service hosts a manual worker: pid 3322729 `node apps/worker/dist/index.js` (cap4 worker, 238 h) running in the user session rather than a unit — survives only while user session lingers.

## Anomalies / follow-ups

1. **Samba (139/445) and Cockpit (9091) bound to 0.0.0.0** — confirm ufw/cloud firewall blocks them externally.
2. **Root-owned dev servers public**: aionui-webui (electron-forge + webpack ts-checker workers as root, ports 3000/9000), sema/semashop `next start` as root, boxy fern docs dev on 3901/3911. Dev tooling serving production traffic as root.
3. **Duplicate boxy fern process**: systemd unit boxy-docs.service AND an orphaned npx copy of the same command both alive.
4. **Two mDNS stacks + adb on port 5353** (nxserver, netmuxd container, manual adb) — harmless but noisy; adb server itself (5037) left running from /apps/glmflix work.
5. **Orphans with 670–790 h uptime** (htmls server.js, claude-code-router) should be adopted into systemd units or stopped (no action taken — read-only audit).
6. Port 6556 = checkmk-agent.socket (systemd socket activation, shows as pid 1).
7. tmux session `cai-0` created 2026-06-05, detached, single idle bash pane at /apps — likely forgotten.
