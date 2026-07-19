# DEL — Internal interface contracts (authoritative for all implementation lanes)

Python 3.10, package root: /apps/del/backend/del_app (import as `del_app`).
Style: pydantic v2 models, type hints everywhere, no ORM (sqlite3 + SQL), stdlib logging.
Dependencies allowed: fastapi, uvicorn, jinja2, pydantic, argon2-cffi, pyyaml, python-multipart, itsdangerous. Nothing else.

## config.py
```python
class Settings(pydantic.BaseModel):
    port: int; db_path: str; manifests_dir: str; backups_dir: str; logs_dir: str
    helper_socket: str = "/run/del/helper.sock"
    session_hours: int = 12; scan_roots: list[str]; protected_apps: list[str] = ["del"]
def get_settings() -> Settings   # loads /apps/del/config/del.toml (tomli), cached
```

## db.py
```python
def get_db() -> sqlite3.Connection        # per-call conn, row_factory=Row, WAL, foreign_keys ON
def run_migrations() -> None              # applies backend/del_app/migrations/NNN_*.sql, tracks in schema_migrations
def q(conn, sql, params=()) -> list[sqlite3.Row]
def x(conn, sql, params=()) -> int        # execute, returns lastrowid, commits
```
Schema: exactly the tables in docs/ARCHITECTURE.md "Data model".

## models.py (pydantic)
Resource(type:str, key:str, display:str, path:str|None, state:str, data:dict)
  resource types: container|image|volume|network|compose_project|nginx_site|systemd_unit|systemd_timer|cron_entry|process|port|directory|git_repo|env_file|tmux_session|bind_mount
Evidence(source:str, statement:str, weight:int)
Association(resource_key:str, resource_type:str, confidence:int, level:str, ownership:str, shared:bool, data_loss_risk:str["none","config","data"], removal_eligible:str["safe","uncertain","blocked"], recommended_action:str, evidence:list[Evidence], excluded:bool=False, approved:bool=False)
AppRecord(slug, name, status, kind, protected:bool, domains:list[str], ports:list[int])
PlanStep(seq:int, stage:str, operation:str, args:dict, description:str, reversible:bool, danger:str["safe","warning","data_loss"])
Plan(id, app_slug, options:dict, steps:list[PlanStep], warnings:list[str], preserved:list[str], manual_followup:list[str], est_reclaim_bytes:int)
levels: confirmed|high|probable|possible|unrelated|manual (thresholds per ARCHITECTURE.md)

Persistence note: plans.options_json stores `options` plus a `"_meta"` key
holding `{warnings, preserved, manual_followup, est_reclaim_bytes}` (the Plan
fields that aren't plan.options itself); `planner.load_plan` pops `_meta`
back out to reconstruct the full Plan.

## discovery/ — each source module exposes
```python
def collect() -> list[Resource]   # read-only, never raises on partial failure (log+skip), strips env VALUES
```
Modules: docker_src (containers, images, volumes, networks; use `docker inspect` via subprocess JSON, socket not required), compose_src (scan settings.scan_roots for compose files, parse with yaml), nginx_src (parse /etc/nginx/sites-enabled + available), systemd_src (`systemctl show`/list-units/list-timers + read custom unit files), proc_src (ss -lntp, ps, tmux ls), cron_src, fs_src (project dirs under scan_roots: du fast estimate, git info, .env var names only).

## correlate.py
```python
def build_apps(resources: list[Resource], manifests: dict[str, Manifest]) -> list[tuple[AppRecord, list[Association]]]
```
Grouping seed = compose project label; then attach nginx sites via proxy port→published port, systemd via WorkingDirectory/ExecStart path match, dirs via compose working_dir/bind mounts, cron via command path. Manifest entries override/augment (level=manual/confirmed). Shared detection: resource associated to >1 app → shared=True on all.

## manifests.py
```python
class Manifest(pydantic.BaseModel): ...   # fields per spec example (id,name,status,domains,compose,repositories,host_paths,systemd_units,nginx,cron,notes, shared:[], excluded:[])
def load_all() -> dict[str, Manifest]; def save(m: Manifest) -> None; def generate_from_app(app, assocs) -> Manifest
```

## scanner.py
```python
def run_scan() -> int  # scan_id: collect all sources, correlate, persist apps/resources/associations, return id
```

## helper_client.py
```python
def call(op: str, args: dict, dry_run: bool = True, timeout: int = 300) -> dict
# JSON over unix socket: request {op,args,dry_run,plan_id?,step_id?}; response {ok:bool, dry_run:bool, output:str, error:str|None, changed:list[str]}
# raises HelperError on transport failure
```

## planner.py
```python
def build_plan(app_slug: str, options: dict) -> Plan
# options: remove_named_volumes:bool, remove_images:str["exclusive","none"], remove_bind_data:bool, remove_repo:bool, backup:str["none","config","full"], remove_networks:bool=True
# Stages order: backup, quiesce, remove_runtime, remove_host, remove_files, validate
# Blocked/possible/excluded/shared-unconfirmed resources → warnings + preserved, never steps.
def persist_plan(plan: Plan, conn: sqlite3.Connection | None = None) -> int   # HMACs steps_json, sets plan.id
def load_plan(plan_id: int, conn=None) -> tuple[Plan, str, str]              # (Plan, stored_hmac, recomputed_hmac); caller must compare
def verify_plan(plan_id: int, conn=None) -> Plan                              # load_plan + raise PlanError on hmac mismatch
```

## jobs.py
```python
def create_job(plan_id: int, mode: str, user_id: int) -> int
def execute_job(job_id: int, confirm_phrase: str | None = None) -> None  # runs in background thread; per-step record before/after; halt on unsafe failure; rollback nginx/systemd from backups on failure; confirm_phrase required for live volume deletion
def job_status(job_id: int) -> dict
def validate_removal(app_slug: str, plan: Plan) -> list[dict]  # post-checks per spec Stage 8
```

## auth.py
```python
def create_user(username, password); def verify(username, password) -> int|None  # argon2id
def login_session(resp, user_id); def require_user(request) -> User  # FastAPI dependency, redirect /login
def csrf_token(session) -> str; def check_csrf(request, submitted: str) -> bool  # on all POST
Rate limit: 5 login attempts/min per IP, in-memory.
```

## auditlog.py
```python
def audit(user_id: int|None, action: str, subject: str, details: dict) -> None  # DB + logs/audit.log line; caller must pre-sanitize
```

## web/ (FastAPI routers + Jinja2)
Routes: /login /logout /(dashboard) /apps /apps/{slug} /apps/{slug}/rescan-approve (POST) /resources /resources/{type} /orphans /apps/{slug}/plan (POST build, GET view) /plans/{id} (GET) /plans/{id}/execute (POST, dry_run|live) /jobs /jobs/{id} /jobs/{id}/status (GET, JSON) /scan (POST) /settings /manifests/{slug} (GET, POST) /healthz (no auth, JSON ok)
Templates in web/templates (base.html + page per route), static in web/static (one app.css dark theme, one app.js ~small: polling job status, confirm dialogs incl. typed phrase for volume deletion).
All pages extend base.html; CSP 'self'; no external assets.

## helper (separate program, stdlib only): /apps/del/helper/del_helper.py
Implements the exact op allowlist + validation rules in docs/ARCHITECTURE.md table. Socket /run/del/helper.sock 0660 root:bjkai. Append-only log /apps/del/logs/helper-audit.log. Every op supports dry_run (returns what WOULD run). Protected roots list per ARCHITECTURE.md. subprocess arg-arrays only, shell=False. Re-validates independently of del-web.
```
