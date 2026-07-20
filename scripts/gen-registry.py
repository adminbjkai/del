#!/apps/del/.venv/bin/python
"""Generate docs/PORT-REGISTRY.md — the transparent map of every live subdomain
to its external host port, backend app, deploy type, and internal container
port(s), straight from DEL's latest scan. Run: /apps/del/scripts/gen-registry.py
(or via scripts/gen-registry.sh). Read-only against the DB."""
import sqlite3
import json
import os

DB = os.environ.get("DEL_DB", "/apps/del/database/del.db")
OUT = os.environ.get("DEL_REGISTRY", "/apps/del/docs/PORT-REGISTRY.md")


def main() -> None:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    last = c.execute("SELECT MAX(id) m FROM scans").fetchone()["m"]

    appkind = {a["slug"]: a["kind"]
               for a in c.execute("SELECT slug, kind FROM applications WHERE last_seen=?", (last,))}

    portmap: dict[str, set] = {}
    for r in c.execute(
        """SELECT a.slug, r.data_json FROM resources r
           JOIN associations s ON s.resource_id=r.id
           JOIN applications a ON a.id=s.app_id
           WHERE r.type='container' AND r.last_seen=?""", (last,)):
        d = json.loads(r["data_json"] or "{}")
        for pm in d.get("port_mappings") or []:
            portmap.setdefault(r["slug"], set()).add(f"{pm.get('host')}→{pm.get('container')}")

    rows = c.execute(
        """SELECT r.id, r.data_json,
                  (SELECT a.slug FROM associations s JOIN applications a ON a.id=s.app_id
                   WHERE s.resource_id=r.id ORDER BY s.confidence DESC LIMIT 1) app_slug
           FROM resources r WHERE r.type='nginx_site' AND r.last_seen=?""", (last,)).fetchall()

    # host ports that actually have a listener (from port resources)
    listening = {d["port"] for d in
                 (json.loads(r["data_json"] or "{}")
                  for r in c.execute("SELECT data_json FROM resources WHERE type='port' AND last_seen=?", (last,)))
                 if d.get("port")}

    entries, gaps = [], []
    for r in rows:
        d = json.loads(r["data_json"] or "{}")
        if not d.get("enabled"):
            continue
        ports = sorted({u.get("port") for u in (d.get("upstreams") or []) if u.get("port")})
        app = r["app_slug"] or "—"
        kind = appkind.get(app, "—")
        cports = ", ".join(sorted(portmap.get(app, []))) or "—"
        for sn in d.get("server_names") or []:
            if not sn or sn.startswith("_") or sn[0].isdigit():
                continue
            entries.append((sn, ports, app, kind, cports))
            dead = ports and not any(p in listening for p in ports)
            if app == "—" or dead:
                gaps.append((sn, ports, "no backend attributed" if app == "—" else "dead upstream (nothing listening)"))

    entries.sort()
    gaps.sort()
    seen = set()
    lines = [
        "# DEL — Live Port & Subdomain Registry",
        "",
        f"Auto-generated from DEL scan {last} — regenerate with `/apps/del/scripts/gen-registry.py`.",
        "Do **not** hand-edit. The transparent map of every live subdomain → host port → backend.",
        "",
        "| Subdomain | Host port (external) | Backend app | Deploy type | Container port map (host→container) |",
        "|---|---|---|---|---|",
    ]
    for sn, ports, app, kind, cports in entries:
        if sn in seen:
            continue
        seen.add(sn)
        p = ", ".join(str(x) for x in ports) if ports else "—"
        lines.append(f"| {sn} | {p} | {app} | {kind} | {cports} |")
    lines += ["", f"_{len(seen)} live subdomains._", ""]

    if gaps:
        gseen = set()
        lines += ["## Needs attention (ambiguity to resolve)", "",
                  "| Subdomain | Host port | Issue |", "|---|---|---|"]
        for sn, ports, why in gaps:
            if sn in gseen:
                continue
            gseen.add(sn)
            lines.append(f"| {sn} | {', '.join(str(x) for x in ports) or '—'} | {why} |")
        lines.append("")

    # --- Port conflicts: one host port proxied by subdomains of DIFFERENT
    # apps means only one can actually work. Same-app aliases are fine. ---
    port_owners: dict[int, set] = {}
    port_subs: dict[int, set] = {}
    for sn, ports, app, kind, cports in entries:
        for p in ports:
            port_owners.setdefault(p, set()).add(app)
            port_subs.setdefault(p, set()).add(sn)
    conflicts, aliases = [], []
    for p, owners in sorted(port_owners.items()):
        distinct = {o for o in owners if o and o != "—"}
        if len(distinct) > 1:
            conflicts.append((p, sorted(port_subs[p]), sorted(distinct)))   # real: >1 app on one port
        elif len(port_subs[p]) > 1:
            aliases.append((p, sorted(port_subs[p]), sorted(distinct) or ["unattributed"]))  # same/one app, multiple domains
    if conflicts:
        lines += ["## ⚠️ Port CONFLICTS (one host port, multiple different apps — only one actually serves)", "",
                  "| Host port | Subdomains pointing at it | Distinct apps |", "|---|---|---|"]
        for p, subs, apps in conflicts:
            lines.append(f"| {p} | {', '.join(subs)} | {', '.join(apps)} |")
        lines.append("")
    if aliases:
        lines += ["## Shared ports (aliases — multiple domains, one app; normal)", "",
                  "| Host port | Domains | App |", "|---|---|---|"]
        for p, subs, apps in aliases:
            lines.append(f"| {p} | {', '.join(subs)} | {', '.join(apps)} |")
        lines.append("")

    with open(OUT, "w") as f:
        f.write("\n".join(lines))
    globals()["_conflicts"] = conflicts
    print(f"wrote {OUT}: {len(seen)} subdomains, {len(set(g[0] for g in gaps))} needing attention")


if __name__ == "__main__":
    main()
