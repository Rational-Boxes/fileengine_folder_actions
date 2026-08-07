# folder_actions

FastAPI service that binds custom, extensible **actions** to a **virtual folder** in
the FileEngine virtual file system. When a recognized event happens to a file inside a
bound folder, folder_actions runs the folder's configured actions — move, notify,
auto-sort, webhook, or any plug-in action. See [`SPECIFICATIONS.md`](SPECIFICATIONS.md)
for the full contract.

Structurally a twin of `discussion_threaded_communication` and `convert_search_ai`:
per-tenant Postgres, trusted-upstream gRPC to the core, and a Redis-Streams event
consumer — reusing their config/auth/db/mailer/directory scaffolding.

## Status

**Implemented** (2026-08-06) on `feature/folder-actions`. Verified end-to-end against a
live stack: a published `fileengine:events` entry flows through folder matching → a
plug-in → a recorded `action_run`; unit tests cover matching, the classifier scorer, and
MIME matching. Frontend UI (drawer Actions tab, generic binding editor, classifier admin)
lives in the `frontend` repo (`feature/folder-actions-ui`, §12).

Depends on two small, additive upstream changes (§4), each on its own branch:
`convert_search_ai` emits `conversion.complete` / `conversion.failed`; `discussion`
adds explicit `review.approved` / `review.rejected` and promotes collaboration events onto
the shared `fileengine:events` stream. folder_actions degrades gracefully without them
(the sorter and review-driven actions simply never fire).

## What it does

An admin binds one or more actions to a folder; the consumer reacts to the recognized
event stream. Five built-in actions (each an in-process plug-in — extend via the
`folder_actions.actions` entry-point group, §6):

- **Move on review approve/reject** — move a file when its review is approved/rejected.
- **Notify user or group** — real-time email to users / role members, optionally
  rendered from a reusable event-notification template.
- **Automatic sorter** — classify a file from its extracted Markdown (the vendored,
  deterministic SmolDocBot scorer) and move it to a destination by score threshold; a
  bound folder becomes an **inbox** (move a file in → auto-filed).
- **Webhook** — POST to a remote (bearer / OAuth2 client-credentials), with an
  admin-authored context bag and a `move_to` / `metadata` response contract; optional
  scoped read-back of file content/renditions.
- **Raise a review** — auto-request a review on an added file to specified reviewers;
  composes with move-on-approve/reject into **chains across folders**.

Every binding carries two **binding-level filters** applied to any action — the
trigger `on_events` and a content-sniffed **MIME-type whitelist**. Plug-ins that move
files unattended (sorter, webhook) declare **`auto_moves`** in their manifest so the
consumer guards them against move loops.

## How it works

- **Events (§3):** consumes the single recognized `fileengine:events` Redis stream with a
  private consumer group (at-least-once, dedupe on `event_id`). Folder scoping matches an
  event's `parent_uid`/anchored file's parent against the bound folder (optional recursive
  subtree). Loop-safe: the service ignores `file.moved` events actored by its own principal.
- **Actions (§6):** each plug-in declares a `type_name`, `supported_events`, a pydantic
  `ConfigModel`, and **`config_fields()`** — a typed FieldDescriptor list so a generic
  frontend renders its form with no bespoke UI. All core mutations run as a dedicated
  **service principal** (§7.5), authorized at binding time, not at event time.
- **State (§10):** its own per-tenant Postgres — `action_binding`, `classifier_set` /
  `classifier` / `classifier_term`, `sorter_route`, encrypted `webhook_secret`, and an
  idempotent `action_run` log.
- **Auth:** the admin API enforces folder ACLs *as the calling user* (core
  `CheckPermission`); the webhook secret box encrypts credentials at rest.

## Processes

One image, three console entry points (`[project.scripts]`):

| Script | Role |
|---|---|
| `folder-actions` | FastAPI admin API (**:8099**) — bindings CRUD, `GET /action-types`, sorter routes, classifier editor, run log, health/ready |
| `folder-actions-consumer` | recognized-event worker — matches events and runs actions |
| `folder-actions-reconcile` | periodic reconcile sweep (catch-up after outages/retention gaps) |

## Configuration

Environment only (see [`.env.example`](.env.example)). Shared cross-service keys keep the
`FILEENGINE_*` prefix (gRPC, LDAP, Redis, `FILEENGINE_EVENTS_STREAM`, `FILEENGINE_JWT_SECRET`);
service-private knobs use `FA_*` (`FA_PG_*`, `FA_HTTP_PORT`, `FA_CSAI_BASE_URL`,
`FA_ENABLED_ACTIONS`, `FA_SECRET_KEY`, `FA_SMTP_*`). The action principal is
`FILEENGINE_FA_USER` / `FILEENGINE_FA_PASSWORD` / `FILEENGINE_FA_TENANT`.

## Layout

```
folder_actions/
  pyproject.toml  Containerfile  .env.example  SPECIFICATIONS.md
  migrations/0001_baseline.sql          # DB-wide bootstrap (per-tenant tables are code-provisioned)
  src/folder_actions/
    app.py api.py classifier_api.py classifier_io.py   # FastAPI + admin surface
    config.py db.py schema.py stores.py                # config + per-tenant Postgres
    core_client.py csai_client.py mime.py secrets.py   # core/CSAI clients, MIME sniff, secret box
    directory.py mailer.py                             # LDAP role→email, SMTP
    _client.py ldap_auth.py http_auth.py bridge_auth.py jwt_verify.py deps.py token_store.py netutil.py failover.py
    events.py consumer.py matching.py reconcile.py     # event consume + folder match + workers
    classifier.py                                      # vendored SmolDocBot scorer
    plugins/  base.py move_review.py notify.py sorter.py webhook.py
  src/tests/
```

## Run (dev)

```bash
pip install -e ../python_interface        # the fileengine gRPC client
pip install -e '.[dev]'
cp .env.example .env                       # fill in creds; create the FA_PG_DATABASE first
psql "$FA_PG_DSN" -f migrations/0001_baseline.sql   # per-tenant tables auto-provision on first use
folder-actions            # admin API (:8099)
folder-actions-consumer   # event worker (separate process)
```

## Test

```bash
pytest                     # unit: matching, classifier scoring, MIME whitelist
```

## Deploy

`Containerfile` builds with the **monorepo parent** as context (so the sibling
`python_interface/` is copied in): `podman build -f folder_actions/Containerfile -t
folder-actions ..`. Run the API, consumer, and reconcile as three commands off the one
image (mirroring the `docker_unified` stack).

## Extending

Add an action without forking: implement an `ActionPlugin` (subclass the contract in
`plugins/base.py` — `type_name`, `supported_events`, `ConfigModel`, `config_fields()`,
`execute()`), register it on the `folder_actions.actions` entry-point group, and it appears
in `GET /action-types` with a generated form. `FA_ENABLED_ACTIONS` restricts which
registered actions are active per deployment.

---

AGPL-3.0-or-later.
