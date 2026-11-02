"""Acting on extracted values (convert_search_ai EXTRACTION_PROMPT_SETS.md §6.2).

- conditions are typed by the prompt set's fields: units must be the field's,
  dates accept today+N, enum values must be the field's, and a condition
  that cannot apply is refused when the binding is saved;
- each action against a fake core: metadata with units and the source,
  move / copy into a sub-folder named from the values under a name made from
  them (extension kept), rename, flag; the filing marker the extract action
  reads; notify gets the values as placeholders;
- first vs all; other prompt sets ignored; a csai outage is retried."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest

from folder_actions.csai_client import CsaiError
from folder_actions.plugins.base import ActionContext
from folder_actions.plugins.extract import FILED_BY
from folder_actions.plugins.extraction_rules import (ExtractionRulesAction, RuleError,
                                                     check_condition, condition_holds, render)

FIELDS = {
    "fire_rating": {"name": "fire_rating", "type": "enum", "enum": ["EI 30", "EI 60", "EI 90"],
                    "required": True, "label": "Fire rating"},
    "width": {"name": "width", "type": "number", "unit": "mm"},
    "manufacturer": {"name": "manufacturer", "type": "string", "required": True},
    "expiry": {"name": "expiry", "type": "date"},
    "fsc": {"name": "fsc", "type": "boolean"},
    "standards": {"name": "standards", "type": "list"},
    "epd": {"name": "epd", "type": "string"},
}
TODAY = dt.date(2026, 11, 2)


def v(value, unit=None, found=True):
    return {"found": found, "value": value if found else None, "unit": unit,
            "evidence": {"quote": "q", "offset": 1} if found else None}


DATA = {"fire_rating": v("EI 60"), "width": v(926, "mm"), "manufacturer": v("Acme/Doors"),
        "expiry": v("2026-12-15"), "fsc": v(True), "standards": v(["EN 1634-1", "EN 13501-2"]),
        "epd": v(None, found=False)}


@pytest.mark.parametrize("cond,expected", [
    ({"field": "fire_rating", "op": "equals", "value": "ei 60"}, True),
    ({"field": "fire_rating", "op": "in", "value": "EI 30, EI 90"}, False),
    ({"field": "width", "op": "between", "value": "900 mm..1000"}, True),
    ({"field": "width", "op": "gt", "value": "926"}, False),
    ({"field": "width", "op": "gte", "value": "926 mm"}, True),
    ({"field": "expiry", "op": "within_days", "value": "90"}, True),
    ({"field": "expiry", "op": "before", "value": "today+30"}, False),
    ({"field": "expiry", "op": "after", "value": "2026-12-01"}, True),
    ({"field": "fsc", "op": "is_true"}, True),
    ({"field": "standards", "op": "contains", "value": "en 13501-2"}, True),
    ({"field": "manufacturer", "op": "matches", "value": r"^Acme"}, True),
    ({"field": "epd", "op": "not_found"}, True),
    ({"field": "epd", "op": "equals", "value": "x"}, False),          # not found never matches
    ({"op": "missing_required"}, False),
    ({"op": "any_dropped"}, True),
])
def test_conditions(cond, expected):
    check_condition(cond, FIELDS)
    assert condition_holds(cond, FIELDS, DATA, {"counts": {"dropped": 1}}, TODAY) is expected


def test_a_missing_required_field():
    data = {**DATA, "manufacturer": v(None, found=False)}
    assert condition_holds({"op": "missing_required"}, FIELDS, data, {}, TODAY)


@pytest.mark.parametrize("cond,needle", [
    ({"field": "nope", "op": "found"}, "has no field"),
    ({"field": "width", "op": "contains", "value": "9"}, "does not apply"),
    ({"field": "width", "op": "gt", "value": "90 cm"}, "not the field's unit"),
    ({"field": "width", "op": "between", "value": "900"}, "low..high"),
    ({"field": "expiry", "op": "before", "value": "next year"}, "not a date"),
    ({"field": "fire_rating", "op": "equals", "value": "EI 120"}, "not one of"),
    ({"field": "manufacturer", "op": "matches", "value": "(["}, "bad pattern"),
    ({"field": "manufacturer", "op": "equals"}, "needs a value"),
])
def test_bad_conditions_are_refused(cond, needle):
    with pytest.raises(RuleError, match=needle):
        check_condition(cond, FIELDS)


def test_render_values_into_safe_names():
    assert render("{manufacturer} {fire_rating} {width}", DATA, FIELDS) == "Acme-Doors EI 60 926 mm"
    assert render("{epd} {fire_rating}", DATA, FIELDS) == "EI 60"


# ------------------------------------------------------------------ the action

class Csai:
    def __init__(self, entries=None, sets=None, fail=None):
        self.entries = entries if entries is not None else [{"data": DATA,
                                                            "fields": list(FIELDS.values())}]
        self.sets = sets if sets is not None else [{"id": "ps_door", "fields": list(FIELDS.values())}]
        self.fail = fail

    def get_extractions(self, tenant, file_uid, **kw):
        if self.fail:
            raise self.fail
        return self.entries

    def list_prompt_sets(self, tenant):
        return self.sets


class Core:
    def __init__(self):
        self.meta, self.calls = {}, []
        self.nodes = {"d1": NS(name="sheet.pdf", parent_uid="IN", version="v2")}

    def stat(self, uid):
        return self.nodes[uid]

    def set_metadata(self, uid, k, val):
        self.meta.setdefault(uid, {})[k] = val
        return True

    def move(self, uid, dest, new_name=None, normalize=True):
        self.calls.append(("move", uid, dest, new_name))
        return True

    def copy(self, uid, dest, new_name=None):
        self.calls.append(("copy", uid, dest, new_name))
        self.nodes["c1"] = NS(name=new_name, parent_uid=dest, version="v9")
        return "c1"

    def rename(self, uid, name):
        self.calls.append(("rename", uid, name))

    def ensure_folder(self, parent, name):
        self.calls.append(("folder", parent, name))
        return f"{parent}/{name}"


def ctx(csai=None, core=None):
    return ActionContext(tenant="t", binding_id="b5", folder_uid="IN", core=core or Core(),
                         csai=csai or Csai(), directory=None, discussion=None, mailer=None,
                         secrets=None, mime=None, store=None, config=None)


EVENT = {"type": "extraction.complete", "file_uid": "d1", "version": "v2", "tenant": "t",
         "prompt_set": {"id": "ps_door", "name": "Door", "revision": 3},
         "payload": {"entry": "k1"}, "counts": {"dropped": 0}}


def run(rules, match="first", **kw):
    c = kw.pop("ctx", None) or ctx()
    cfg = ExtractionRulesAction.ConfigModel(prompt_set="ps_door", match=match, rules=rules)
    return ExtractionRulesAction().execute({**EVENT, **kw}, cfg, c), c


def test_metadata_with_units_and_source():
    r, c = run([{"name": "meta", "action": "write_metadata"}])
    meta = c.core.meta["d1"]
    assert r.status == "done"
    assert meta["extract.ps_door.width"] == "926 mm"
    assert meta["extract.ps_door.standards"] == "EN 1634-1, EN 13501-2"
    assert meta["extract.ps_door._source"] == "v2 r3"
    assert "extract.ps_door.epd" not in meta                          # not found: not written


def test_move_into_a_subfolder_named_from_values_with_a_new_name_and_marker():
    r, c = run([{"action": "move", "destination": "FILED", "subfolder": "{manufacturer}/{fire_rating}",
                 "name_template": "{manufacturer} {width}"}])
    assert ("folder", "FILED", "Acme-Doors") in c.core.calls
    assert ("move", "d1", "FILED/Acme-Doors/EI 60", "Acme-Doors 926 mm.pdf") in c.core.calls
    assert c.core.meta["d1"][FILED_BY] == "b5@v2"


def test_copy_marks_the_copy_at_its_own_version():
    r, c = run([{"action": "copy", "destination": "ARCHIVE"}])
    assert ("copy", "d1", "ARCHIVE", None) in c.core.calls
    assert c.core.meta["c1"][FILED_BY] == "b5@v9" and FILED_BY not in c.core.meta.get("d1", {})


def test_first_versus_all_and_conditions():
    rules = [{"name": "expiring", "conditions": [{"field": "expiry", "op": "within_days",
                                                  "value": "36500"}], "action": "flag"},
             {"name": "meta", "action": "write_metadata"},
             {"name": "never", "conditions": [{"field": "fsc", "op": "is_false"}],
              "action": "rename", "name_template": "x"}]
    r, c = run(rules, "first")
    assert [x["rule"] for x in r.detail["ran"]] == ["expiring"]
    r, c = run(rules, "all")
    assert [x["rule"] for x in r.detail["ran"]] == ["expiring", "meta"]
    assert c.core.meta["d1"]["extract.status"] == "needs_attention"
    assert c.core.meta["d1"]["extract.reason"] == "expiring"


def test_rename_keeps_the_extension():
    r, c = run([{"action": "rename", "name_template": "{fire_rating} door"}])
    assert ("rename", "d1", "EI 60 door.pdf") in c.core.calls


def test_notify_gets_the_values_as_placeholders(monkeypatch):
    seen = {}
    from folder_actions.plugins import notify

    def fake(self, event, config, ctx):
        seen.update(event["template_values"], recipients=config.recipients)
        return NS(status="done", retryable=False)
    monkeypatch.setattr(notify.NotifyAction, "execute", fake)
    run([{"action": "notify", "recipients": [{"principal": "role:qa"}]}])
    assert seen["fire_rating"] == "EI 60" and seen["width"] == "926 mm"
    assert seen["recipients"] == ["role:qa"]


def test_other_sets_are_ignored_and_outages_retried():
    r, _ = run([{"action": "flag"}], prompt_set={"id": "ps_other", "revision": 1})
    assert r.detail["reason"] == "other_prompt_set"
    r, _ = run([{"action": "flag"}], ctx=ctx(csai=Csai(fail=CsaiError(503))))
    assert r.status == "failed" and r.retryable
    r, _ = run([{"action": "flag"}], ctx=ctx(csai=Csai(fail=CsaiError(404))))
    assert r.status == "failed" and not r.retryable


def test_a_rule_the_set_no_longer_supports_fails_visibly():
    r, _ = run([{"conditions": [{"field": "gone", "op": "found"}], "action": "flag"}])
    assert r.status == "failed" and r.detail["reason"] == "rule_no_longer_valid"


# ------------------------------------------------------------------ on save

def test_save_checks_every_rule_against_the_set():
    good = {"prompt_set": "ps_door", "rules": [
        {"name": "expiring", "conditions": [{"field": "expiry", "op": "within_days", "value": "90"}],
         "action": "flag"},
        {"name": "file", "action": "move", "destination": "F", "subfolder": "{manufacturer}"}]}
    out = ExtractionRulesAction.prepare_config(good, Csai(), "t")
    assert set(out["fields"]) == set(FIELDS)
    for bad, needle in [
        ({**good, "prompt_set": "ps_missing"}, "not published"),
        ({**good, "rules": [{"action": "flag", "conditions": [{"field": "width", "op": "gt",
                                                               "value": "1 m"}]}]}, "unit"),
        ({**good, "rules": [{"action": "move", "destination": "F",
                             "name_template": "{nope}"}]}, "is not a field"),
        ({**good, "rules": [{"action": "move"}]}, "choose a folder"),
        ({**good, "rules": [{"action": "explode"}]}, "choose an action"),
        ({**good, "rules": [{"action": "move", "destination": "F",
                             "subfolder": "{epd}/{epd}/{epd}/{epd}"}]}, "sub-folder levels"),
    ]:
        with pytest.raises(RuleError, match=needle):
            ExtractionRulesAction.prepare_config(bad, Csai(), "t")
