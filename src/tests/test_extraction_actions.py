"""Extraction in folder automation (convert_search_ai EXTRACTION_PROMPT_SETS.md
§6.1, §6.3).

- the ``extract`` action queues one csai job per prompt set (fixed, or chosen
  by classification), passes pinned revisions, skips a version the rules
  already filed, and maps csai's refusals to retryable or terminal failures;
- saving a binding pins application-managed sets (and every set with pin_all);
- a binding's events must be ones its action handles;
- the webhook opts into extraction events and merges the values under
  ``extracted``: filtered by set, fields, evidence off by default, capped,
  and recorded in the run log without values."""
import json
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException

from folder_actions.api import _check_events
from folder_actions.csai_client import CsaiError, TextNotReady
from folder_actions.plugins.base import ActionContext
from folder_actions.plugins.extract import FILED_BY, ExtractAction
from folder_actions.plugins.webhook import EXTRACTED_MAX_BYTES, WebhookAction


class Csai:
    def __init__(self, text="", sets=(), entries=(), fail=None):
        self.text, self.sets, self.entries, self.fail = text, list(sets), list(entries), fail
        self.jobs, self.reads = [], []

    def get_text(self, file_uid, tenant):
        if self.text is None:
            raise TextNotReady("x")
        return self.text

    def start_extract_job(self, tenant, **kw):
        if self.fail:
            raise self.fail
        self.jobs.append(kw)
        return f"xj_{len(self.jobs)}"

    def list_prompt_sets(self, tenant):
        return self.sets

    def get_extractions(self, tenant, file_uid, **kw):
        self.reads.append(kw)
        return self.entries


class Core:
    def __init__(self, meta=None, version="v2"):
        self.meta, self.version = meta or {}, version

    def stat(self, uid):
        return NS(version=self.version, parent_uid="F")

    def metadata(self, uid):
        return self.meta


class Store:
    def get_classifier_set_full(self, tenant, set_id):
        return {"classifiers": [
            {"name": "door", "terms": [{"term": "fire door", "distance": 0, "weight": 2}]},
            {"name": "window", "terms": [{"term": "glazing", "distance": 0, "weight": 1}]}]}


def ctx(csai, core=None, store=None):
    return ActionContext(tenant="t", binding_id="b1", folder_uid="F", core=core or Core(),
                         csai=csai, directory=None, discussion=None, mailer=None, secrets=None,
                         mime=NS(resolve=lambda uid: "application/pdf"), store=store or Store(),
                         config=None)


EVENT = {"type": "conversion.complete", "file_uid": "d1", "version": "v2",
         "renditions": ["text", "pdf"], "tenant": "t"}


def test_fixed_sets_are_queued_with_their_pins():
    c = Csai()
    cfg = ExtractAction.ConfigModel(prompt_sets=["ps_a", "ps_b"], pinned={"ps_b": 3})
    r = ExtractAction().execute(EVENT, cfg, ctx(c))
    assert r.status == "done" and r.detail["jobs"] == ["xj_1", "xj_2"]
    assert [(j["prompt_set"], j["revision"], j["version"], j["binding_id"]) for j in c.jobs] == \
        [("ps_a", None, "v2", "b1"), ("ps_b", 3, "v2", "b1")]


def test_the_classification_picks_the_set():
    c = Csai(text="A fire door with glazing")
    cfg = ExtractAction.ConfigModel(mode="by_classification", classifier_set_id="cs",
                                    by_label=[{"label": "door", "prompt_set": "ps_door"},
                                              {"label": "window", "prompt_set": "ps_win"}])
    r = ExtractAction().execute(EVENT, cfg, ctx(c))
    assert r.status == "done" and [j["prompt_set"] for j in c.jobs] == ["ps_door"]
    none = Csai(text="unrelated")
    assert ExtractAction().execute(EVENT, cfg, ctx(none)).detail["reason"] == \
        "no_classification_with_a_prompt_set"


def test_no_text_and_already_filed_are_skipped():
    c = Csai()
    cfg = ExtractAction.ConfigModel(prompt_sets=["ps_a"])
    r = ExtractAction().execute({**EVENT, "renditions": ["pdf"]}, cfg, ctx(c))
    assert r.detail["reason"] == "no_text"
    filed = Core(meta={FILED_BY: "b7@v2"})
    assert ExtractAction().execute(EVENT, cfg, ctx(c, core=filed)).detail["reason"] == \
        "already_extracted_and_filed"
    newer = Core(meta={FILED_BY: "b7@v1"})                       # a new version: extract again
    assert ExtractAction().execute(EVENT, cfg, ctx(c, core=newer)).status == "done"


