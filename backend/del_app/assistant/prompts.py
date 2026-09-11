"""System prompt and the predefined prompt library (docs/ASSISTANT.md
"Prompts"). Prompt ids are a stable API."""
from __future__ import annotations

SYSTEM_PROMPT = """You are the DEL Assistant, a read-only advisor built into DEL, a tool that \
inventories the applications on one Linux server (Docker containers, images, volumes, \
networks, compose projects, systemd units, nginx sites, cron entries, directories, ports) \
and plans their safe removal. You cannot run commands, change anything, or see anything \
beyond the inventory context supplied with each question.

DEL vocabulary:
- confidence (0-100) and level: confirmed (>=95), high (>=80), probable (>=60, strong enough to count as an owner), possible (>=30, weak — review), unrelated; manual = operator manifest. Do not call probable "uncertain".
- ownership (association field): exclusive (only this app), shared (more than one current app), or possible (weak ownership enum — not the confidence band).
- shared: true when more than one current application owns the resource. Never treat shared as exclusive.
- data_loss_risk: none | config | data — "data" means user data lives there.
- removal_eligible: yes | uncertain | no; recommended_action is DEL's own suggestion.
- orphan buckets: actionable (a real cleanup candidate), system (OS/vendor \
infrastructure), expected (known non-orphan situations such as an image a compose \
file still declares).

Hard rules:
1. Answer only from the supplied context. If something is not in it, say so plainly; \
never guess at contents, sizes or owners that are not listed. If a Shared resources or ownership-index section is present, that list is complete for the types it covers — quote every owner slug.
2. Never call a resource "safe to delete" when shared is true or it has more than one \
owner. Call that out explicitly and name every owner.
3. For anything with data_loss_risk: data, state that a backup is required first.
4. Point the operator to the DEL page that performs the action (/apps/<slug>, /orphans, \
/resources/<type>) instead of giving shell commands, unless the operator asks for commands.
5. Be concise. Prefer short bullet lists. Quote resource keys and names exactly as given.
6. /orphans is review-only. Never instruct a delete from that page; send the operator to Ask, a manifest, or /apps/<slug>/plan.
7. You cannot change the host, run plans, or delete anything. If the operator asks you to change something, tell them which DEL page does it.
8. If sections named Shared resources, Stale candidates, Removal-risk ranking, or Protected apps are present, they are complete for those questions — quote them. Do not say shared/stale/risk fields are missing when those sections exist.
"""

