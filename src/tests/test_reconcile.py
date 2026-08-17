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

"""Reconcile sweep unit tests (SPECIFICATIONS.md §8) — no core/db/redis needed."""
from datetime import datetime, timedelta, timezone

from folder_actions.reconcile import RECONCILABLE_EVENTS, Reconciler, _event_id

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


class FakeConfig:
    reconcile_lookback_s = 86400
    reconcile_overlap_s = 300
    reconcile_max_files = 5000
    reconcile_max_depth = 32
    reconcile_interval_s = 900
    reconcile_enabled = True


class Entry:
    """Stand-in for a client DirectoryEntry."""
    def __init__(self, uid, name="f", modified_at=None, is_container=False,
                 modified_by="alice"):
        self.uid = uid
        self.name = name
        self.modified_at = modified_at
        self.is_container = is_container
        self.modified_by = modified_by


class Stat:
    def __init__(self, version):
        self.version = version


class FakeCore:
    """uid -> child entries; stat() returns a per-uid version."""
    def __init__(self, tree=None, versions=None, unlistable=()):
        self.tree = tree or {}
        self.versions = versions or {}
        self.unlistable = set(unlistable)
        self.listed = []

    def listdir(self, uid):
        if uid in self.unlistable:
            raise RuntimeError("boom")
        self.listed.append(uid)
        return self.tree.get(uid, [])

    def stat(self, uid):
        return Stat(self.versions.get(uid, "v1"))


class FakeStore:
    def __init__(self, bindings=None, watermark=None, tenants=("default",)):
        self.bindings = bindings or []
        self.watermark = watermark
        self.tenants = list(tenants)
        self.marks = []

    def list_tenants(self):
        return self.tenants

    def list_enabled_bindings(self, tenant):
        return self.bindings

    def get_reconcile_watermark(self, tenant):
        return self.watermark

    def set_reconcile_watermark(self, tenant, when):
        self.marks.append((tenant, when))


class FakeConsumer:
    def __init__(self):
        self.events = []

    def handle(self, event, *, collapse_on_content=False):
        self.events.append((event, collapse_on_content))
        return False


def _binding(**kw):
    b = {"id": "b1", "folder_uid": "F", "recursive": False, "enabled": True,
         "on_events": ["file.created"], "action_type": "notify"}
    b.update(kw)
    return b


def _reconciler(store, core, consumer=None):
    return Reconciler(FakeConfig(), store, consumer=consumer or FakeConsumer(),
                      core_factory=lambda tenant: core)


# ------------------------------------------------------------------ window
def test_first_sweep_uses_full_lookback():
    r = _reconciler(FakeStore(), FakeCore())
    assert r.window_start("default", NOW) == NOW - timedelta(seconds=86400)


def test_watermark_is_rewound_by_the_overlap():
    mark = NOW - timedelta(seconds=1000)
    r = _reconciler(FakeStore(watermark=mark), FakeCore())
    assert r.window_start("default", NOW) == mark - timedelta(seconds=300)


def test_window_is_clamped_to_the_lookback_after_a_long_outage():
    # A watermark from a week ago must not widen the window past the lookback.
    r = _reconciler(FakeStore(watermark=NOW - timedelta(days=7)), FakeCore())
    assert r.window_start("default", NOW) == NOW - timedelta(seconds=86400)


def test_naive_watermark_is_treated_as_utc():
    naive = (NOW - timedelta(seconds=1000)).replace(tzinfo=None)
    r = _reconciler(FakeStore(watermark=naive), FakeCore())
    assert r.window_start("default", NOW) == NOW - timedelta(seconds=1300)


# ------------------------------------------------------------- enumeration
def test_only_files_changed_inside_the_window_are_dispatched():
    core = FakeCore(tree={"F": [
        Entry("fresh", modified_at=NOW - timedelta(minutes=5)),
        Entry("stale", modified_at=NOW - timedelta(days=3)),
    ]})
    consumer = FakeConsumer()
    r = _reconciler(FakeStore(bindings=[_binding()]), core, consumer)
    counts = r.sweep_tenant("default", now=NOW)

    assert counts["candidates"] == 1
    assert [e["file_uid"] for e, _ in consumer.events] == ["fresh"]


def test_entry_without_a_timestamp_is_skipped():
    core = FakeCore(tree={"F": [Entry("nots", modified_at=None)]})
    consumer = FakeConsumer()
    r = _reconciler(FakeStore(bindings=[_binding()]), core, consumer)
    assert r.sweep_tenant("default", now=NOW)["candidates"] == 0
    assert consumer.events == []


def test_non_recursive_binding_does_not_descend():
    core = FakeCore(tree={
        "F": [Entry("sub", is_container=True)],
        "sub": [Entry("deep", modified_at=NOW)],
    })
    consumer = FakeConsumer()
    r = _reconciler(FakeStore(bindings=[_binding()]), core, consumer)
    r.sweep_tenant("default", now=NOW)
    assert consumer.events == []
    assert "sub" not in core.listed


def test_recursive_binding_descends():
    core = FakeCore(tree={
        "F": [Entry("sub", is_container=True)],
        "sub": [Entry("deep", modified_at=NOW)],
    })
    consumer = FakeConsumer()
    r = _reconciler(FakeStore(bindings=[_binding(recursive=True)]), core, consumer)
    r.sweep_tenant("default", now=NOW)
    assert [e["file_uid"] for e, _ in consumer.events] == ["deep"]
    # The synthesized parent is where the file actually lives, not the bound root.
    assert consumer.events[0][0]["parent_uid"] == "sub"


