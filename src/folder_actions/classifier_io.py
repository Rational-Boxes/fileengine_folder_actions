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

"""Classifier-set YAML import/export against the folder_actions ``Store`` (§7.3.1).

The SmolDocBot ``type: classifier`` YAML round-trips so authored and externally
generated sets interoperate. This re-implements the vendored ``import_export.py``
logic (originally SQLAlchemy) on top of the per-tenant ``Store`` (§10):

  {
    "name": "invoices",
    "type": "classifier",
    "classifiers": [
      {"name": "invoice", "terms": [{"term": "total due", "distance": 1, "weight": 2.0}]}
    ]
  }
"""
from __future__ import annotations

from typing import Any

import yaml
from fastapi import HTTPException


def export_classifier_to_yaml(store, tenant: str, set_id: str) -> str:
    """Serialise a tenant's classifier set to SmolDocBot YAML. 404 if it is unknown."""
    full = store.get_classifier_set_full(tenant, set_id)
    if full is None:
        raise HTTPException(status_code=404, detail="Classifier set not found")

    export_data: dict[str, Any] = {
        "name": full.get("name"),
        "type": "classifier",
        "classifiers": [],
    }
    for classifier in full.get("classifiers", []):
        export_data["classifiers"].append({
            "name": classifier.get("name"),
            "terms": [
                {
                    "term": term.get("term"),
                    "distance": term.get("distance"),
                    "weight": term.get("weight"),
                }
                for term in classifier.get("terms", [])
            ],
        })
    return yaml.dump(export_data, default_flow_style=False, sort_keys=False)


def import_classifier_from_yaml(store, tenant: str, yaml_content: str,
                                created_by: str = "") -> str:
    """Create a classifier set (+ classifiers + terms) from SmolDocBot YAML.

    Mirrors the vendored ``import_export.py`` validation, raising HTTPException on
    malformed input. Returns the new set id."""
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML format: {str(e)}")

    if not isinstance(data, dict) or data.get("type") != "classifier":
        raise HTTPException(status_code=400, detail="Invalid classifier YAML format")

    for field in ("name", "classifiers"):
        if field not in data:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    classifiers = data["classifiers"]
    if not isinstance(classifiers, list):
        raise HTTPException(status_code=400, detail="Invalid classifier format in YAML")
    for classifier_data in classifiers:
        if not isinstance(classifier_data, dict) or "name" not in classifier_data:
            raise HTTPException(status_code=400, detail="Invalid classifier format in YAML")

    set_id = store.create_classifier_set(tenant, str(data["name"]), created_by=created_by)
    for position, classifier_data in enumerate(classifiers):
        classifier_id = store.add_classifier(
            tenant, set_id, str(classifier_data["name"]), position=position)
        for term_data in classifier_data.get("terms", []) or []:
            if isinstance(term_data, dict):
                term = term_data.get("term", "")
                distance = term_data.get("distance", 0)
                weight = term_data.get("weight", 1.0)
            else:
                term = getattr(term_data, "term", "")
                distance = getattr(term_data, "distance", 0)
                weight = getattr(term_data, "weight", 1.0)
            store.add_term(tenant, classifier_id, str(term), int(distance or 0),
                           float(weight if weight is not None else 1.0))
    return set_id
