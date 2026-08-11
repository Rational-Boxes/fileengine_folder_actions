# folder_actions — Custom actions on events within a folder

Bind custom, extensible actions to a **virtual folder** in the FileEngine virtual
file system. When a recognized event occurs on a file within a bound folder,
folder_actions runs the folder's configured actions (move, notify, auto-sort,
webhook, or any plug-in action).

> Status: **specification**. This document is the authoritative contract for the
> `folder_actions` service and for the small, additive upstream changes it
> depends on (§4). The seed code already in this directory — `classifier.py`
> (the deterministic text scorer) and `import_export.py` (classifier-set YAML
> import/export) — is vendored from the **SmolDocBot** project and is consumed
> as-is by the automatic sorter (§7.3).

---

## 1. Goals & non-goals

**Goals**
- Attach one or more **actions** to a virtual folder, keyed off recognized
  file-activity events on files within that folder.
- Ship five built-in actions: **move on review approve/reject**, **notify user or
  group**, **automatic sorter** (SmolDocBot classification), **webhook call**, and
  **raise a review** (auto-request a review on an added file — chainable across
  folders, §7.6).
- Two **binding-level filters** apply to *every* action: the trigger **`on_events`**
  and a content **MIME-type whitelist** (§5) — actions do not re-declare their own.
- Make actions **extensible via an in-process plug-in mechanism** (§6): the four
  built-ins are themselves plug-ins registered through the same interface.
- Fit the existing platform conventions exactly: Python/FastAPI service, trusted
  `AuthenticationContext` to core, Redis-Streams event consumption, per-tenant
  Postgres, LDAP/JWT auth, loopback-only monitoring.

**Non-goals**
- folder_actions is **not** a workflow engine or a rules DSL. Each binding is a
  single (event → action) rule with typed config; there is no branching,
  chaining, or scripting language in v1.
- It does **not** author reviews, comments, or classifications — it *reacts* to
  them. Reviews/comments are owned by `discussion_threaded_communication`;
  text extraction and classification input are owned by `convert_search_ai`.
- It does **not** re-implement extraction; it consumes CSAI's extracted text.

---

## 2. Placement in the ecosystem

| Concern | How folder_actions does it | Reference |
|---|---|---|
| Language / framework | Python ≥3.10, FastAPI + uvicorn, setuptools/PEP 621, `src/` layout, AGPL-3.0 header on every file | matches `discussion`, `convert_search_ai`, `audit_service` |
| Talk to core | `fileengine` Python client from the sibling `python_interface/`, via a `_client.py` bootstrap + `core_client.py` wrapper; trusted `AuthenticationContext` | `discussion/_client.py`, `discussion/core_client.py` |
| Event intake | Redis Streams consumer group `folder_actions` on the **single recognized stream** `fileengine:events` (XREADGROUP + XACK, at-least-once) | `EVENT_CONTRACT.md`; `discussion/consumer.py` |
| Own state | Its **own** per-tenant Postgres DB (schema provisioned in code) | `discussion/schema.py`, `convert_search_ai/schema.py` |
| Incoming admin auth | LDAP bind (Basic) or bridge HS256 JWT (Bearer), same stack as siblings | `discussion/http_auth.py`, `bridge_auth.py` |
| Email | `smtplib` SMTP mailer, best-effort, STARTTLS, MIME multipart | `discussion/mailer.py` |
| Role → email | LDAP `groupOfNames` members → `inetOrgPerson` `mail` | `ldap_manager/ldap_client.py` |
| Ports | app **8099**, monitor **loopback-only** (e.g. 18099 or 8100) | 8081–8098 already allocated |
| Deploy | `Containerfile` (build context = monorepo parent so `python_interface/` is copied first), compose services reusing one image with different `command:`; Ansible role | `convert_search_ai/Containerfile`, `docker_unified/` |

**Env prefix:** shared infra uses `FILEENGINE_*` (gRPC, Redis, Postgres host,
`FILEENGINE_JWT_SECRET`, `FILEENGINE_EVENTS_STREAM`); folder_actions-specific
knobs use the **`FA_`** prefix (§9).

---

## 3. Event model — the recognized stream

folder_actions consumes exactly one stream, `fileengine:events` (the
`EVENT_CONTRACT.md` envelope), with a dedicated consumer group so it sees every
event independently of other consumers. Delivery is **at-least-once and
fail-open** (the core publisher drops-oldest on Redis backpressure), so
folder_actions **dedupes** (§8) and runs a **reconcile sweep** (§8) to cover
retention gaps.

### 3.1 Trigger matrix

| Event type | Source | folder_actions treats as | Default triggers |
|---|---|---|---|
| `file.created` | core | file appears in folder (new row) | new-file |
| `file.updated` | core | new **version** written (PUT) | new-version |
| `file.moved`   | core | **file enters** folder (when new `parent_uid` = bound folder) or leaves it | new-file / arrival; **sorter (existing file moved in → inbox)** |
| `conversion.complete` | **CSAI (new, §4)** | extracted text/renditions ready for a version | automatic sorter (new content) |
| `conversion.failed` | **CSAI (new, §4)** | conversion resolved with **no** renditions — `reason` `unsupported` (non-previewable type) or `error` | webhook (still fire on non-previewable types); sorter (terminate deferral → skip) |
| `review.approved` | **discussion (new, §4)** | review request approved | move-on-approval |
| `review.rejected` | **discussion (new, §4)** | review request rejected | move-on-reject |
| `thread.opened`, `comment.created`, `mention.created`, `thread.resolved` | **discussion (promoted, §4)** | comment state-change | notify (opt-in) |
| `file.renamed`, `file.deleted`, `file.restored`, `dir.created`, `dir.deleted` | core | folder/file lifecycle | notify (opt-in) |
| `acl.changed`, `role.*` | core | governance | **ignored in v1** |
| any event with `is_rendition: true` | core | CSAI's own rendition write | **ignored** (avoids feedback) |

Notes:
- **New-file arrives three ways.** A file can enter a folder by **creation**
  (`file.created`), by **copy** (core `Copy` emits `file.created` for the **new
  uid** — a copy is logically a brand-new file and is handled exactly like any other
  new file, not as a reference to its source), or by **move** (`file.moved` with the
  new `parent_uid` = the bound folder). "New file in this folder" actions — and the
  sorter, via the copy's own `conversion.complete` — match all three.
- **New-file is a two-event sequence.** Creating a file through the bridges is
  `touch`→`file.created` then `put`→`file.updated`. Non-text actions must dedupe
  so a brand-new file does not double-fire.
- **The sorter waits for text.** It keys off `conversion.complete`, **not**
  `file.updated`, so the extracted text is guaranteed present when it classifies
  (no polling/retry race).

### 3.2 Folder scoping