def test_a_folder_that_cannot_be_listed_is_skipped_not_fatal():
    core = FakeCore(tree={"G": [Entry("ok", modified_at=NOW)]}, unlistable={"F"})
    consumer = FakeConsumer()
    store = FakeStore(bindings=[_binding(folder_uid="F"),
                                _binding(id="b2", folder_uid="G")])
    counts = _reconciler(store, core, consumer).sweep_tenant("default", now=NOW)
    assert counts["candidates"] == 1
    assert [e["file_uid"] for e, _ in consumer.events] == ["ok"]


def test_one_folder_is_listed_once_for_several_bindings():
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW)]})
    store = FakeStore(bindings=[_binding(id="b1"), _binding(id="b2")])
    _reconciler(store, core).sweep_tenant("default", now=NOW)
    assert core.listed.count("F") == 1


# ---------------------------------------------------------------- dispatch
def test_dispatch_always_collapses_on_content():
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW)]})
    consumer = FakeConsumer()
    _reconciler(FakeStore(bindings=[_binding()]), core, consumer).sweep_tenant(
        "default", now=NOW)
    assert all(collapse for _, collapse in consumer.events)


def test_dispatch_carries_the_modification_time_for_the_idempotency_guard():
    # Regression: the guard cannot key on version alone. A core file.created event
    # records version='' while the sweep stamps the version read back from stat, so
    # matching on version let every newly-created file run a second time. The event
    # carries modified_at so "already ran after the last change" can be asked.
    mod = NOW - timedelta(minutes=5)
    core = FakeCore(tree={"F": [Entry("a", modified_at=mod)]}, versions={"a": "v9"})
    consumer = FakeConsumer()
    _reconciler(FakeStore(bindings=[_binding()]), core, consumer).sweep_tenant(
        "default", now=NOW)
    assert consumer.events[0][0]["reconciled_since"] == mod


def test_synthesized_event_carries_the_current_version_and_actor():
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW, modified_by="bob")]},
                    versions={"a": "20260817_120000.000"})
    consumer = FakeConsumer()
    _reconciler(FakeStore(bindings=[_binding()]), core, consumer).sweep_tenant(
        "default", now=NOW)
    ev = consumer.events[0][0]
    assert ev["version"] == "20260817_120000.000"
    assert ev["actor"] == "bob"
    assert ev["reconciled"] is True


def test_event_ids_are_deterministic_across_sweeps():
    assert _event_id("file.created", "a", "v1") == _event_id("file.created", "a", "v1")
    assert _event_id("file.created", "a", "v1") != _event_id("file.created", "a", "v2")
    assert _event_id("file.created", "a", "v1") != _event_id("file.updated", "a", "v1")
    assert _event_id("file.created", "a", "v1").startswith("reconcile:file.created:")


def test_only_reconcilable_event_types_are_synthesized():
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW)]})
    consumer = FakeConsumer()
    binding = _binding(on_events=["file.created", "file.moved", "review.approved"])
    _reconciler(FakeStore(bindings=[binding]), core, consumer).sweep_tenant(
        "default", now=NOW)
    assert {e["type"] for e, _ in consumer.events} == {"file.created"}


def test_binding_with_no_reconcilable_events_is_counted_not_run():
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW)]})
    consumer = FakeConsumer()
    binding = _binding(on_events=["review.approved", "review.rejected"])
    counts = _reconciler(FakeStore(bindings=[binding]), core, consumer).sweep_tenant(
        "default", now=NOW)
    assert counts["bindings_unreconcilable"] == 1
    assert consumer.events == []


def test_every_reconcilable_type_a_binding_listens_for_is_synthesized():
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW)]})
    consumer = FakeConsumer()
    binding = _binding(on_events=list(RECONCILABLE_EVENTS))
    _reconciler(FakeStore(bindings=[binding]), core, consumer).sweep_tenant(
        "default", now=NOW)
    assert {e["type"] for e, _ in consumer.events} == set(RECONCILABLE_EVENTS)


# --------------------------------------------------------------- watermark
def test_watermark_advances_after_a_complete_sweep():
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW)]})
    store = FakeStore(bindings=[_binding()])
    _reconciler(store, core).sweep_tenant("default", now=NOW)
    assert store.marks == [("default", NOW)]


def test_watermark_advances_when_there_is_nothing_to_do():
    store = FakeStore(bindings=[])
    _reconciler(store, FakeCore()).sweep_tenant("default", now=NOW)
    assert store.marks == [("default", NOW)]


def test_watermark_is_held_when_the_file_budget_truncates_the_sweep():
    cfg = FakeConfig()
    cfg.reconcile_max_files = 1
    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW),
                                Entry("b", modified_at=NOW)]})
    store = FakeStore(bindings=[_binding()])
    r = Reconciler(cfg, store, consumer=FakeConsumer(), core_factory=lambda t: core)
    counts = r.sweep_tenant("default", now=NOW)
    assert counts["truncated"] >= 1
    # Holding the watermark is what makes the next sweep resume, not skip ahead.
    assert store.marks == []


# ------------------------------------------------------------------- sweep
def test_sweep_once_covers_every_tenant_and_survives_one_failing():
    class Boom(FakeStore):
        def list_enabled_bindings(self, tenant):
            if tenant == "bad":
                raise RuntimeError("nope")
            return [_binding()]

    core = FakeCore(tree={"F": [Entry("a", modified_at=NOW)]})
    store = Boom(tenants=["good", "bad", "other"])
    totals = _reconciler(store, core).sweep_once()
    assert totals["candidates"] == 2          # good + other; bad raised
    assert {t for t, _ in store.marks} == {"good", "other"}
