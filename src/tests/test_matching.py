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

"""Pure matching/scoring unit tests (no core/db/redis needed)."""
from folder_actions import matching
from folder_actions.mime import mime_matches


class FakeCore:
    """Minimal core stub: a uid -> parent_uid map for ancestry/anchor resolution."""
    def __init__(self, parents=None):
        self.parents = parents or {}

    def parent_of(self, uid):
        return self.parents.get(uid, "")


def _binding(**kw):
    b = {"id": "b1", "folder_uid": "F", "recursive": False, "enabled": True,
         "on_events": ["file.created"], "action_type": "notify"}
    b.update(kw)
    return b


def test_file_created_in_folder_matches():
    ev = {"type": "file.created", "tenant": "default", "file_uid": "x", "parent_uid": "F"}
    assert matching.binding_matches(_binding(), ev, FakeCore())


def test_move_into_folder_matches_inbox():
    # file.moved carries the NEW parent as parent_uid -> arriving in F.
    ev = {"type": "file.moved", "file_uid": "x", "parent_uid": "F"}
    assert matching.binding_matches(_binding(on_events=["file.moved"]), ev, FakeCore())


def test_move_out_of_folder_does_not_match():
    ev = {"type": "file.moved", "file_uid": "x", "parent_uid": "OTHER"}
    assert not matching.binding_matches(_binding(on_events=["file.moved"]), ev, FakeCore())


def test_wrong_event_type_skipped():
    ev = {"type": "file.updated", "file_uid": "x", "parent_uid": "F"}
    assert not matching.binding_matches(_binding(), ev, FakeCore())


def test_disabled_binding_skipped():
    ev = {"type": "file.created", "file_uid": "x", "parent_uid": "F"}
    assert not matching.binding_matches(_binding(enabled=False), ev, FakeCore())


def test_anchored_review_event_resolves_to_parent():
    # review.* is anchored to file_uid; membership comes from the file's parent.
    core = FakeCore({"doc1": "F"})
    ev = {"type": "review.approved", "file_uid": "doc1", "review_id": "r1"}
    b = _binding(on_events=["review.approved"])
    assert matching.binding_matches(b, ev, core)


def test_recursive_binding_matches_descendant():
    # F is an ancestor of the event's folder G (G's parent is F).
    core = FakeCore({"G": "F"})
    ev = {"type": "file.created", "file_uid": "x", "parent_uid": "G"}
    assert matching.binding_matches(_binding(recursive=True), ev, core)
    # non-recursive does not reach into the subtree
    assert not matching.binding_matches(_binding(recursive=False), ev, core)


def test_recursive_cycle_is_safe():
    core = FakeCore({"A": "B", "B": "A"})  # cycle
    ev = {"type": "file.created", "file_uid": "x", "parent_uid": "A"}
    assert matching.binding_matches(_binding(folder_uid="Z", recursive=True), ev, core) is False


def test_mime_whitelist_matching():
    assert mime_matches("application/pdf", ["application/pdf"])
    assert mime_matches("image/png", ["image/*"])
    assert mime_matches("IMAGE/PNG", ["image/*"])          # case-insensitive
    assert mime_matches("text/plain; charset=utf-8", ["text/plain"])
    assert not mime_matches("application/zip", ["application/pdf", "image/*"])
    assert mime_matches("anything", [])                    # empty whitelist = all
    assert not mime_matches("", ["application/pdf"])
