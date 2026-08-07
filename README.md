# folder_actions

Bind custom, extensible **actions** to a virtual folder in the FileEngine virtual
file system. When a recognized event happens to a file in a bound folder,
folder_actions runs the folder's configured actions.

See **[SPECIFICATIONS.md](SPECIFICATIONS.md)** for the full contract.

## What it does

Four built-in actions (each an in-process plug-in; extend via the
`folder_actions.actions` entry-point group):

- **move on review approve/reject** — move a file when its review is approved/rejected.
- **notify user or group** — real-time email to users / role members on any event.
- **automatic sorter** — classify a file (SmolDocBot deterministic classifier) from its
  extracted Markdown and move it to a destination by score threshold.
- **webhook** — POST to a remote (bearer / OAuth2), with a MIME whitelist, an
  admin-authored context bag, and a `move_to` / `metadata` response contract.

It consumes the single recognized `fileengine:events` Redis stream, owns its own
per-tenant Postgres, and calls the core over gRPC as a dedicated service principal.

## Processes

| Console script | Role |
|---|---|
| `folder-actions` | FastAPI admin API (:8099) — bindings, `/action-types`, classifier editor, run log |
| `folder-actions-consumer` | recognized-event worker (reacts + runs actions) |
| `folder-actions-reconcile` | periodic reconcile sweep |

## Develop

```bash
pip install -e ../python_interface        # the fileengine gRPC client
pip install -e '.[dev]'
cp .env.example .env                       # fill in creds
psql "$FA_PG_DSN" -f migrations/0001_baseline.sql   # per-tenant tables are code-provisioned
folder-actions           # API
folder-actions-consumer  # worker (separate process)
pytest
```

## Deploy

`Containerfile` builds with the **monorepo parent** as context (so `python_interface/`
is copied in). One image, three commands (API / consumer / reconcile) — see the
`docker_unified` stack. AGPL-3.0-or-later.
