# Changelog

## 2026.9.10 docs

Operator-loop docs page (steps, mermaid, screenshots, collapsible vocabulary).
Overview tabs. Architecture loop diagram. Assistant/confidence accordions.
Mobile CSS for Fern and the app SOP/actions. Unauthenticated `/static/*` claim
matches the files that actually ship.

## 2026.9.10

Ask can name every current owner. App-scope context now includes
`all_owners` / `also_owners` on each association. General context still
lists every volume, image, network, and container with sorted owner slugs
before the app list.

Vocabulary is one meaning per word: probable (≥60) is a strong owner claim;
possible is weak. `/orphans` is review-only. Dashboard SOP has a single
primary scan control. Ask chips open the right rail. AG Grid Ask columns
stay pinned. Docs match the shipped UI (context budget 48000, on-demand
scan, 337 tests).