An event is matched to a binding when the event's **`parent_uid` equals the bound
folder's UID** (the entity lives directly in the folder), or the event's
**`file_uid` equals the bound folder** (an event about the folder itself). Review
and comment events are anchored to a `file_uid`; folder_actions resolves that
file's current parent (via core `Stat`, cached) to test folder membership.

- **Recursion:** a binding is **folder-only by default**. A binding may opt into
  `recursive: true` to apply to the whole subtree; membership is then tested by
  walking `parent_uid` ancestry via core (bounded depth, cached). Path strings in
  the envelope are advisory and never used for a security or routing decision.

### 3.3 Loop avoidance

folder_actions' own `Move`/`SetMetadata` operations generate `file.moved` /
metadata events on the same stream. To avoid runaway recursion **without breaking
chains**:
- Every action core-call is made as the folder_actions **service principal** (§7.5).
- **Loop-safety is a plug-in manifest flag, not a hardcoded action.** Each plug-in
  declares **`auto_moves`** (§6) — `True` if it moves (or re-emits events for) files
  **unattended**, i.e. with no human gate between trigger and mutation (the sorter is
  the canonical case). The consumer **short-circuits any `auto_moves` action on a
  `file.moved` event whose `actor` is the service principal**, so such an action can
  never cascade on folder_actions' own (or another action's) moves. Future plug-ins
  that auto-move just set the flag and inherit the guard — no consumer change.
- **Actions without the flag deliberately DO react to service-principal moves.** This
  lets a file moved into a folder (e.g. by move-on-approve) trigger that folder's
  `raise_review` — enabling **human-gated chains** across folders (§7.6). Such a
  chain can't loop on its own because every hop waits on a human review decision.
- Additionally, each intended mutation is recorded in `action_run` (§10) and a
  redelivered/self-generated event matching a completed run is a no-op.

---

## 4. Required upstream changes (dependencies)

folder_actions' recognized triggers require these **small, additive** changes in
sibling services. They are additive to `EVENT_CONTRACT.md` (`schema` stays `1`;
new types, unknown-tolerant consumers unaffected).

1. **`convert_search_ai` — emit a terminal conversion outcome.** CSAI gains an
   `EventPublisher` (mirroring `discussion/events.py`) that, once a file's
   conversion **resolves**, `XADD`s one of two terminal events to
   `fileengine:events` (best-effort, never fails ingestion):
   - **`conversion.complete`** — after it durably writes the extracted
     text/renditions. Payload: base envelope + `file_uid`, the **source `version`**
     that became ready, `tenant`, `actor`, and `renditions: ["text", "pdf", …]`
     (what is available).
   - **`conversion.failed`** — conversion did not produce renditions, carrying a
     **`reason`**: `unsupported` (a **non-previewable type** — no converter exists,
     so a rendition will *never* arrive) or `error` (a converter was attempted and
     failed). Same envelope; `renditions: []`.

   Both are **terminal** — exactly one fires per resolved version — so a consumer
   that must wait for content resolution can key off either and never hang on a
   file that simply cannot be converted. (This replaces the earlier "optional,
   observability-only" framing: `conversion.failed` is now a first-class recognized
   event because webhooks and the sorter depend on the *non-previewable* signal.)

2. **`discussion_threaded_communication` — promote collaboration events to the
   recognized stream and add explicit review states.**
   - Publish the comment/thread events (`thread.opened`, `comment.created`,
     `mention.created`, `thread.resolved`) and the review events onto
     **`fileengine:events`** (the single recognized stream), rather than only the
     private `discussion:events` side stream. (Dual-publish during migration is
     acceptable; the digest may keep reading the side stream.)
   - Replace the free-form `review.completed` + `outcome` terminal with explicit
     **`review.approved`** and **`review.rejected`** transitions/events. The
     review lifecycle folder_actions recognizes is: `review.requested` →
     `review.acknowledged` → (`review.approved` | `review.rejected`). Events carry
     the existing extras: `review_id`, `target_user`, and (for comment events)
     `thread_id` / `anchor`.

3. **`EVENT_CONTRACT.md`** — document the new `conversion.*` and collaboration
   event family as first-class recognized types.

If (1)/(2) are not yet deployed, folder_actions degrades gracefully: the sorter
and review-driven actions simply never fire; file/version/move-driven actions
work against core events alone.

---

## 5. Configuration model — folder bindings

A **binding** attaches one action to one folder. It is the unit of config.

```jsonc
// action_binding (stored in folder_actions' own DB, §10)
{
  "id":            "uuid",
  "tenant":        "default",
  "folder_uid":    "uuid",          // the bound virtual folder
  "recursive":     false,           // apply to subtree? (§3.2)
  "action_type":   "sorter",        // plug-in type name (§6)
  "on_events":     ["conversion.complete"], // binding-level trigger: recognized types this fires on
  "mime_types":    ["application/pdf", "image/*"], // binding-level content filter; empty = all types
  "config":        { /* action-type-specific, validated by the plug-in schema */ },
  "enabled":       true,
  "created_by":    "alice",
  "created_at":    "…"
}
```

- **Two binding-level filters apply to every action** — actions never re-declare
  them: **`on_events`** (which recognized event types fire the binding) and
  **`mime_types`** (a content whitelist). The consumer enforces both *before*
  invoking the plug-in. `mime_types` is **content-sniffed and fail-closed** (§7.4.1
  semantics, generalized): if set and the file's MIME can't be resolved or doesn't
  match (exact type or trailing wildcard `image/*`), the run is skipped. Empty
  `mime_types` = fire on all types.
- **Multiple bindings per folder** are allowed (e.g. notify + sorter). They run
  independently; ordering between bindings on the same event is unspecified
  (each is idempotent).
- **ACL-governed (locked):** creating/editing/deleting a binding requires the
  caller to hold **WRITE (or MANAGE_ACL)** on `folder_uid`, checked via core
  `CheckPermission` as the calling user. Reading a folder's bindings requires
  READ. folder_actions never exposes another tenant's or unauthorized folder's
  config.
- Config is surfaced through a small admin REST API (`/folders/{uid}/actions`,
  CRUD) intended for the frontend, alongside the **classifier-set editor** API
  (§7.3.1: CRUD, import/export via `import_export.py`, and test-scoring).

---

## 6. Plug-in mechanism

Actions are **in-process Python plug-ins** discovered at startup.

- **Discovery:** setuptools entry-point group **`folder_actions.actions`**; each
  entry point resolves to an `ActionPlugin` subclass. Built-ins register the same
  way, so third parties add actions without forking the service. A static
  allowlist env (`FA_ENABLED_ACTIONS`) can restrict which registered plug-ins are
  active per deployment.
