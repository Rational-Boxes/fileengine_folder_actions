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
"""The sign-in origin's label is never a tenant.

login.<base> also serves the workspace when a tenant's own subdomain is
unreachable, so its service paths are proxied there. A request on that host
without X-Tenant must not resolve to tenant "login": the core auto-registers
any tenant it is asked about, and download tickets, service tokens and Basic
service principals are not stopped by the session-token membership check.
"""
import pytest

from folder_actions.http_auth import extract_tenant


@pytest.fixture(autouse=True)
def _no_login_override(monkeypatch):
    monkeypatch.delenv("LOGIN_SUBDOMAIN", raising=False)


def test_the_login_host_is_not_a_tenant():
    assert extract_tenant({}, "login.example.com", "default") == "default"
    assert extract_tenant({}, "LOGIN.example.com:443", "default") == "default"


def test_x_tenant_still_wins_on_the_login_host():
    # The SPA's fallback sends X-Tenant on every request; that is the tenant.
    assert extract_tenant({"x-tenant": "acme"}, "login.example.com", "default") == "acme"


def test_a_reserved_name_in_x_tenant_is_ignored_not_obeyed():
    assert extract_tenant({"x-tenant": "login"}, "acme.example.com", "default") == "acme"
    assert extract_tenant({"x-tenant": "Login"}, "login.example.com", "default") == "default"
    assert extract_tenant({"x-tenant": "www"}, "example.com", "default") == "default"


def test_a_renamed_sign_in_origin_is_reserved_instead(monkeypatch):
    monkeypatch.setenv("LOGIN_SUBDOMAIN", "signin")
    assert extract_tenant({}, "signin.example.com", "default") == "default"
    # With the sign-in origin renamed, "login" is an ordinary label again.
    assert extract_tenant({}, "login.example.com", "default") == "login"


def test_tenant_hosts_are_unchanged():
    assert extract_tenant({}, "acme.example.com", "default") == "acme"
    assert extract_tenant({}, "acme-drive.example.com", "default") == "acme"
    assert extract_tenant({}, "www.example.com", "default") == "default"
