# Removal task — 5 apps (rowboat stack + runtipi stack)

Goal (user, explicit ×3): fully delete rowboat, deck-renderer, llm-proxy, runtipi,
fireshare-migrated and everything associated. "however need be ... i dont want them."
Method: DEL's real staged workflow (planner.build_plan -> jobs.execute_job via root helper).

## Scope / IDs
- rowboat(70), deck-renderer(69), llm-proxy(71)  -> share rowboat_net + rowboat.bjk.ai vhost
- runtipi(17), fireshare-migrated(6)              -> share runtipi_tipi_main_network
- NOT in scope (must survive): fireshare(5), pilotdeck(53), affine, compose, blinko

## Key facts
- fireshare-migrated data lives under /apps/runtipi/.internal -> wiped with runtipi (intended).
- Shared images (preserve; used by out-of-scope apps): e628485c98f8(affine), d6566e93e6a9(compose), a209aced4fa1(blinko).
- Named volumes destroyed (DATA LOSS): rowboat_uploads, runtipi_pgdata + 2 anon hashes.
- Runtipi system bind mounts (/var/run/docker.sock,/etc/*,/proc/*) auto-skipped by protected-path guard.
- Shared resources can't be removed in pass 1 -> 2-pass: remove apps -> rescan -> mop up rowboat_net, runtipi_tipi_main_network, rowboat.bjk.ai vhost.

## Options per app
backup=config, remove_named_volumes=True, approved_volumes=<app's vols>,
remove_images=exclusive, remove_bind_data=True, remove_repo=True, remove_networks=True.

## Order
llm-proxy -> deck-renderer -> rowboat -> fireshare-migrated -> runtipi -> rescan -> mop up -> verify.

## Status — COMPLETE (all 5 removed, verified against live docker/fs/nginx)
- [x] llm-proxy   removed (job 117: container, exclusive image, /apps/llm-proxy)
- [x] deck-renderer removed (job 122: container, image 1b7c, /apps/deck-renderer)
- [x] rowboat     removed (job 126, 18 steps: 4 containers, compose down, rowboat_net,
      vols rowboat_uploads+anon, exclusive imgs add05/4f6c, nginx rowboat.bjk.ai+reload, /apps/rowboat)
- [x] fireshare-migrated removed (job 127, 9 steps: container, runtipi_tipi_main_network,
      image cf863, 4 data dirs under /apps/runtipi/.internal)
- [x] runtipi     removed (job 128: /apps/runtipi; its containers/pgdata already gone pre-task)
- [x] shared networks gone: rowboat_net, runtipi_tipi_main_network (auto once un-shared)
- [x] shared vhost rowboat.bjk.ai gone; runtipi.bjk.ai gone; `nginx -t` ok
- [x] PRESERVED (correct): shared base images redis/mongo/postgres (affine/compose/blinko dep)
- [x] UNTOUCHED: fireshare(id5), pilotdeck, affine, blinko — all still running
- [x] final scan 116: none of the 5 re-detected (all show removed in DEL UI)

## Lesson learned
jobs.py:257 auto-rescans after every LIVE job -> running jobs back-to-back advances
MAX(scans) mid-batch and collapses later apps' plans (build_plan filters last_seen==MAX).
Fix used: patch del_app.scanner.run_scan to no-op in the driver, run ONE scan up front,
remove serially. Considered a real product bug worth noting for batch removals.
