# Nginx audit notes — 2026-07-19

- Version: nginx version: nginx/1.18.0 (Ubuntu)
- `sudo nginx -t`: PASS (nginx: configuration file /etc/nginx/nginx.conf test is successful)

## Counts
- sites-available: 219
- sites-enabled: 162 (112 symlinks, 50 regular files)
- conf.d: 2 (bjk_dashboard_auth.conf — auth_basic snippet; connection_upgrade.conf — websocket map)
- dead upstream references: 17 across 13 sites
- available-only (never enabled) files: 70 (mostly .bak / .retired / .stale copies)

## Dead upstreams (enabled config proxies to a port with no listener)
- dockhand.bjk.ai: port(s) 9230
- fizzy.bjk.ai: port(s) 8077
- manynotes.bjk.ai: port(s) 8084
- metabase.bjk.ai: port(s) 3001
- netdata.bjk.ai: port(s) 19999
- notecapai.bjk.ai: port(s) 9101
- shows.bjk.ai: port(s) 8335
- stv.bjk.ai.conf: port(s) 8082, 8766
- trflix.bjk.ai: port(s) 8048
- twenty.bjk.ai: port(s) 8036
- viniplay.bjk.ai: port(s) 8998
- zabbix.bjk.ai: port(s) 8300
- zettelgarden.bjk.ai: port(s) 8322, 9321

## sites-enabled hygiene
- 36x: regular file (not symlink); sites-available counterpart exists and is DIFFERENT
- 13x: regular file (not symlink); no sites-available counterpart
- 1x: regular file (not symlink); sites-available counterpart exists and is identical
- Note: 36 enabled regular files DIFFER from their same-named sites-available copy — the live config is the sites-enabled file; sites-available versions are stale.

## Shared upstream ports (multiple hostnames -> same backend)
- :7127 <- fileshare2.bjk.ai, fs2.bjk.ai
- :8022 <- wbd.bjk.ai, willbedone.bjk.ai
- :8029 <- b64pdf2.bjk.ai, pdf64.bjk.ai
- :8045 <- 64pdf.bjk.ai, b64pdf.bjk.ai
- :8055 <- baserow.bjk.ai, n50.bjk.ai, ppv.bjk.ai
- :8086 <- api.boxy.bjk.ai, boxy.bjk.ai
- :8124 <- 2FAuth.bjk.ai, 2fa.bjk.ai
- :18789 <- 192.168.1.164, openclaw.bjk.ai

## Duplicate server_names across enabled configs
- None found.

## Certificates
- Nearly all enabled sites use the wildcard /etc/letsencrypt/live/bjk.ai/ cert.
- Exceptions (3): api.boxy.bjk.ai and docs.boxy.bjk.ai use their own letsencrypt certs; 'openclaw' uses a self-signed /etc/nginx/ssl/openclaw.crt.
- No enabled 443 site is missing an ssl_certificate directive.

## del.bjk.ai
- No config anywhere (sites-enabled, sites-available, or conf.d) references del.bjk.ai. The name is unclaimed.

## Other anomalies
- ssl_protocols in nginx.conf still allows TLSv1/TLSv1.1 (legacy).
- trflix.bjk.ai references dead port 8048 from 3 separate locations; stv.bjk.ai and zettelgarden.bjk.ai each reference 2 dead ports.
