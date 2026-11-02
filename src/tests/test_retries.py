"""Retryable failures are retried by the consumer, not by sleeping in a plug-in.

Before: a retryable result left the stream entry un-acked and nothing read it
again until the consumer restarted, while the webhook slept through its own
exponential backoff inside the single consumer loop, stalling every other
folder's events. Now the consumer re-claims an entry after ``retry_delay_s``
(XAUTOCLAIM), counts deliveries, and on the last one records the run as failed
(``retries_exhausted``) and acks it; the webhook makes one attempt per delivery.
"""
import urllib.error

import pytest
from pydantic import BaseModel

from folder_actions.consumer import EventConsumer
from folder_actions.plugins.base import ActionContext, ActionResult, _REGISTRY
from folder_actions.plugins.webhook import WebhookAction


class Flaky:
    """Fails retryably until ``succeed_on`` (a delivery number)."""
    type_name = "flaky"
    label = "Flaky"
    supported_events = frozenset({"file.created"})
    succeed_on = 99
    attempts: list = []

    class ConfigModel(BaseModel):
        pass

    @classmethod
    def config_fields(cls):
        return []

    def execute(self, event, config, ctx):
        Flaky.attempts.append(ctx.attempt)
        if ctx.attempt >= Flaky.succeed_on:
            return ActionResult()
        return ActionResult.failed("down", retryable=True)


@pytest.fixture(autouse=True)
def _register():
    Flaky.attempts, Flaky.succeed_on = [], 99
    _REGISTRY["flaky"] = Flaky
    yield
    _REGISTRY.pop("flaky", None)


class FakeConfig:
    enabled_actions: set = set()
    agent_user = "svc@example.com"
    tenant = "default"
    retry_delay_s = 60
    action_max_retries = 2


class FakeStore:
    def __init__(self):
        self.recorded = []

    def run_exists(self, tenant, event_id, binding_id):
        return any(r["event_id"] == event_id and r["binding_id"] == binding_id
                   for r in self.recorded)

    def record_run(self, tenant, **kw):
        self.recorded.append(kw)

    def list_enabled_bindings(self, tenant):
        return [{"id": "b1", "folder_uid": "F", "recursive": False, "enabled": True,
                 "on_events": ["file.created"], "action_type": "flaky", "config": {},
                 "mime_types": []}]


class FakeCore:
    def parent_of(self, uid):
        return "F"


class FakeSource:
    """A stream group's pending list: claim_stale hands back every pending entry
    (the test controls time) with its delivery count bumped, like XAUTOCLAIM."""
    def __init__(self):
        self.pending = {}            # msg_id -> [event, deliveries]
        self.acked = []

    def deliver(self, msg_id, event):
        self.pending[msg_id] = [event, 1]

    def claim_stale(self, min_idle_ms, count=32):
        assert min_idle_ms == 60_000
        out = []
        for mid, entry in list(self.pending.items()):
            entry[1] += 1
            out.append((mid, entry[0], entry[1]))
        return out

    def ack(self, ids):
        for i in ids:
            self.pending.pop(i, None)
            self.acked.append(i)


EVENT = {"type": "file.created", "event_id": "e1", "tenant": "default",
         "file_uid": "x", "parent_uid": "F", "version": "v1"}


def _consumer(store):
    return EventConsumer(FakeConfig(), store, core=FakeCore(), csai=object(),
                         directory=object(), mailer=object(), secrets=object(),
                         mime=object(), discussion=object())


def _first_delivery(consumer, source):
    source.deliver("1-0", EVENT)
    if not consumer.handle(EVENT):
        source.ack(["1-0"])


def test_a_retryable_failure_is_retried_and_then_succeeds():
    store, source = FakeStore(), FakeSource()
    Flaky.succeed_on = 2
    c = _consumer(store)
    _first_delivery(c, source)
    assert "1-0" in source.pending and store.recorded == []   # left for a retry
    assert c.retry_pending(source) == 1
    assert Flaky.attempts == [1, 2]
    assert source.acked == ["1-0"]
    assert [r["status"] for r in store.recorded] == ["done"]


def test_after_the_last_retry_the_run_is_recorded_failed_and_acked():
    store, source = FakeStore(), FakeSource()
    c = _consumer(store)
    _first_delivery(c, source)
    c.retry_pending(source)                       # delivery 2: still failing, kept
    assert "1-0" in source.pending and store.recorded == []
    c.retry_pending(source)                       # delivery 3 = 1 + max_retries(2): give up
    assert Flaky.attempts == [1, 2, 3]
    assert "1-0" not in source.pending
    (run,) = store.recorded
    assert run["status"] == "failed"
    assert run["detail"]["reason"] == "retries_exhausted" and run["detail"]["attempts"] == 3


def test_an_entry_trimmed_from_the_stream_is_acked_without_running():
    store, source = FakeStore(), FakeSource()
    source.deliver("1-0", {})
    _consumer(store).retry_pending(source)
    assert source.acked == ["1-0"] and Flaky.attempts == []


# ------------------------------------------------------------------ webhook

def test_the_webhook_makes_one_attempt_and_never_sleeps(monkeypatch):
    calls = []

    def post(self, url, body, headers, timeout):
        calls.append(url)
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(WebhookAction, "_post", post)
    monkeypatch.setattr("time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    status, _, transient = WebhookAction()._post_once("https://x.example/h", b"{}", {}, 5)
    assert status is None and transient and calls == ["https://x.example/h"]


class _Core:
    def metadata(self, uid):
        return {}


class _Mime:
    def resolve(self, uid):
        return "application/pdf"


class _Store:
    def get_secret(self, tenant, binding_id):
        return None


def _webhook_ctx(attempt):
    return ActionContext(tenant="default", binding_id="b1", folder_uid="F", core=_Core(),
                         csai=None, directory=None, discussion=None, mailer=None,
                         secrets=None, mime=_Mime(), store=_Store(), config=None,
                         attempt=attempt)


@pytest.mark.parametrize("attempt,retryable", [(1, True), (2, True), (3, False)])
def test_webhook_max_retries_is_measured_in_deliveries(monkeypatch, attempt, retryable):
    """max_retries=2: deliveries 1 and 2 are retryable, the 3rd gives up."""
    monkeypatch.setattr(WebhookAction, "_post",
                        lambda self, url, body, headers, timeout: (503, b""))
    cfg = WebhookAction.ConfigModel(url="https://x.example/h", max_retries=2)
    r = WebhookAction().execute(EVENT, cfg, _webhook_ctx(attempt))
    assert r.status == "failed" and r.retryable is retryable


def test_a_webhook_4xx_is_not_retried(monkeypatch):
    monkeypatch.setattr(WebhookAction, "_post",
                        lambda self, url, body, headers, timeout: (400, b""))
    cfg = WebhookAction.ConfigModel(url="https://x.example/h")
    r = WebhookAction().execute(EVENT, cfg, _webhook_ctx(1))
    assert r.status == "failed" and not r.retryable and r.detail["reason"] == "http_error"
