# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Act on extracted values (convert_search_ai EXTRACTION_PROMPT_SETS.md §6.2).

Fires on ``extraction.complete`` for one prompt set. Reads the values from
csai (as the service principal, READ-checked like any read) and runs an ordered
list of rules. Each rule has conditions on the values, typed by the prompt
set's field definitions, and one action:

- ``write_metadata``: every found value as metadata ``<prefix><field>`` (with
  its unit), plus ``<prefix>_source`` = version and revision, so a stale value
  is recognisable;
- ``move`` / ``copy``: file the document into a folder, optionally into a
  sub-folder and under a name made from the values;
- ``rename`` in place, from the values;
- ``raise_review`` and ``notify``: the existing actions, with the values
  available to the email template as ``{field}``;
- ``flag``: ``extract.status = needs_attention`` with the rule's name.

``match: first`` runs the first rule whose conditions hold; ``all`` runs every
such rule in order.

Conditions are checked against the set when the binding is SAVED (an unknown
field, an operator the field's type does not have, a value that does not
parse, a unit that is not the field's are refused then), and again at run
time.

Whatever this action files is marked ``fa.filed_by = <binding>@<version>``,
which the extract action reads, so filing a document never makes it be
extracted again."""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any, Optional

from pydantic import BaseModel, field_validator

from ..csai_client import CsaiError
from .base import ActionContext, ActionResult, FieldDescriptor, FieldOption, register
from .extract import FILED_BY

log = logging.getLogger("folder_actions.plugins.extraction_rules")

ACTIONS = ("write_metadata", "move", "copy", "rename", "raise_review", "notify", "flag")
_SPECIAL = {"missing_required", "any_dropped"}

OPS_BY_TYPE = {
    "any": {"found", "not_found"},
    "string": {"equals", "not_equals", "in", "contains", "matches"},
    "text": {"contains", "matches"},
    "enum": {"equals", "not_equals", "in"},
    "number": {"eq", "lt", "lte", "gt", "gte", "between"},
    "integer": {"eq", "lt", "lte", "gt", "gte", "between"},
    "date": {"before", "after", "within_days", "on"},
    "boolean": {"is_true", "is_false"},
    "list": {"contains"},
}
_NO_VALUE = {"found", "not_found", "is_true", "is_false"}
OPERATORS = sorted(set().union(*OPS_BY_TYPE.values()))
_MAX_SUBFOLDER_DEPTH = 3


class RuleError(ValueError):
    """A rule cannot be used with this prompt set (refused at save, 422)."""


# ------------------------------------------------------------------ values

def _number(text: str, unit: str, where: str) -> float:
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([^\d\s].*)?\s*", str(text))
    if not m:
        raise RuleError(f"{where}: {text!r} is not a number")
    if m.group(2) and m.group(2).strip() != (unit or ""):
        raise RuleError(f"{where}: unit {m.group(2).strip()!r} is not the field's "
                        f"unit {unit or '(none)'!r}")
    return float(m.group(1))


def _date(text: str, where: str, today: Optional[_dt.date] = None) -> _dt.date:
    today = today or _dt.date.today()
    t = str(text).strip().lower()
    m = re.fullmatch(r"today\s*([+-]\s*\d+)?\s*d?", t)
    if m:
        return today + _dt.timedelta(days=int((m.group(1) or "0").replace(" ", "")))
    try:
        return _dt.date.fromisoformat(t[:10])
    except ValueError:
        raise RuleError(f"{where}: {text!r} is not a date (YYYY-MM-DD or today+N)")


def check_condition(cond: dict, fields: dict) -> None:
    """Refuse a condition that cannot be evaluated against these fields."""
    name, op, value = cond.get("field") or "", cond.get("op") or "", cond.get("value")
    where = f"condition {name or op!r}"
    if op in _SPECIAL:
        return
    f = fields.get(name)
    if f is None:
        raise RuleError(f"{where}: the prompt set has no field {name!r}")
    ftype = f.get("type") or "string"
    if op not in OPS_BY_TYPE["any"] | OPS_BY_TYPE.get(ftype, set()):
        raise RuleError(f"{where}: {op!r} does not apply to a {ftype} field "
                        f"(use one of {', '.join(sorted(OPS_BY_TYPE['any'] | OPS_BY_TYPE.get(ftype, set())))})")
    if op in _NO_VALUE:
        return
    if value in (None, ""):
        raise RuleError(f"{where}: {op!r} needs a value")
    if ftype in ("number", "integer"):
        parts = str(value).split("..") if op == "between" else [value]
        if op == "between" and len(parts) != 2:
            raise RuleError(f"{where}: between takes 'low..high'")
        for p in parts:
            _number(p, f.get("unit", ""), where)
    elif ftype == "date" and op != "within_days":
        _date(value, where)
    elif ftype == "date":
        if not str(value).strip().isdigit():
            raise RuleError(f"{where}: within_days takes a number of days")
    elif op == "matches":
        try:
            re.compile(str(value))
        except re.error as e:
            raise RuleError(f"{where}: bad pattern ({e})")
    elif ftype == "enum" and op in ("equals", "not_equals", "in"):
        allowed = {str(a).strip().lower() for a in f.get("enum") or []}
        for v in (str(value).split(",") if op == "in" else [value]):
            if str(v).strip().lower() not in allowed:     # compared ignoring case
                raise RuleError(f"{where}: {str(v).strip()!r} is not one of the field's values")


def condition_holds(cond: dict, fields: dict, data: dict, info: dict,
                    today: Optional[_dt.date] = None) -> bool:
    op = cond.get("op") or ""
    if op == "missing_required":
        return any(f.get("required") and not (data.get(n) or {}).get("found")
                   for n, f in fields.items())
    if op == "any_dropped":
        return int((info.get("counts") or {}).get("dropped") or 0) > 0
    name = cond.get("field") or ""
    f = fields.get(name) or {}
    v = data.get(name) or {}
    found = bool(v.get("found"))
    if op == "found":
        return found
    if op == "not_found":
        return not found
    if not found:
        return False
    val, arg, ftype = v.get("value"), cond.get("value"), f.get("type") or "string"
    try:
        if op in ("equals", "not_equals"):
            same = str(val).strip().lower() == str(arg).strip().lower()
            return same if op == "equals" else not same
        if op == "in":
            return str(val).strip().lower() in {a.strip().lower() for a in str(arg).split(",")}
        if op == "contains":
            if isinstance(val, list):
                return any(str(arg).strip().lower() == str(x).strip().lower() for x in val)
            return str(arg).strip().lower() in str(val).lower()
        if op == "matches":
            return re.search(str(arg), str(val)) is not None
        if op in ("is_true", "is_false"):
            return bool(val) is (op == "is_true")
        if ftype in ("number", "integer"):
            x = float(val)
            if op == "between":
                lo, hi = (_number(p, f.get("unit", ""), "value") for p in str(arg).split(".."))
                return lo <= x <= hi
            y = _number(arg, f.get("unit", ""), "value")
            return {"eq": x == y, "lt": x < y, "lte": x <= y, "gt": x > y, "gte": x >= y}[op]
        if ftype == "date":
            d = _dt.date.fromisoformat(str(val)[:10])
            if op == "within_days":
                t = today or _dt.date.today()
                return t <= d <= t + _dt.timedelta(days=int(arg))
            ref = _date(arg, "value", today)
            return {"before": d < ref, "after": d > ref, "on": d == ref}[op]
    except (ValueError, TypeError, RuleError):
        return False
    return False


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def render(template: str, data: dict, fields: dict) -> str:
    """``{field}`` → the value (with unit for numbers); unknown or not-found
    fields render empty. Path- and name-safe."""
    def sub(m):
        name = m.group(1)
        v = data.get(name) or {}
        if not v.get("found"):
            return ""
        val = v.get("value")
        text = ", ".join(map(str, val)) if isinstance(val, list) else str(val)
        if v.get("unit") and (fields.get(name) or {}).get("type") in ("number", "integer"):
            text = f"{text} {v['unit']}"
        return _UNSAFE.sub("-", text)
    out = re.sub(r"\{([A-Za-z_][A-Za-z0-9_.\-]*)\}", sub, template or "")
    return re.sub(r"\s+", " ", out).strip(" .-")


def template_fields(template: str):
    return re.findall(r"\{([A-Za-z_][A-Za-z0-9_.\-]*)\}", template or "")


# ------------------------------------------------------------------ plug-in

@register
class ExtractionRulesAction:
    type_name = "extraction_rules"
    label = "Act on extracted values"
    supported_events = frozenset({"extraction.complete"})
    # Files documents unattended: never re-run on its own moves (§3.3).
    auto_moves = True

    class ConfigModel(BaseModel):
        prompt_set: str
        match: str = "first"                         # first | all
        rules: list[dict] = []
        fields: dict[str, dict] = {}                 # the set's fields, cached at save

        @field_validator("rules", mode="before")
        @classmethod
        def _rows(cls, v):
            out = []
            for r in v or []:
                if not isinstance(r, dict):
                    continue
                r = dict(r)
                r["conditions"] = [c for c in (r.get("conditions") or [])
                                   if isinstance(c, dict) and (c.get("op") or c.get("field"))]
                for k in ("reviewers", "recipients"):
                    r[k] = [x.get("principal") if isinstance(x, dict) else x
                            for x in (r.get(k) or []) if (x.get("principal")
                                                           if isinstance(x, dict) else x)]
                out.append(r)
            return out

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]:
        actions = [FieldOption(value="write_metadata", label="Write the values as metadata"),
                   FieldOption(value="move", label="Move the document"),
                   FieldOption(value="copy", label="Copy the document"),
                   FieldOption(value="rename", label="Rename the document"),
                   FieldOption(value="raise_review", label="Request a review"),
                   FieldOption(value="notify", label="Email people"),
                   FieldOption(value="flag", label="Flag for attention")]
        ops = [FieldOption(value=o, label=o.replace("_", " ")) for o in
               ["found", "not_found", "equals", "not_equals", "in", "contains", "matches",
                "eq", "lt", "lte", "gt", "gte", "between", "before", "after", "on",
                "within_days", "is_true", "is_false", "missing_required", "any_dropped"]]
        return [
            FieldDescriptor(key="prompt_set", label="Prompt set", type="ref", required=True,
                            options_source="prompt_sets",
                            help="Rules are written against this set's fields; other "
                                 "extractions are ignored."),
            FieldDescriptor(key="match", label="Which rules run", type="select", default="first",
                            options=[FieldOption(value="first", label="The first that applies"),
                                     FieldOption(value="all", label="Every rule that applies")]),
            FieldDescriptor(
                key="rules", label="Rules", type="group",
                help="Each rule: conditions (all must hold; none = always) and one action. "
                     "Numbers may carry the field's unit (900 mm); dates are YYYY-MM-DD or "
                     "today+90; 'between' takes low..high; 'in' takes a comma list.",
                item_fields=[
                    FieldDescriptor(key="name", label="Rule name", type="string"),
                    FieldDescriptor(
                        key="conditions", label="When", type="group",
                        item_fields=[
                            FieldDescriptor(key="field", label="Field", type="string"),
                            FieldDescriptor(key="op", label="Is", type="select", options=ops),
                            FieldDescriptor(key="value", label="Value", type="string"),
                        ]),
                    FieldDescriptor(key="action", label="Then", type="select", required=True,
                                    options=actions),
                    FieldDescriptor(key="destination", label="Folder", type="folder",
                                    visible_when={"key": "action", "equals": ["move", "copy"]}),
                    FieldDescriptor(key="subfolder", label="Sub-folder from the values",
                                    type="string", help="e.g. {manufacturer}/{fire_rating}",
                                    visible_when={"key": "action", "equals": ["move", "copy"]}),
                    FieldDescriptor(key="name_template", label="New name from the values",
                                    type="string", help="e.g. {manufacturer} {model}; the "
                                    "extension is kept",
                                    visible_when={"key": "action",
                                                  "equals": ["move", "copy", "rename"]}),
                    FieldDescriptor(key="metadata_prefix", label="Metadata key prefix",
                                    type="string", help="Default: extract.<prompt set>.",
                                    visible_when={"key": "action", "equals": "write_metadata"}),
                    FieldDescriptor(key="reviewers", label="Reviewers", type="principal",
                                    visible_when={"key": "action", "equals": "raise_review"}),
                    FieldDescriptor(key="recipients", label="Recipients", type="principal",
                                    visible_when={"key": "action", "equals": "notify"}),
                    FieldDescriptor(key="template", label="Email template", type="ref",
                                    options_source="notify_templates",
                                    visible_when={"key": "action", "equals": "notify"}),
                ]),
        ]

    # ------------------------------------------------------------------ on save
    @classmethod
    def prepare_config(cls, cfg: dict, csai, tenant: str) -> dict:
        """Check every rule against the prompt set's fields now, so a bad rule is
        refused when it is written, not discovered when a document arrives."""
        m = cls.ConfigModel(**cfg)
        try:
            sets = {s["id"]: s for s in csai.list_prompt_sets(tenant)}
        except Exception:
            raise RuleError("the prompt sets cannot be read right now; try again")
        s = sets.get(m.prompt_set)
        if s is None:
            raise RuleError(f"prompt set {m.prompt_set!r} is not published")
        fields = {f["name"]: f for f in s.get("fields") or []}
        if m.match not in ("first", "all"):
            raise RuleError("match must be 'first' or 'all'")
        for i, r in enumerate(m.rules, 1):
            label = r.get("name") or f"rule {i}"
            if r.get("action") not in ACTIONS:
                raise RuleError(f"{label}: choose an action")
            for c in r.get("conditions") or []:
                try:
                    check_condition(c, fields)
                except RuleError as e:
                    raise RuleError(f"{label}: {e}")
            for key in ("subfolder", "name_template"):
                for name in template_fields(r.get(key) or ""):
                    if name not in fields:
                        raise RuleError(f"{label}: {{{name}}} is not a field of the set")
            if r.get("action") in ("move", "copy") and not r.get("destination"):
                raise RuleError(f"{label}: choose a folder")
            if r.get("action") == "rename" and not r.get("name_template"):
                raise RuleError(f"{label}: give the new name")
            if len([p for p in (r.get("subfolder") or "").split("/") if p.strip()]) \
                    > _MAX_SUBFOLDER_DEPTH:
                raise RuleError(f"{label}: at most {_MAX_SUBFOLDER_DEPTH} sub-folder levels")
        out = dict(cfg)
        out["fields"] = fields
        return out

    # ------------------------------------------------------------------ run
    def execute(self, event: dict, config: "ExtractionRulesAction.ConfigModel",
                ctx: ActionContext) -> ActionResult:
        ps = event.get("prompt_set") or {}
        if ps.get("id") != config.prompt_set:
            return ActionResult.skipped("other_prompt_set")
        file_uid = event.get("file_uid") or ""
        version = event.get("version") or ""
        try:
            entries = ctx.csai.get_extractions(
                ctx.tenant, file_uid, version=version,
                entry=(event.get("payload") or {}).get("entry") or "")
        except CsaiError as e:
            if e.retryable:
                return ActionResult.failed("csai_unavailable", retryable=True, status=e.status)
            return ActionResult.failed("values_unreadable", status=e.status)
        if not entries:
            return ActionResult.failed("values_unreadable")
        entry = entries[0]
        data = entry.get("data") or {}
        fields = {f["name"]: f for f in entry.get("fields") or []} or config.fields
        today = _dt.date.today()
        ran = []
        for i, r in enumerate(config.rules, 1):
            conds = r.get("conditions") or []
            try:
                for c in conds:
                    check_condition(c, fields)
            except RuleError as e:
                # The set changed under a saved rule: report, don't guess.
                return ActionResult.failed("rule_no_longer_valid", rule=r.get("name") or i,
                                           error=str(e)[:200])
            if not all(condition_holds(c, fields, data, event, today) for c in conds):
                continue
            outcome = self._act(r, event, entry, fields, data, ctx, config)
            ran.append({"rule": r.get("name") or i, "action": r.get("action"), **outcome})
            if outcome.get("retryable"):
                return ActionResult.failed("action_failed", retryable=True, ran=ran)
            if config.match == "first":
                break
        if not ran:
            return ActionResult.skipped("no_rule_applied")
        return ActionResult.done(ran=ran, prompt_set=config.prompt_set,
                                 revision=ps.get("revision"))

    # ------------------------------------------------------------------ actions
    def _act(self, r, event, entry, fields, data, ctx, config) -> dict:
        action = r.get("action")
        file_uid = event.get("file_uid") or ""
        version = event.get("version") or ""
        marker = f"{ctx.binding_id}@{version}"
        try:
            if action == "write_metadata":
                ps = event.get("prompt_set") or {}
                prefix = r.get("metadata_prefix") or f"extract.{ps.get('id', 'set')}."
                n = 0
                for name, v in data.items():
                    if not v.get("found"):
                        continue
                    val = v.get("value")
                    text = ", ".join(map(str, val)) if isinstance(val, list) else str(val)
                    if v.get("unit"):
                        text = f"{text} {v['unit']}"
                    ctx.core.set_metadata(file_uid, prefix + name, text)
                    n += 1
                ctx.core.set_metadata(file_uid, prefix + "_source",
                                      f"{version} r{ps.get('revision')}")
                return {"keys": n}
            if action == "flag":
                ctx.core.set_metadata(file_uid, "extract.status", "needs_attention")
                ctx.core.set_metadata(file_uid, "extract.reason", str(r.get("name") or "rule"))
                return {}
            if action in ("move", "copy", "rename"):
                info = ctx.core.stat(file_uid)
                new_name = self._new_name(r.get("name_template"), info.name, data, fields)
                if action == "rename":
                    if new_name and new_name != info.name:
                        ctx.core.rename(file_uid, new_name)
                    return {"name": new_name or info.name}
                dest = r.get("destination")
                for part in [p for p in render(r.get("subfolder") or "", data, fields)
                             .split("/") if p.strip()][:_MAX_SUBFOLDER_DEPTH]:
                    dest = ctx.core.ensure_folder(dest, part.strip())
                if action == "move":
                    if getattr(info, "parent_uid", None) != dest or (new_name and
                                                                     new_name != info.name):
                        ctx.core.set_metadata(file_uid, FILED_BY, marker)
                        ctx.core.move(file_uid, dest, new_name=new_name or None)
                    return {"to": dest, "name": new_name or info.name}
                new_uid = ctx.core.copy(file_uid, dest, new_name=new_name or None)
                if new_uid:
                    try:
                        copy_version = getattr(ctx.core.stat(new_uid), "version", "") or version
                    except Exception:
                        copy_version = version
                    ctx.core.set_metadata(new_uid, FILED_BY, f"{ctx.binding_id}@{copy_version}")
                return {"to": dest, "copy": new_uid or None}
            if action == "raise_review":
                from .raise_review import RaiseReviewAction
                res = RaiseReviewAction().execute(
                    event, RaiseReviewAction.ConfigModel(reviewers=r.get("reviewers") or []), ctx)
                return {"status": res.status, "retryable": res.retryable}
            if action == "notify":
                from .notify import NotifyAction
                values = {n: render("{" + n + "}", data, fields) for n in fields}
                res = NotifyAction().execute(
                    {**event, "template_values": values},
                    NotifyAction.ConfigModel(recipients=r.get("recipients") or [],
                                             template=r.get("template") or None), ctx)
                return {"status": res.status}
        except Exception as e:
            log.warning("extraction_rules: %s on %s failed", action, file_uid, exc_info=True)
            return {"error": str(e)[:200], "retryable": True}
        return {"error": f"unknown action {action!r}"}

    @staticmethod
    def _new_name(template: Optional[str], current: str, data, fields) -> str:
        if not template:
            return ""
        base = render(template, data, fields)
        if not base:
            return ""
        ext = ""
        if "." in current:
            ext = current[current.rfind("."):]
        return base if base.lower().endswith(ext.lower()) else base + ext
