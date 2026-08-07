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

"""The vendored SmolDocBot scorer, as folder_actions consumes it (§7.3)."""
from folder_actions.classifier import document_classifier_simple


def test_weighted_sum_scoring_and_threshold():
    text = "This invoice number 12345 is due on receipt. Total amount payable."
    classifiers = [
        {"name": "Invoice", "terms": [
            {"term": "invoice", "distance": 1, "weight": 2.0},
            {"term": "amount payable", "distance": 2, "weight": 1.5},
        ]},
        {"name": "Contract", "terms": [
            {"term": "agreement", "distance": 1, "weight": 3.0},
        ]},
    ]
    scores = document_classifier_simple(text, classifiers)
    # Invoice terms both match -> unbounded weighted sum 2.0 + 1.5.
    assert scores["Invoice"] == 2.0 + 1.5
    # Contract term absent -> 0.
    assert scores["Contract"] == 0.0
    # A per-folder threshold of 3.0 would route this to Invoice, not Contract.
    winners = {k: v for k, v in scores.items() if v >= 3.0}
    assert winners == {"Invoice": 3.5}


def test_fuzzy_distance_matches_typo():
    # A Levenshtein distance of 1 tolerates a single-character typo.
    classifiers = [{"name": "PO", "terms": [{"term": "purchase", "distance": 1, "weight": 1.0}]}]
    assert document_classifier_simple("this purchse order", classifiers)["PO"] == 1.0   # 1 edit
    assert document_classifier_simple("this xyzzy order", classifiers)["PO"] == 0.0


def test_wildcards_are_stripped_by_normalization():
    # KNOWN LIMITATION of the vendored SmolDocBot scorer: normalize_text() removes
    # non-word chars, so the wildcard tokens ``*`` ``?`` ``#`` are dropped before
    # matching — "ref #" degrades to the plain word "ref" and matches any "ref".
    classifiers = [{"name": "Ref", "terms": [{"term": "ref #", "distance": 0, "weight": 1.0}]}]
    assert document_classifier_simple("ref 4471", classifiers)["Ref"] == 1.0
    assert document_classifier_simple("ref alpha", classifiers)["Ref"] == 1.0  # wildcard not enforced