@pytest.mark.parametrize("err,retryable", [(CsaiError(503), True), (CsaiError(0), True),
                                           (CsaiError(429), True),
                                           (CsaiError(410, b'{"kind":"prompt_set_retired"}'), False),
                                           (CsaiError(403), False)])
def test_csai_refusals(err, retryable):
    r = ExtractAction().execute(EVENT, ExtractAction.ConfigModel(prompt_sets=["ps_a"]),
                                ctx(Csai(fail=err)))
    assert r.status == "failed" and r.retryable is retryable
    if err.status == 410:
        assert r.detail["reason"] == "prompt_set_retired"


def test_saving_pins_application_sets_and_drops_stale_pins():
    sets = [{"id": "ps_app", "managed": "application", "revision": 4},
            {"id": "ps_builder", "managed": "builder", "revision": 2}]
    out = ExtractAction.prepare_config(
        {"prompt_sets": ["ps_app", "ps_builder"], "pinned": {"ps_gone": 1}}, Csai(sets=sets), "t")
    assert out["pinned"] == {"ps_app": 4}
    out = ExtractAction.prepare_config({"prompt_sets": ["ps_app", "ps_builder"], "pin_all": True,
                                        "pinned": {"ps_app": 2}}, Csai(sets=sets), "t")
    assert out["pinned"] == {"ps_app": 2, "ps_builder": 2}         # an existing pin is kept


def test_a_binding_event_must_be_handled_by_its_action():
    _check_events(ExtractAction, ["conversion.complete"])
    with pytest.raises(HTTPException) as e:
        _check_events(ExtractAction, ["review.approved"])
    assert e.value.status_code == 422 and "review.approved" in e.value.detail


# ------------------------------------------------------------------ webhook

ENTRY = {"entry": "k1", "prompt_set": {"id": "ps_a", "revision": 2, "name": "Door"},
         "model_choice": {"used": "m", "fallback": False}, "source": "automation",
         "created_at": "2026-11-02T10:00:00Z",
         "fields": [{"name": "fire_rating", "label": "Fire rating", "type": "string"},
                    {"name": "width", "type": "number", "unit": "mm"},
                    {"name": "epd", "type": "string"}],
         "data": {"fire_rating": {"found": True, "value": "EI 60", "unit": None,
                                  "evidence": {"quote": "Fire resistance | EI 60", "offset": 40}},
                  "width": {"found": True, "value": 926, "unit": "mm",
                            "evidence": {"quote": "Width | 926 mm", "offset": 70}},
                  "epd": {"found": False, "value": None, "unit": None, "evidence": None}}}
XEVENT = {"type": "extraction.complete", "file_uid": "d1", "version": "v2", "tenant": "t",
          "source": "automation", "prompt_set": {"id": "ps_a", "name": "Door", "revision": 2},
          "model": {"used": "m"}, "counts": {"fields": 3, "found": 2},
          "payload": {"entry": "k1"}, "markup": {"status": "written", "name": "v2-extractpdf_a.pdf"}}


class WCore:
    def metadata(self, uid):
        return {}


class WStore:
    def get_secret(self, tenant, binding_id):
        return None


def wctx(csai):
    return ActionContext(tenant="t", binding_id="b1", folder_uid="F", core=WCore(), csai=csai,
                         directory=None, discussion=None, mailer=None, secrets=None,
                         mime=NS(resolve=lambda uid: "application/pdf"), store=WStore(),
                         config=None)


def send(monkeypatch, event, cfg, csai):
    sent = []

    def post(self, url, body, headers, timeout):
        sent.append(json.loads(body))
        return 200, b""
    monkeypatch.setattr(WebhookAction, "_post", post)
    r = WebhookAction().execute(event, WebhookAction.ConfigModel(url="https://x.example/h", **cfg),
                                wctx(csai))
    return r, (sent[0] if sent else None)