- **Interface:**

  ```python
  class ActionPlugin(Protocol):
      type_name: str                      # e.g. "sorter", "webhook"
      label: str                          # human name for the "add action" UI
      supported_events: set[str]          # recognized types this action may bind to
      auto_moves: bool = False            # manifest loop-safety flag (§3.3): True if
                                          # it moves files UNATTENDED (sorter, webhook)
      ConfigModel: type[pydantic.BaseModel]   # typed config; server-side validation

      @classmethod
      def config_fields(cls) -> list[FieldDescriptor]: ...  # generic form schema (§6.1)

      def execute(self, event: dict, config: ConfigModel,
                  ctx: ActionContext) -> ActionResult: ...
  ```
  The **manifest** each plug-in publishes (`type_name`, `label`, `supported_events`,
  **`auto_moves`**, and the `config_fields` form schema) is exposed at
  `GET /action-types` — so the consumer's loop-guard (§3.3) and the frontend form are
  both driven by declared metadata, never hardcoded per action.

- **`ActionContext`** (dependency-injected, the plug-in's only capabilities):
  `core` (a `ManagedFiles` client bound to the **service principal**, §7.5),
  `csai` (extracted-text fetch), `mailer` (SMTP), `directory` (LDAP user/role →
  email), `secrets` (decrypt webhook creds), `log`, and the resolved `tenant`.
- **Trust:** plug-ins run **in-process with service credentials** — they are
  trusted, first-party or operator-installed code, not tenant-supplied. Untrusted
  remote logic belongs behind the **webhook** action (§7.4), which is the
  sandboxed extension point for third parties.
- **Failure isolation:** an exception from `execute` fails only that binding's run
  for that event (recorded in `action_run`), never the consumer loop or other
  bindings. See retry policy §8.

### 6.1 Config field descriptors (generic form contract)

Each plug-in **publishes a typed list of config field descriptors** — a
declarative form schema — so a **generic frontend renders a config form for any
plug-in without plug-in-specific code** (§12.2). The frontend keeps a
**field-type → widget registry**; a plug-in that reuses existing field types needs
**zero** frontend work, and only a brand-new field *type* requires a new widget.

- **Enumeration API:** `GET /action-types` →
  `[{ type_name, label, description, supported_events, fields: [FieldDescriptor…] }]`.
  The frontend builds the entire "add / edit action" form from this response.
- **The server stays the validation authority.** Descriptors carry the constraints,
  and the service validates `binding.config` against them **and** the plug-in's
  `ConfigModel` on write — a generic UI never weakens server-side validation. (A
  plug-in may derive its descriptors from its `ConfigModel`, or declare them
  directly; the descriptors are the canonical *form* schema, `ConfigModel` the
  typed/validated representation.)

**FieldDescriptor** shape:

```jsonc
{
  "key": "timeout_s",             // config key it sets
  "label": "Timeout (seconds)",
  "type": "integer",              // from the standard catalog below
  "required": false,
  "default": 10,
  "help": "How long to wait for the remote.",
  // type-specific (only the relevant ones present):
  "min": 1, "max": 120, "step": 1,               // number / integer
  "max_length": 2048, "pattern": "^https://",     // string
  "options": [{ "value": "bearer", "label": "Bearer token" }],  // static select
  "options_source": "event_catalog",              // dynamic options (see below)
  "item_fields": [ /* FieldDescriptor… */ ],      // rows of a group/array
  "secret": true,                                 // write-only, never returned
  "visible_when": { "key": "auth_type", "equals": "oauth2_client_credentials" }
}
```

**Standard field-type catalog** (the generic renderer's widget registry):

| `type` | Widget | Used by (examples) |
|---|---|---|
| `string` | single-line text | webhook URL, classification name |
| `text` | multi-line text | notify template body |
| `integer` / `number` | number input (min/max/step) | timeout, retries, threshold, distance, weight, priority |
| `boolean` | toggle | `recursive`, `grant_read` |
| `select` | dropdown (static `options`) | auth type, read-back `format` |
| `multiselect` | multi-chip select | `on_events` (source `event_catalog`) |
| `secret` | write-only field, never rendered back | webhook token, client secret |
| `folder` | **folder picker** | move destinations, `on_approved` / `on_rejected` |
| `file` | file picker | test-panel file selection |
| `principal` | **user/role picker** (LDAP autocomplete) | notify recipients |
| `ref` | dropdown from a service list (`options_source`) | classifier-set reference |
| `group` | **repeatable rows** of nested `item_fields` | sorter routing table, recipient list |

- **Dynamic option sources** (`options_source`) the frontend resolves generically
  against existing APIs — `event_catalog` (recognized event types),
  `classifier_sets` (the tenant's sets), `mime_catalog` (common MIME types, as a
  `multiselect` that also accepts free-entry wildcard patterns like `image/*`) —
  while `folder` / `principal` are their own pickers. Static choices use inline
  `options`.
- **Conditional fields:** `visible_when` shows/hides a field based on another
  field's value (e.g. OAuth2 fields only when `auth_type` = client credentials) —
  still fully generic, no per-plug-in code.

This renders the four built-ins declaratively — e.g. the **sorter's routing table**
is a `group` of `{ ref classification, number threshold, folder destination,
integer priority }`, and **notify recipients** a `group`/`principal` list — and any
third-party plug-in composed from these types is configurable out of the box.

---

## 7. Built-in actions

### 7.1 Move on review approve/reject

- **Trigger:** `review.approved` / `review.rejected` for a file currently in the
  bound folder.
- **Config:** `{ "on_approved": "<dest-folder-uuid>?",
  "on_rejected": "<dest-folder-uuid>?" }` (either may be omitted → no move for
  that outcome).
- **Behavior:** core `Move(file_uid, dest_parent_uid)` as the service principal.
  Destination validated (exists, is a folder, same tenant). No-op if the file
  already lives in the destination.

### 7.2 Notify user or group

- **Trigger:** the binding's `on_events` (§5) — notify does not carry its own event
  list.
- **Config:** `{ "recipients": ["alice", "role:editors"], "template": "<template-id?>" }`.
  `template` is an optional reference to a stored **event-notification email
  template** (§7.2.1); blank uses the built-in default body.
- **Behavior:** resolve recipients to email addresses —
  - a user → LDAP `mail` (fallback: the uid if it is an address);
  - a `role:<name>` → **all members** of the tenant role via
    `list_members(tenant, role)` then `get_user(uid).mail` (the LDAP bind-DN
    placeholder is filtered out) — this is the spec's "all users in a role are
    emailed."
  - Send **in real time** (per event, not digested) via the SMTP mailer,
    best-effort. Email body deep-links to the file/version/thread in the SPA.
- **De-dupe:** no self-notification (recipient == `actor` skipped), and
  per-`event_id` so redelivery does not re-mail.

#### 7.2.1 Event-notification email templates

Reusable, **tenant-level** email templates the notify action renders per event.
Managed in-product under **System → Email templates → Event notifications** (§12),
tenant-admin gated; a folder editor may only *select* one on a notify binding.

- **Fields:** `name`, `subject`, `body_text`, `body_html`. subject/body carry
  `{placeholder}` tokens substituted at send time: `{actor}`, `{event}`, `{name}`,
  `{file_uid}`, `{version}`, `{tenant}`, `{folder_uid}`, `{link}` (SPA deep-link).
- **Store + API:** `notify_template` table (§10); `GET/POST/PUT/DELETE
  /notify-templates` — mutations tenant-admin, **`GET` (list) available to any
  authenticated user** so the notify binding editor's template dropdown resolves.
- The notify config's `template` field is a **`ref`** (`options_source:
  notify_templates`) so the generic form renders a live dropdown; adding a template
  reflects immediately (frontend shares a store, §12).

### 7.3 Automatic sorter (SmolDocBot classification)

- **Triggers (two).** The sorter classifies and re-files a file whenever it has
  ready content in the bound folder, whether the content is **new** or the file was
  **moved in**:
  - **`conversion.complete`** — a file's content became ready (new file or new
    version); text is guaranteed present. Primary path for newly-added content.
  - **`file.moved` into the bound folder** — an **existing** file was moved in;
    classify it and route it per the determination. This makes a bound folder an
    **inbox / drop zone**: move any file in and it is automatically filed to the
    right destination.

  A `file.moved` whose `actor` is the folder_actions service principal is **ignored**
  (§3.3), so the sorter's *own* routing move never re-triggers the sorter (no loop /
  no cascade if the destination also has a sorter binding).
- **Inputs:** the folder binds a **classifier set** (SmolDocBot YAML, imported via
  `import_export.py`; scored by `classifier.py`) plus a **routing table** mapping
  each classification name → `{ threshold, destination_folder }`. The classifier
  set (reusable) and the per-folder routing live in the DB (§10); the YAML stays
  the pure classifier definition so the same set can route differently per folder.
- **Behavior:**
  1. Resolve the file's current version and fetch its **extracted Markdown** (the
     normalized text backing CSAI's search index) from CSAI — the same
     source-agnostic text surface a webhook can request as `format=markdown` (§7.4).
     - On **`conversion.complete`** the ready version is named by the event.
     - On **`file.moved`** the file is usually already converted, so its text is
       available immediately. If its text is **not yet available** (never converted,
       or events were off when it was created), the sorter **defers** rather than
       classifying empty text: it requests conversion (CSAI
       `POST /documents/{uid}/convert`) and lets the ensuing `conversion.complete`
       re-fire the sort. If **`conversion.failed`** arrives instead (non-previewable
       type or error), the sorter **stops deferring and skips** (recorded `skipped`
       in `action_run`) — a file with no extractable text cannot be classified, so
       it is left in place rather than deferred indefinitely.
  2. Run `document_classifier(text, classifier_set)` → `{classification: score}`.
     **Scores are unbounded weighted sums** (sum of matched term weights), not
     normalized confidences; thresholds are expressed in the same weight units.
  3. Select winners: classifications whose `score ≥ threshold`.
  4. **Tie-break (locked):** **highest score wins**; ties broken by a per-binding
     **priority order** over classification names; final fallback is
     first-declared. If no classification clears its threshold, the file is **left
     in place** (logged, no move).
  5. `Move(file_uid, destination_folder)` as the service principal; record the
     winning classification, score, and provenance in `action_run`.
- **Idempotent** on `(event_id, binding)`; already-in-destination is a no-op — so a
  file that lands on its correct destination is not moved again, and a moved-in file
  already classified to *this* folder stays put.

#### 7.3.1 Classifier sets — editor, import/export, and tuning

A classifier set is a **reusable, tenant-scoped** object (defined once, routed
per-folder by `sorter_route`, §10). folder_actions integrates a full **editor**
for authoring and tuning them in-product — not only SmolDocBot YAML import:

- **CRUD** over sets, classifications, and terms, persisted in
  `classifier_set` / `classifier` / `classifier_term` (§10) and validated by the
  `classifier.py` pydantic models (`Term.distance ≥ 0`, `weight ≥ 0`; wildcard
  tokens `*` / `?` / `#` allowed in `term`).
- **Import / export** SmolDocBot YAML via the vendored `import_export.py`
  (`type: classifier`), so authored and externally-generated sets round-trip.
- **Test / dry-run scoring** — the key tuning affordance. Score arbitrary pasted
  text **or a chosen file** (fetch its search-index Markdown from CSAI, READ-gated
  as the calling user) against a set, returning **per-classification scores and
  the terms that matched**. Because scores are **unbounded weighted sums**, an
  author cannot choose sane per-folder `threshold` values (§7.3) without seeing
  real scores on real documents — this endpoint makes thresholds tunable. It runs
  `classifier.py`'s `document_classifier` (pure, side-effect-free).
- **API** (served by the `folder-actions` process, consumed by the frontend UI):
  `GET/POST/PUT/DELETE /classifier-sets[/{id}[/classifiers[/{cid}[/terms/{tid}]]]]`;
  `POST /classifier-sets/import` + `GET /classifier-sets/{id}/export` (YAML);
  `POST /classifier-sets/{id}/test` `{ text | file_uid }` → `{scores, matches}`.
- **UI placement.** The editor *interface* lives in the **frontend** repo (per the
  platform's UI-ownership convention); folder_actions owns the API + validation.
- **Permissions.** Editing a shared classifier set is **tenant-admin / configured-
  role gated** (a set spans folders, so it is not governed by any one folder's
  ACL); *binding* a set to a folder stays folder-ACL-governed (§5). The `test`
  endpoint additionally enforces READ on any `file_uid` it scores, as the caller.

### 7.4 Webhook call

- **Trigger:** the binding's `on_events` (§5). Which file types fire it is the
  binding's `mime_types` filter (§5) — both are binding-level, not webhook config.
- **Config:**
  ```jsonc
  {
    "url": "https://remote/hook",
    "auth": {
      "type": "bearer" | "oauth2_client_credentials",
      // bearer:
      "token": "<secret>",
      // oauth2_client_credentials:
      "token_url": "https://idp/token", "client_id": "…",
      "client_secret": "<secret>", "scopes": ["…"]
    },
    "context": { "project": "Acme Tower", "stage": "design-review" },  // arbitrary admin key:values (§7.4.2)
    "grant_read": true,        // mint a scoped READ token so the remote can fetch the file
    "timeout_s": 10, "max_retries": 5
  }
  ```
  Secrets (`token`, `client_secret`) are stored **encrypted** (§10) and injected
  via `ctx.secrets`, never logged or returned by the admin API.
- **MIME-type whitelist (firing filter).** `mime_types` restricts the webhook to
  files whose MIME type matches the list, so a remote only ever sees the types it
  handles.
  - **Semantics:** empty / absent ⇒ fire on **all** types (no filter). Otherwise
    the webhook fires **only** when the target file's MIME matches an entry. Entries
    support a trailing wildcard (`image/*`, `text/*`) and exact types
    (`application/pdf`). Matching is case-insensitive on the type/subtype.
  - **MIME resolution is content-based (anti-spoofing).** The event envelope
    **does not carry `mime`** (per `EVENT_CONTRACT.md`), and the whitelist must
    **not** trust the filename extension — that would be trivially spoofable. So
    folder_actions reads a **byte prefix** (~first 8 KiB) of the target `file_uid`
    via core `GetFile`/range and **content-sniffs** it with CSAI's
    `mime.detect(data, name)`: a magic-byte table + OOXML/ZIP container sniff +
    `libmagic` (`python-magic`, an optional dep) when available, with the filename
    extension only as a **last resort**. Renaming `malware.exe` to `report.pdf`
    therefore does **not** satisfy an `application/pdf` whitelist — the match is on
    the actual bytes. The resolved type is cached per `(file_uid, version)`.
  - **Extension-only guesses are low-confidence.** When content sniffing is
    inconclusive and only the filename yields a type, a whitelist match is treated
    as **unresolved → skip** (fail-closed) — a spoofed name cannot pass the filter
    by extension alone. (Content-sniffed AEC/CAD text formats like IFC/STEP/STL are
    covered by CSAI's sniffer, so they match normally.)
  - **Fail-closed on a set whitelist:** if a whitelist is configured and the MIME
    **cannot be resolved** (e.g. the event is folder-scoped, or the file is
    unreadable) or does **not** match, the webhook is **skipped** for that event
    (recorded `skipped` in `action_run`, with the reason). A whitelist means "only
    these types," so an unknown type is excluded, not included.
  - This is a general filtering pattern; the same `mime_types` predicate may be
    offered by other actions (e.g. notify) in future without changing the model.
- **Custom context (admin-authored, §7.4.2).** `context` is an **arbitrary
  key:value map** the administrator authors when configuring the webhook, sent
  **verbatim** with every POST under the top-level `context` key — for routing
  hints, project/tenant tags, correlation ids, or whatever additional context the
  remote needs. It is **static** (identical on every fire of this binding),
  namespaced under `context` so it never collides with reserved payload fields, and
  **not a secret store** (it is plaintext and readable back via the admin API — use
  `auth` for credentials). The generic form renders it as a repeatable `group` of
  `{ string key, string value }` (§6.1).
- **Request:** `POST url` with a JSON body:
  ```jsonc
  {
    "event": "conversion.failed",
    "document_id": "<file_uid>", "version": "<version>",
    "tenant": "default",
    "metadata": { /* current core file metadata */ },
    "mime": "application/x-lorem",           // the content-sniffed type (§7.4.1)
    "conversion": {                          // so the remote knows what it can read back
      "status": "failed",                    // "complete" | "failed" | "unknown"
      "reason": "unsupported",               // present on failed: "unsupported" | "error"
      "renditions": []                       // available formats; [] when non-previewable
    },
    "user": { "actor": "<event.actor>" },   // the acting user identity
    "folder_uid": "<bound folder>",
    "context": { "project": "Acme Tower", "stage": "design-review" }  // admin-authored (§7.4.2)
  }
  ```
  Auth to the remote: `Authorization: Bearer <token>` (static) or an **OAuth 2.0
  client-credentials** token fetched from `token_url` and cached until expiry.
- **Scoped read-back (optional):** if `grant_read` is set, folder_actions mints a
  **short-lived, READ-scoped token** for the target file (via the http_bridge
  OAuth/introspection path, or a time-boxed core ACL grant to a webhook principal)
  and includes it so the remote can read the file contents, then revokes/expires
  it. Scope is **READ on that one file only** — and because the core gates
  **rendition bytes by READ on the source file**, that single grant transitively
  authorizes the file's renditions too.
- **Rendition choice on read-back.** The content endpoint the remote calls is
  **rendition-aware**: it may request the **original** bytes *or* a **standard
  rendition** via a `format` parameter, so a remote can consume a normalized form
  without handling every source type. Recognized formats:
  - `original` — raw source bytes (via the REST door, core `GetFile`).
  - `markdown` — the **extracted Markdown that backs CSAI's search index**
    (CSAI `GET /documents/{uid}/text`): a normalized, source-agnostic plain-text
    form. In many cases this is a **better input to an external text-processing
    system** (NLP, LLM, extractor) than the original bytes — a remote need not
    parse PDFs/Office/CAD itself, and it gets the *same* clean text across every
    source type. It is exactly the text the automatic sorter's classifier
    consumes (§7.3), so downstream behavior is consistent between the two.
  - `pdf`, `preview`, `thumbnail`, `poster`, `model` — CSAI rendition children
    (the `_KNOWN_FMTS` vocabulary), addressed as the `<version>-<fmt>` hidden
    child and READ-gated by the source token.
- **Extraction/indexing delay.** A requested rendition (Markdown, PDF, …) is
  produced by CSAI's **asynchronous** extraction/indexing pipeline and **may not
  exist yet** when the webhook fires. Handling:
  - **Preferred:** bind a webhook that needs a rendition to **`conversion.complete`**
    (§3.1) rather than to `file.updated`; by then the renditions and index are
    ready, so the read-back never races. This is the same readiness gate the
    automatic sorter (§7.3) uses.
  - **Otherwise:** the read-back endpoint returns a **"not ready"** response
    (e.g. `202` + `Retry-After`) for a missing rendition so the remote polls, and
    MAY **on-demand generate** it via CSAI `POST /documents/{uid}/convert`
    (bounded wait) before serving. `original` is always available immediately.
- **Non-previewable types must still fire.** Some files can never be converted
  (no converter for the type) — binding only to `conversion.complete` would drop
  them silently. To fire on **content resolution regardless of previewability**,
  bind to **both `conversion.complete` and `conversion.failed`** (the terminal
  outcome pair, §4). Exactly one fires per version, so the webhook always runs once
  the file's fate is known.
  - The request's **`conversion` block** tells the remote what to expect:
    `status: "complete"` with a non-empty `renditions` list, or `status: "failed"`
    (`reason: "unsupported" | "error"`) with `renditions: []`.
  - On a `failed`/non-previewable file, only **`original`** is available on
    read-back; a `markdown`/`pdf`/… request returns **not available** (`409`/`404`)
    rather than looping on `Retry-After` — there is nothing coming.
  - For events **not** gated on conversion (`file.updated`, `review.*`, comment,
    move), the `conversion` block is filled **best-effort** from current rendition
    state (`status: "unknown"` if not yet resolved).
- **Response handling:** on `2xx`, the returned JSON may contain:
  - **`move_to`**: a folder UUID → `Move(file_uid, move_to)` as the service
    principal (validated like §7.1).
  - **`metadata`**: an object → `SetMetadata(file_uid, k, v)` per key (added to the
    file; never deletes existing keys unless explicitly null).
- **Failure semantics:** connection/timeout/5xx → bounded exponential-backoff
  retries (`max_retries`); after exhaustion the run is marked `failed` and logged;
  the event is still `XACK`ed (a poison webhook must not block the stream).
  4xx (except 429) → no retry (caller misconfiguration), marked `failed`.
- **Loop-safety:** the response's `move_to` is an **unattended** move, so webhook
  declares **`auto_moves = True`** (§3.3) — it won't re-fire on folder_actions' own
  moves.

### 7.6 Raise a review (chainable)

Automatically raise a **review request** on a newly-added file, assigned to
specified reviewers — the building block for **action chains across folders**.

- **Trigger:** the binding's `on_events` — typically the "file added" events
  (`file.created`, `file.moved` into the folder; also `file.updated` /
  `conversion.complete`).
- **Config:** `{ "reviewers": ["bob", "role:legal"] }` — encoded principals; a
  `role:<name>` expands to each member. Rendered by the generic form as a
  `principal` picker.
- **Behavior:** resolve reviewers (expand roles; drop the `actor` — no self-review;
  dedupe), then POST `/files/{file_uid}/reviews` to the **discussion service**
  (`DiscussionClient`, as the service principal) with the reviewers and the event's
  `version`. The discussion service validates each reviewer holds READ (a `422`
  error-marks any who don't → the run is `skipped: reviewers_no_access`). A transport
  failure is `retryable`.
- **Loop-safety:** raise_review does **not** move files and does **not** set
  `auto_moves`, so it *does* react to service-principal moves — which is what makes
  chains work.
- **Chaining pattern:** folder A binds `raise_review` (on file added) + `move_review`
  (approved→B, rejected→C). A file lands in A → a review is requested → a human
  approves → `move_review` moves it to B → B's `raise_review` fires on the arrival →
  the next review is requested, and so on. Every hop is human-gated, so the chain
  advances only on real decisions and cannot loop on its own (§3.3).

---

## 8. Execution semantics

- **Idempotency.** Dedupe on `event_id`; every `(event_id, binding_id)` maps to a
  single `action_run` row (unique constraint). Redelivery of a completed run is a
  no-op. Content actions additionally collapse on `(file_uid, version)`.
- **At-least-once + ack.** `XREADGROUP` in a consumer group; `XACK` **only after**
  the run reaches a terminal state (`done` / `failed` / `skipped`) and is
  committed. `XAUTOCLAIM` reclaims stalled entries on restart.
- **Ordering.** Best-effort per `file_uid` only; never assume global order. For
  the sorter, the `version` string in `conversion.complete` is authoritative for
  "newest wins."
- **Reconcile sweep.** A periodic sweep (like CSAI/discussion) reconciles against
  core to recover events missed during a Redis outage or beyond stream retention
  (e.g. re-evaluate sorter routing for recently-converted files in bound folders).
- **Fail-open, isolated.** A failing binding never blocks the loop or sibling
  bindings; failures are counted and surfaced on `/readyz`/metrics.
- **Poison messages** (unparseable envelopes) are logged, counted, and acked.

---

## 9. Service shape

**Entry points** (`[project.scripts]`), one image, several compose commands:
- `folder-actions` — FastAPI admin API (bindings CRUD, **`GET /action-types`**
  field-schema enumeration §6.1, **classifier-set editor** — CRUD + import/export +
  test-score, §7.3.1 — run-log queries) on **:8099**.
- `folder-actions-consumer` — the Redis-Streams worker (portless).
- `folder-actions-reconcile` — periodic reconcile sweep (portless / cron).

**Config (env).** Shared: `FILEENGINE_GRPC_HOST/PORT` (50051),
`FILEENGINE_REDIS_HOST/PORT/PASSWORD/DB`, `FILEENGINE_EVENTS_STREAM`
(`fileengine:events`), `FILEENGINE_PG_HOST`, `FILEENGINE_JWT_SECRET`.
folder_actions-specific (`FA_`): `FA_PG_DB`/`FA_PG_USER`/`FA_PG_PASSWORD`,
`FA_EVENTS_GROUP` (`folder_actions`), `FA_HTTP_HOST`/`FA_HTTP_PORT` (8099),
`FA_SERVICE_PRINCIPAL` / service-principal credentials (§7.5),
`FA_CSAI_BASE_URL` (extracted-text fetch), `FA_ENABLED_ACTIONS`,
`FA_SECRET_KEY` (webhook-secret encryption), and SMTP: `FA_SMTP_HOST/PORT/USER/
PASSWORD/FROM` (empty `FA_SMTP_HOST` disables email → log instead).

**Health.** `/healthz` (liveness), `/readyz` (core gRPC + Redis + Postgres + LDAP
reachable), `/poolz`; all **bound loopback-only** behind the monitoring allowlist
middleware (never `0.0.0.0`).

**Repo layout.**
```
folder_actions/
  pyproject.toml  README.md  SPECIFICATIONS.md  LICENSE  .env.example
  migrations/0001_baseline.sql            # DB-wide bootstrap (extensions) only
  classifier.py                           # SmolDocBot scorer (vendored, consumed by sorter)
  import_export.py                        # classifier-set YAML import/export (vendored)
  src/folder_actions/
    app.py            # FastAPI factory + monitoring allowlist + main()
    api.py            # bindings CRUD, /healthz /readyz
    classifier_api.py # classifier-set editor: CRUD + import/export + test-score (§7.3.1)
    config.py         # load_dotenv + Config(env)
    _client.py core_client.py             # fileengine client bootstrap + service-principal wrapper
    ldap_auth.py http_auth.py bridge_auth.py jwt_verify.py
    classifier_io.py notify_templates_api.py  # classifier YAML + notify-template API
    consumer.py events.py                 # recognized-stream consume + folder matching
    matching.py                           # event → binding resolution (§3.2)
    plugins/                              # ActionPlugin registry + built-ins
      base.py move_review.py notify.py sorter.py webhook.py raise_review.py
    csai_client.py discussion_client.py   # extracted-text fetch; raise-review POST
    mime.py                               # content-sniff MIME resolver + whitelist match
    mailer.py directory.py                # SMTP + LDAP role/user → email
    secrets.py                            # webhook-secret encryption
    db.py schema.py store.py              # per-tenant Postgres
  src/tests/
```

**Deployment.** `Containerfile` (base `python:3.12-slim`, build context = monorepo
parent so `python_interface/` copies first; `.env` never copied); compose services
`folder-actions`, `folder-actions-consumer`, `folder-actions-reconcile` on one
image; Ansible role `scripts/Ansible/roles/folder_actions/`.

---

## 10. Data model (folder_actions DB, per-tenant schema)

| Table | Purpose | Key columns |
|---|---|---|
| `action_binding` | one (folder, event, action) rule | `id, folder_uid, recursive, action_type, on_events[], mime_types[], config jsonb, enabled, created_by` |
| `classifier_set` / `classifier` / `classifier_term` | imported SmolDocBot classifier definitions | per `import_export.py` (`name`; `name`; `term, distance, weight`) |
| `sorter_route` | per-binding routing over a classifier set | `binding_id, classification_name, threshold, destination_folder, priority` |
| `notify_template` | reusable event-notification email templates (§7.2.1) | `id, name, subject, body_text, body_html, created_by` |
| `webhook_secret` | encrypted webhook credentials | `binding_id, ciphertext` (AES via `FA_SECRET_KEY`) |
| `action_run` | idempotency + execution log/audit | `event_id, binding_id, file_uid, version, status(done/failed/skipped), detail jsonb, ts` — **UNIQUE (event_id, binding_id)** |

Per-tenant schemas are provisioned in code (`schema.ensure_tenant_schema`), the
DB-wide `migrations/0001_baseline.sql` only bootstraps extensions — mirroring
`discussion`/`convert_search_ai`.

---

## 11. Security model (summary)

- **Binding config** is written/read only by users holding WRITE/MANAGE_ACL / READ
  on the folder (checked as the calling user via core `CheckPermission`).
- **Action execution** runs as a dedicated **folder_actions service principal**
  (§7.5), **never as the triggering user** — so an action is **not gated by the
  effective permissions of the user who created or moved-in the file**. A document may
  be moved into (or have metadata written on) a folder the triggering user has **no
  access to at all** — not read, not write — and the move must **not** be blocked by
  that user's ACLs. This is intentional: actions are the folder owner's configured
  automation, authorized at *binding* time (when an admin with WRITE/MANAGE_ACL on the
  folder set them up), not re-checked against the acting user at *event* time. The
  **only** ACL that governs a move is the **service principal's** WRITE on the
  destination. Consequences worth stating: a submit-then-hand-off workflow (drop a file
  in an intake folder → it is auto-routed to a review folder the submitter can't see) is
  a first-class use case; and the triggering user simply loses visibility of the file
  once it lands where they lack access — expected, not an error. The service principal's
  gRPC access is inside the trusted zone; the gRPC port is never network-exposed.
- **Webhook read-back** is the only outward data exposure: a **short-lived,
  single-file, READ-scoped** token, revoked/expired after use.
- **Secrets** (webhook tokens/client secrets) are encrypted at rest and never
  logged or returned by the API.
- **Loopback-only monitoring** endpoints (§9), per platform convention.

### 7.5 The service principal
A dedicated identity (`FILEENGINE_FA_USER` / `FILEENGINE_FA_PASSWORD`, e.g.
`svc:folder_actions`) with a role granting the writes the actions need
(Move/SetMetadata on the folders it manages). **Every** action core-call binds this
identity — moves/metadata are performed *as the service principal*, independent of and
never gated by the triggering user's permissions (§11). It also drives loop-avoidance
(§3.3). **Operational requirement:** the service principal must therefore hold WRITE on
every folder any action routes *into* (across the folder graph a chain can span); a
move to a destination the principal cannot write fails (recorded `failed`), so grant it
appropriately — the cleanest is a role with WRITE on the managed-folder subtree (or
`system_admin` if it legitimately manages the whole tenant, though least-privilege is
preferred).

### 7.7 Permission normalization on move
Every move an action performs (`CoreClient.move`, used by move-on-review, the sorter,
and the webhook `move_to`) runs a **post-move followup** that normalizes the moved
file's ACL to its destination folder — so a document does not carry stale grants from
where it came from into a folder with a different access profile.

- **Mode: mirror.** The file's **own** explicit ACL is made a copy of the destination
  folder's **own** explicit ACL. It's reconciled as a minimal diff — read both ACLs
  (`GetResourceAcls`), then revoke only the atoms the file has but the destination
  doesn't and grant only those it's missing — so a rule already correct on both (e.g.
  the service principal's `MANAGE_ACL`) is never churned. This is a point-in-time
  snapshot: it does not track later changes to the folder's ACL, and is intentionally
  partly redundant with the model's own parent-traversal inheritance.
- **Granularity.** `GetResourceAcls` returns each rule's permissions as the internal
  bitmask; Grant/Revoke take a single permission, so a rule is copied bit-by-bit, with
  the `role:`/`claim:` principal prefix reconstructed from the stored PrincipalType
  (the reserved `GROUP` type is skipped). ALLOW and DENY rules are mirrored faithfully.
- **Best-effort, never reverses a move.** The move has already succeeded and its
  `file.moved` event fired before normalization runs; a normalization failure (or any
  individual grant/revoke) is logged and skipped, never failing or undoing the move.
- **Rights.** Reading/replacing a resource's ACL requires `MANAGE_ACL`; the service
  principal must therefore hold `MANAGE_ACL` on both the file and the destination folder
  (satisfied by the managed-folder-subtree role of §7.5, or `system_admin`). Where it
  lacks `MANAGE_ACL`, normalization is skipped best-effort and the move still stands.

---

## 12. Frontend UI surfaces (frontend repo)

Per the platform's UI-ownership convention, all end-user UI lives in the
**frontend** Vue 3 SPA, not here; folder_actions exposes the REST APIs these
surfaces consume (bearer-JWT auth, same stack as the other services).

**Interaction model — summary in the drawer, full editor in a modal overlay.**
The file/folder **drawer tab** carries only a compact **summary** (the folder's
bindings, their state, quick enable/disable/delete). The actual **authoring UI is
too complex for the drawer** — schema-driven field forms with folder/principal
pickers and repeatable groups, and especially the classifier editor with its test
panel — so **add / edit opens a full modal overlay** (a focused, roomier surface),
not an inline drawer form. The surfaces needed:

1. **Folder actions panel (drawer summary).** In the folder/file browser drawer, a
   tab **summarizing** the folder's action bindings — type, trigger events, a
   one-line config summary, enabled toggle, last-run status — with quick
   enable/disable/delete and an **"add / edit → opens the modal editor"** affordance.
   Shown only to users with READ on the folder; editable only with WRITE/MANAGE_ACL
   (§5). Distinguishes folder-only vs `recursive` bindings.

2. **Binding editor (modal overlay).** The full add/edit surface, opened over the
   drawer/browser. One **generic form renderer** driven by each plug-in's published
   **config field descriptors** (§6.1) via `GET /action-types` — a
   **field-type → widget registry** (string, number, select, `secret`, `folder`
   picker, `principal` picker, repeatable `group`, …). A new plug-in that composes
   existing field types needs **no frontend change**; only a brand-new field *type*
   adds a widget. The built-ins render through this same path:
   - *Move on approve/reject* — destination **folder pickers** for `on_approved` /
     `on_rejected`.
   - *Notify* — **user/role recipient picker** (LDAP directory autocomplete,
     reusing the @mention component), event-type multiselect, template choice.
   - *Sorter* — pick a **classifier set**, then edit the **routing table**
     (classification → threshold + destination folder + priority), with an inline
     link to that set's **test panel** to calibrate thresholds.
   - *Webhook* — URL, auth (bearer / OAuth2 client-credentials), event selection,
     `grant_read` + default read-back `format`, timeout / retries. **Secret fields
     are write-only** — entered once, never rendered back (§11).

   The binding editor also carries the **binding-level filters** (§5): the
   **Trigger on events** checklist and a **MIME types** list-management field
   (add/remove entries, exact or `image/*` wildcards) — both apply to any action.
   For the notify action the **Email template** field is a live dropdown of stored
   templates (§7.2.1) that updates as templates are added (shared store).

3. **Classifier set editor** (§7.3.1) — a **tenant-level admin** surface (sets are
   reused across folders). *As built* it is a **tab in the System configuration hub**
   (see below), not under one folder:
   - List / create / rename / delete sets.
   - Edit classifications and terms (term string, `distance`, `weight`, with inline
     help for the `*` / `?` / `#` wildcards).
   - **Import / export** SmolDocBot YAML (file upload / download).
   - **Test panel** — the highest-value surface: paste text *or* pick a file, call
     `POST /classifier-sets/{id}/test`, and show **per-classification scores +
     matched terms**, so an author sets thresholds against real numbers (scores are
     unbounded weighted sums). Gated by READ on any file tested.
   - Tenant-admin / configured-role gated (§7.3.1).

4. **Run log / activity view.** Per-folder or per-binding history from `action_run`
   (§10): event, action, outcome (done / failed / skipped), detail, timestamp — the
   debugging surface ("why didn't my sorter move this file?"). Offers a **retry** on
   failed webhook runs.

5. **Reused pickers.** The destination **folder picker** and **user/role picker**
   (LDAP directory) several forms above depend on; likely already present in the SPA.

6. **Admin navigation (as built).** Tenant-level configuration is consolidated into a
   tabbed **System** hub (`/admin/ops`) and a **Security** hub (`/admin/security`),
   so it isn't scattered under Users & Roles:
   - **System configuration** — tabs: *Storage & sync · Integrations · Classifier
     sets · Email templates*. The **Email templates** tab has two sub-areas:
     **Account** (user/account templates from `ldap_manager`) and **Event
     notifications** (the notify templates, §7.2.1) — both under one interface.
   - **Security** — tabs: *Audit · Security · Events* (moved out of Users & Roles).

**Related dependency (discussion-service UI).** The move-on-review action needs the
review UI (owned by discussion / frontend) to expose explicit **Approve** /
**Reject** controls that drive the new `review.approved` / `review.rejected`
transitions (§4), replacing the free-form `outcome`.

---

## 13. End-user documentation (frontend repo)

Per the platform convention, **end-user docs live in the frontend repo**, not here,
and must be written against the current internal spec and service behavior (review
this document and the relevant core / CSAI / discussion sources when authoring —
these features' behavior is owned across several services). The documentation set
folder_actions requires:

1. **Concept overview.** What folder actions are, the event-driven model (an action
   runs when a recognized event happens to a file in a bound folder), the four
   built-in action types at a glance, and who may configure them (folder
   WRITE/MANAGE_ACL; classifier sets are tenant-admin). Audience: all users.

2. **Configuring a folder action.** Walkthrough of the folder actions panel (§12.1):
   add / edit / delete a binding, pick trigger events, folder-only vs `recursive`,
   enable / disable, and read the run log. Permissions called out.

3. **Per-action guides** (task-oriented):
   - *Move on approval / rejection* — wiring approve → folder and reject → folder;
     depends on a review being raised (link to the reviews doc).
   - *Notify a user or group* — choosing user / role recipients, which events, that
     delivery is **real-time email** (a role fans out to all members), and that an
     admin must have SMTP configured.
   - *Automatic sorter* — the end-to-end flow: create or import a classifier set →
     tune with the test panel → set routing (classification → threshold →
     destination). Explains that **scores are weighted sums, not percentages**, that
     sorting runs **after content extraction completes**, and what happens on
     no-match (file stays put) and multi-match (highest score, then priority).
   - *Webhook* — configuring URL + auth, selecting events, and the response contract
     (`move_to`, `metadata`) at a user level.

4. **Classifier authoring guide** (the deepest doc). Classifications, terms, **fuzzy
   distance** (Levenshtein), **weights**, and **wildcards** (`*` any, `?` word, `#`
   number); how scoring accumulates into an **unbounded weighted sum**; using the
   **test panel** to calibrate thresholds against real documents; import / export of
   SmolDocBot YAML. Audience: power users / tenant admins.

5. **Webhook integration guide** (developer-facing). The POST request payload; auth
   setup (static bearer vs OAuth2 client-credentials); the **scoped read-back**
   endpoint with **rendition `format`** options (original / markdown / pdf / …); the
   **extraction-delay** behavior (`202` + `Retry-After`; prefer binding to
   `conversion.complete`); the JSON **response contract**; retry / failure
   semantics; and security guidance (short-lived single-file token, verify the
   bearer). Audience: integrators building the remote endpoint.

6. **Troubleshooting / FAQ.** Why an action didn't fire (events disabled by the
   operator, insufficient permissions, extraction not yet complete, no classification
   cleared its threshold, webhook failed after retries), how to read the run log, and
   "notification email never arrived" (SMTP not configured).

7. **Admin prerequisites note.** The operator-side conditions actions depend on: the
   core **event publisher enabled** (off by default), **SMTP** for notify, **CSAI**
   for the sorter / renditions, and the **discussion** service for reviews. Points
   admins at the relevant deployment config.

**Authoring dependencies.** Because these docs describe behavior owned across
services, writing them requires reviewing: this spec (actions, events, security),
the `EVENT_CONTRACT.md` recognized types, the CSAI rendition / `text` endpoints, and
the discussion review lifecycle.

---

## 14. Open items / future

- **Confirmed decisions (locked):** single recognized stream; explicit
  `review.approved`/`review.rejected`; text from CSAI via `conversion.complete`;
  per-folder classifier sets; sorter tie-break = highest-score-then-priority;
  own Postgres DB; ACL-governed bindings; dedicated service-principal identity.
- **Future:** additional built-in actions (tag/metadata stamp, copy-instead-of-
  move, retention/cull triggers); digest-style batched notifications; per-binding
  dry-run/preview; metrics/Prometheus surface; transport swap (Kafka/NATS) behind
  the same `EventSource` interface.