# Prompt = {id, scope, label, text, description}. `<type>` in label/text is
# filled from the target at request time (prompt_library()).
PROMPT_LIBRARY: list[dict] = [
    {
        "id": "general.overview",
        "scope": "general",
        "label": "Summarise this server's inventory",
        "text": "Summarise this server's inventory: how many applications, in what state, what "
                "resource types dominate, and anything that stands out.",
        "description": "High-level picture of what is installed.",
    },
    {
        "id": "general.risky",
        "scope": "general",
        "label": "Which apps look most expensive or risky to remove?",
        "text": "Using Removal-risk ranking and Protected apps, which applications look most "
                "expensive or risky to remove? Use shared_assocs, data_risk, protected, warnings, "
                "and resources from the context. Do not say those fields are missing.",
        "description": "Ranks apps by removal risk.",
    },
    {
        "id": "general.stale",
        "scope": "general",
        "label": "Which apps look unused or stale?",
        "text": "Using the Stale candidates section, list unused or stale applications with "
                "status, kind, domains, ports, and resources. That section is complete — do not "
                "say the names are missing.",
        "description": "Candidates for a closer look.",
    },
    {
        "id": "general.shared",
        "scope": "general",
        "label": "What is shared between apps?",
        "text": "Using the Shared resources section, name every shared resource and every owner "
                "slug. That section is complete — do not say shared flags are missing.",
        "description": "Cross-app dependencies.",
    },
    {
        "id": "general.owners",
        "scope": "general",
        "label": "Map every volume/image/network/container to its owners",
        "text": "Using the ownership index, list every volume, image, network and container with "
                "its owner slugs. Call out shared=true, unassigned (owners=0), and data_loss=data. "
                "If the context says it was truncated, say which section was cut.",
        "description": "Complete who-uses-what map.",
    },
    {
        "id": "general.protected",
        "scope": "general",
        "label": "Which apps are protected and why that matters",
        "text": "Which applications are protected=true? Explain that DEL will not build a removal "
                "plan for them, and list any resources they share with unprotected apps.",
        "description": "Protected apps and shared blast radius.",
    },
    {
        "id": "app.explain",
        "scope": "app",
        "label": "Explain what this app consists of",
        "text": "Explain what this application consists of: its resources grouped by type, how "
                "confident DEL is about each, and how the pieces fit together.",
        "description": "Walk through the app's resources.",
    },
    {
        "id": "app.remove_impact",
        "scope": "app",
        "label": "What would removing this app affect?",
        "text": "What would removing this application affect? Call out shared resources, other "
                "apps that depend on them, and anything DEL marks as uncertain.",
        "description": "Blast radius of a removal.",
    },
    {
        "id": "app.data",
        "scope": "app",
        "label": "What data would be lost and what should be backed up?",
        "text": "What data would be lost if this application were removed, and what should be "
                "backed up first? Use data_loss_risk and the volume/bind-mount/directory entries.",
        "description": "Backup checklist before removal.",
    },
    {
        "id": "app.shared",
        "scope": "app",
        "label": "Which of its resources are shared with other apps?",
        "text": "Which of this application's resources are shared with other applications? "
                "Name each shared resource and its other owners.",
        "description": "Shared-resource audit for the app.",
    },
    {
        "id": "app.confidence",
        "scope": "app",
        "label": "Which associations are uncertain and why?",
        "text": "Which of this application's associations are weak (confidence band possible, "
                "ownership=possible, or removal_eligible uncertain)? Probable confidence is a "
                "strong claim, not uncertain. Quote evidence.",
        "description": "Weak associations to verify by hand.",
    },
    {
        "id": "app.trace",
        "scope": "app",
        "label": "Trace this app to every shared resource and other app",
        "text": "Trace this application: for each resource, name ownership, shared, other owner "
                "apps if listed, data_loss_risk, and recommended_action. End with a go / no-go "
                "for building a removal plan (protected = no-go).",
        "description": "End-to-end ownership trace.",
    },
    {
        "id": "orphans.review",
        "scope": "orphans",
        "label": "Review the orphan list and group it",
        "text": "Review the orphan candidate list and group it into sensible clusters (by "
                "likely origin, type, or risk). Summarise each cluster briefly.",
        "description": "Structured overview of the orphans.",
    },
    {
        "id": "orphans.safe",
        "scope": "orphans",
        "label": "Which orphans are clearly safe to remove?",
        "text": "/orphans is review-only — do not tell the operator to delete from that page. "
                "Which actionable candidates look lowest-risk to investigate next (then Ask or "
                "open an app plan), and which need a closer look? Use bucket and data only.",
        "description": "Triage into safe / check-first.",
    },
    {
        "id": "orphans.suspicious",
        "scope": "orphans",
        "label": "Which orphans might belong to an app DEL missed?",
        "text": "Which orphan candidates might actually belong to an application that DEL "
                "failed to correlate? Look at names, paths and compose project hints.",
        "description": "Possible correlation misses.",
    },
    {
        "id": "orphans.reclaim",
        "scope": "orphans",
        "label": "Rank by disk reclaimable",
        "text": "Rank the orphan candidates by how much disk space removing them would reclaim, "
                "using only sizes present in the context; say which have no size information.",
        "description": "Biggest wins first.",
    },
    {
        "id": "rtype.review",
        "scope": "resource_type",
        "label": "Review all <type>s and flag anything shared",
        "text": "Review all <type>s in the inventory and flag anything shared between "
                "applications or with more than one owner.",
        "description": "Full pass over one resource type.",
    },
    {
        "id": "rtype.unused",
        "scope": "resource_type",
        "label": "Which <type>s are unused or dangling?",
        "text": "Which <type>s are unused, dangling, or have no owner? List them with their state.",
        "description": "Unused entries of this type.",
    },
    {
        "id": "rtype.multi_owner",
        "scope": "resource_type",
        "label": "Which <type>s are tied to more than one app?",
        "text": "Which <type>s are tied to more than one application? Name the owners of each.",
        "description": "Multi-owner entries of this type.",
    },
    {
        "id": "res.safe",
        "scope": "resource",
        "label": "Is it safe to delete this? Is it tied to multiple apps?",
        "text": "Is it safe to delete this resource? Is it tied to multiple applications, and "
                "what would be affected?",
        "description": "Deletion safety check.",
    },
    {
        "id": "res.owners",
        "scope": "resource",
        "label": "Who uses this and how confident is DEL?",
        "text": "Who uses this resource, and how confident is DEL about each owner? Quote the "
                "evidence.",
        "description": "Ownership and evidence.",
    },
    {
        "id": "res.contents",
        "scope": "resource",
        "label": "What does this contain and would deleting it lose data?",
        "text": "Based only on the context, what is this resource likely to contain, and would "
                "deleting it lose data? Say clearly what is not known.",
        "description": "Data-loss assessment.",
    },
]


def _type_from_target(scope: str, target: str | None) -> str:
    if not target:
        return "resource"
    if scope == "resource" and ":" in target:
        return target.split(":", 1)[0]
    return target


def prompt_library(scope: str, target: str | None = None) -> list[dict]:
    """Prompts for `scope`, with `<type>` substituted from `target` (the type
    for resource_type, the `<type>:` prefix for resource)."""
    out = []
    for p in PROMPT_LIBRARY:
        if p["scope"] != scope:
            continue
        entry = dict(p)
        if "<type>" in entry["label"] or "<type>" in entry["text"]:
            t = _type_from_target(scope, target)
            entry["label"] = entry["label"].replace("<type>", t)
            entry["text"] = entry["text"].replace("<type>", t)
        out.append(entry)
    return out


def get_prompt(prompt_id: str) -> dict | None:
    for p in PROMPT_LIBRARY:
        if p["id"] == prompt_id:
            return p
    return None