def test_the_webhook_merges_extracted_values(monkeypatch):
    c = Csai(entries=[ENTRY])
    r, body = send(monkeypatch, XEVENT, {}, c)
    ext = body["extracted"]
    assert ext["status"] == "complete" and ext["prompt_set"]["id"] == "ps_a"
    assert ext["values"]["width"] == {"found": True, "value": 926, "unit": "mm",
                                      "evidence": {"offset": 70}}             # no quote by default
    assert ext["fields"]["width"] == {"type": "number", "unit": "mm"}
    assert ext["counts"] == {"fields": 3, "found": 2} and ext["markup"]["status"] == "written"
    assert c.reads == [{"version": "v2", "entry": "k1"}]
    assert r.detail["extracted"] == {"status": "complete", "prompt_set": "ps_a", "revision": 2,
                                     "fields": 3, "quotes": False, "too_large": False}
    assert "EI 60" not in json.dumps(r.detail)                                # run log: no values


def test_webhook_filters_fields_only_found_and_quotes(monkeypatch):
    _r, body = send(monkeypatch, XEVENT, {"extraction_fields": [{"name": "fire_rating"},
                                                                {"name": "epd"}],
                                          "extraction_only_found": True,
                                          "extraction_include_evidence": True}, Csai(entries=[ENTRY]))
    assert list(body["extracted"]["values"]) == ["fire_rating"]
    assert body["extracted"]["values"]["fire_rating"]["evidence"]["quote"] == "Fire resistance | EI 60"


def test_webhook_set_filter_and_raw_schema_opt_in(monkeypatch):
    r, body = send(monkeypatch, XEVENT, {"extraction_prompt_sets": ["ps_other"]}, Csai())
    assert r.detail["reason"] == "other_prompt_set" and body is None
    raw = {**XEVENT, "prompt_set": None}
    r, body = send(monkeypatch, raw, {}, Csai(entries=[{**ENTRY, "prompt_set": None,
                                                        "fields": [], "schema": {"type": "object"}}]))
    assert r.detail["reason"] == "raw_schema_extraction"
    r, body = send(monkeypatch, raw, {"extraction_include_raw": True},
                   Csai(entries=[{**ENTRY, "prompt_set": None, "fields": [],
                                  "schema": {"type": "object"}}]))
    assert body["extracted"]["schema"] == {"type": "object"}


def test_a_failed_extraction_is_sent_without_values(monkeypatch):
    _r, body = send(monkeypatch, {**XEVENT, "type": "extraction.failed", "reason": "no_text"},
                    {}, Csai())
    assert body["extracted"] == {"status": "failed", "reason": "no_text",
                                 "prompt_set": XEVENT["prompt_set"], "model": {"used": "m"},
                                 "source": "automation"}


def test_other_events_can_carry_the_latest_values(monkeypatch):
    older = {**ENTRY, "entry": "k0", "created_at": "2026-11-01T10:00:00Z",
             "data": {"fire_rating": {"found": True, "value": "EI 30", "unit": None,
                                      "evidence": None}}}
    ev = {"type": "file.moved", "file_uid": "d1", "tenant": "t"}
    _r, body = send(monkeypatch, ev, {"extraction_attach": "latest"},
                    Csai(entries=[older, ENTRY]))
    assert body["extracted"]["values"]["fire_rating"]["value"] == "EI 60"
    _r, body = send(monkeypatch, ev, {"extraction_attach": "latest"}, Csai(entries=[]))
    assert body["extracted"] == {"status": "none"}
    _r, body = send(monkeypatch, ev, {}, Csai(entries=[ENTRY]))
    assert "extracted" not in body                                           # off by default


def test_oversized_values_lose_quotes_first_then_are_withheld(monkeypatch):
    big = json.loads(json.dumps(ENTRY))
    big["data"]["fire_rating"]["evidence"]["quote"] = "x" * (EXTRACTED_MAX_BYTES + 10)
    _r, body = send(monkeypatch, XEVENT, {"extraction_include_evidence": True}, Csai(entries=[big]))
    assert "quote" not in body["extracted"]["values"]["fire_rating"]["evidence"]
    huge = json.loads(json.dumps(ENTRY))
    huge["data"]["fire_rating"]["value"] = "y" * (EXTRACTED_MAX_BYTES + 10)
    _r, body = send(monkeypatch, XEVENT, {}, Csai(entries=[huge]))
    assert body["extracted"]["too_large"] is True and body["extracted"]["entry"] == "k1"
