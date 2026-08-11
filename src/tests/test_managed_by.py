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

"""The `managed_by` externally-managed marker on classifier sets + notify templates
(provisioning §14a). Pure checks: DDL migration + API request models."""
from folder_actions import schema
from folder_actions.classifier_api import SetCreate
from folder_actions.notify_templates_api import TemplateCreate


def test_ddl_adds_managed_by_columns_and_migrations():
    ddl = schema.tenant_ddl("t")
    # fresh-install column on both config tables
    assert "managed_by" in ddl
    # idempotent migration for already-provisioned tenants
    assert 'ALTER TABLE "tenant_t".classifier_set ADD COLUMN IF NOT EXISTS managed_by text' in ddl
    assert 'ALTER TABLE "tenant_t".notify_template ADD COLUMN IF NOT EXISTS managed_by text' in ddl


def test_set_create_accepts_managed_by():
    assert SetCreate(name="mfg").managed_by is None
    assert SetCreate(name="mfg", managed_by="acme-crm").managed_by == "acme-crm"


def test_template_create_accepts_managed_by():
    t = TemplateCreate(name="approved", subject="s", managed_by="acme-crm")
    assert t.managed_by == "acme-crm"
    assert TemplateCreate(name="approved").managed_by is None
