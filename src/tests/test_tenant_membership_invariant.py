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

"""The tenant-membership invariant, at every door this service opens.

Membership in a tenant IS holding >=1 LDAP group beneath that tenant's ou.
So: no group in T => no access to T, and roles never leak across tenants.

Each test here fails against the pre-fix code; they are the regression guard
for a cross-tenant privilege escalation (a member of tenant A's
`administrators` acting as an administrator of tenant B) and a cross-tenant
read (an authenticated non-member served under the core's read-by-default).
"""
import types

import pytest

from folder_actions import http_auth, ldap_auth, tenant_access


class _Entry:
    def __init__(self, cn):
        self.cn = cn
        self.entry_dn = f"cn={cn}"


class _FakeConn:
    """Minimal ldap3 Connection: answers searches from a {base: [cn]} map."""

    def __init__(self, groups_by_base, user_dn="uid=alice,ou=users,dc=x"):
        self.groups_by_base = groups_by_base
        self.user_dn = user_dn
        self.entries = []

    def search(self, base, filt, **kw):
        if base.startswith("ou=users"):
            self.entries = [_Entry("alice")]
            self.entries[0].entry_dn = self.user_dn
            return True
        self.entries = [_Entry(cn) for cn in self.groups_by_base.get(base, [])]
        return True

    def unbind(self):
        pass


def _is_admin(ident) -> bool:
    """Admin-ness, however this service spells it.

    Some copies expose an `is_admin` property; the rest carry the roles only.
    Either way an admin is someone holding an admin role IN the tenant."""
    if hasattr(ident, "is_admin"):
        return bool(ident.is_admin)
    return bool({"administrators", "system_admin"} & set(ident.roles or []))



def _auth_against(mod, cfg, user, pw, tenant):
    """_authenticate_against takes a tenant in most copies; mcp pins its own."""
    import inspect
    sig = inspect.signature(mod._authenticate_against)
    if "tenant" in sig.parameters:
        return mod._authenticate_against("ldap://x", cfg, user, pw, tenant)
    return mod._authenticate_against("ldap://x", cfg, user, pw)


def _cfg(**kw):
    c = types.SimpleNamespace(
        ldap_tenant_base="ou=tenants,dc=x",
        ldap_user_base="ou=users,dc=x",
        tenant="alpha",
        agent_user="",
        service_principals="",
        ldap_service_base="",
        ldap_bind_dn="cn=svc",
        ldap_bind_password="pw",
    )
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# --- roles are scoped to the tenant asked about -----------------------------

def test_roles_come_only_from_the_requested_tenants_ou():
    cfg = _cfg()
    conn = _FakeConn({
        "ou=alpha,ou=tenants,dc=x": ["administrators"],
        "ou=beta,ou=tenants,dc=x": [],
    })
    assert "administrators" in tenant_access.roles_in_tenant(conn, cfg, "uid=alice", "alpha")
    # The same user, asked about a tenant they hold no group in.
    assert tenant_access.roles_in_tenant(conn, cfg, "uid=alice", "beta") == []


def test_administrators_in_one_tenant_is_not_an_admin_in_another():
    """The escalation this fix exists to stop."""
    cfg = _cfg()
    conn = _FakeConn({"ou=alpha,ou=tenants,dc=x": ["administrators"]})
    alpha = tenant_access.roles_in_tenant(conn, cfg, "uid=alice", "alpha")
    beta = tenant_access.roles_in_tenant(conn, cfg, "uid=alice", "beta")
    assert "system_admin" in alpha          # mapped from `administrators`
    assert beta == []                       # and NOT carried into beta
    assert not _is_admin(ldap_auth.Identity(user="a", roles=beta))


def test_role_lookup_failure_fails_closed():
    cfg = _cfg()

    class _Boom(_FakeConn):
        def search(self, base, filt, **kw):
            raise tenant_access.LDAPException("unreachable")

    assert tenant_access.roles_in_tenant(_Boom({}), cfg, "uid=alice", "alpha") == []


def test_empty_tenant_is_never_a_membership():
    assert tenant_access.roles_in_tenant(_FakeConn({}), _cfg(), "uid=alice", "") == []


# --- the token path ---------------------------------------------------------

def _claims(roles, sub="alice"):
    return {"sub": sub, "tenant": "alpha", "roles": roles}


def test_token_for_one_tenant_is_refused_for_another():
    claims = _claims({"alpha": ["users"]})
    assert tenant_access.scope_claims_to_tenant(claims, "alpha") == ("alice", ["users"])
    # Pre-fix this returned ("alice", []) — an AUTHENTICATED non-member.
    assert tenant_access.scope_claims_to_tenant(claims, "beta") is None


def test_token_roles_do_not_cross_tenants():
    claims = _claims({"alpha": ["administrators"], "beta": ["users"]})
    assert tenant_access.scope_claims_to_tenant(claims, "beta") == ("alice", ["users"])


def test_token_without_a_subject_is_refused():
    assert tenant_access.scope_claims_to_tenant({"roles": {"alpha": []}}, "alpha") is None


def test_membership_with_an_empty_role_list_is_still_membership():
    """The bridge only mints a tenant key for a tenant the user belongs to."""
    assert tenant_access.scope_claims_to_tenant(_claims({"alpha": []}), "alpha") == ("alice", [])


def test_non_bridge_tokens_are_left_alone():
    """No roles map => not a bridge session; membership is not asserted here."""
    assert tenant_access.scope_claims_to_tenant({"sub": "svc"}, "alpha") == ("svc", [])


# --- the Basic/LDAP path ----------------------------------------------------

def test_bind_without_a_group_in_the_tenant_is_not_authenticated(monkeypatch):
    cfg = _cfg()
    conn = _FakeConn({"ou=alpha,ou=tenants,dc=x": ["users"]})
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)

    ok = ldap_auth._authenticate_against("ldap://x", cfg, "alice", "pw", "alpha")
    assert ok.authenticated and ok.tenant == "alpha" and ok.roles == ["users"]

    # Same bind, a tenant they hold no group in: bound, but NOT a member.
    denied = ldap_auth._authenticate_against("ldap://x", cfg, "alice", "pw", "beta")
    assert not denied.authenticated


def test_service_principal_reaches_a_tenant_it_holds_no_group_in(monkeypatch):
    cfg = _cfg(agent_user="svc@platform.test")
    conn = _FakeConn({})            # in no tenant group anywhere
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)

    ident = ldap_auth._authenticate_against("ldap://x", cfg, "SVC@Platform.test", "pw", "beta")
    assert ident.authenticated          # the admission test does not apply to it
    assert ident.roles == []            # and it holds nothing there, so nothing
    assert not _is_admin(ident)


def test_service_principal_keeps_the_roles_it_holds_in_the_tenant(monkeypatch):
    """The exemption skips the admission test — it does not blank roles.

    Blanking them broke production: the workers reach the core as this
    principal, and stripping every role turned operations they legitimately
    perform into PermissionDenied. The escalation to remove was the
    CROSS-TENANT one.
    """
    cfg = _cfg(agent_user="svc@platform.test")
    conn = _FakeConn({"ou=alpha,ou=tenants,dc=x": ["administrators"],
                      "ou=beta,ou=tenants,dc=x": []})
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)

    here = ldap_auth._authenticate_against("ldap://x", cfg, "svc@platform.test", "pw", "alpha")
    assert here.authenticated and "administrators" in here.roles

    # ...but still never carried into a tenant it holds nothing in.
    there = ldap_auth._authenticate_against("ldap://x", cfg, "svc@platform.test", "pw", "beta")
    assert there.authenticated and there.roles == []
    assert not _is_admin(there)


def test_service_principal_match_is_case_insensitive():
    cfg = _cfg(agent_user="Svc@Platform.test")
    assert tenant_access.is_service_principal(cfg, "svc@platform.TEST")
    assert not tenant_access.is_service_principal(cfg, "alice")
    assert not tenant_access.is_service_principal(cfg, "")


def test_extra_service_principals_are_configurable():
    cfg = _cfg(service_principals=" a@x.test , b@x.test ")
    assert tenant_access.is_service_principal(cfg, "b@x.test")
    assert not tenant_access.is_service_principal(cfg, "c@x.test")


# --- the own-token path -----------------------------------------------------

class _Store:
    def __init__(self, ident):
        self._ident = ident

    def resolve(self, token):
        return self._ident if token == "t" else None


def test_own_token_is_bound_to_the_tenant_it_was_issued_for():
    ident = ldap_auth.Identity(user="alice", roles=["administrators"],
                               tenant="alpha", authenticated=True)
    store = _Store(ident)
    assert http_auth.resolve_identity("Bearer t", "alpha", _cfg(), store) is ident
    # Pre-fix this re-stamped the header's tenant and handed back the
    # alpha-resolved roles for use against beta.
    assert http_auth.resolve_identity("Bearer t", "beta", _cfg(), store) is None


# --- the bridge-token path, through the service's own entry points ----------

def test_identity_from_claims_refuses_a_foreign_tenant():
    """`jwt_verify.identity_from_claims` is what the service actually calls."""
    from folder_actions import jwt_verify
    claims = _claims({"alpha": ["users"]})
    assert jwt_verify.identity_from_claims(claims, "alpha") == ("alice", ["users"])
    assert jwt_verify.identity_from_claims(claims, "beta") is None


def test_bridge_verifier_refuses_a_foreign_tenant(monkeypatch):
    """End of the local-HS256 shortcut: the branch production actually runs."""
    import hashlib, hmac, base64, json, time
    from folder_actions import bridge_auth, token_revocation

    secret = "s3cret"

    def _b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    def _mint(claims):
        h = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        p = _b64(json.dumps(claims).encode())
        sig = _b64(hmac.new(secret.encode(), h + b"." + p, hashlib.sha256).digest())
        return (h + b"." + p + b"." + sig).decode()

    monkeypatch.setattr(token_revocation, "permits", lambda jti: True)
    monkeypatch.setattr(bridge_auth.token_revocation, "permits", lambda jti: True)

    tok = _mint({"sub": "alice", "tenant": "alpha", "jti": "j1",
                 "roles": {"alpha": ["users"]}, "exp": int(time.time()) + 3600})
    v = bridge_auth.BridgeTokenVerifier(base_url="", jwt_secret=secret)

    ok = v.verify(tok, "alpha")
    assert ok is not None and ok.user == "alice" and ok.tenant == "alpha"
    # Pre-fix: Identity(user='alice', roles=[], tenant='beta', authenticated=True)
    assert v.verify(tok, "beta") is None


# --- a worker's own identity lives outside the user base --------------------

def test_the_agent_is_found_in_the_service_base(monkeypatch):
    """Service accounts live under ou=services so no user-facing query sees them.

    Rosters, user search, mention resolution and the sign-in doors are all
    rooted at ldap_user_base, so a machine account under a different ou is
    invisible to them BY CONSTRUCTION. The cost is that this path has to be
    told where to look.
    """
    cfg = _cfg(agent_user="svc-worker", ldap_service_base="ou=services,dc=x")

    class _TwoBase(_FakeConn):
        def __init__(self):
            super().__init__({"ou=alpha,ou=tenants,dc=x": []})
            self.searched = []

        def search(self, base, filt, **kw):
            self.searched.append(base)
            if base.startswith("ou=services"):
                e = _Entry("svc-worker")
                e.entry_dn = "uid=svc-worker,ou=services,dc=x"
                self.entries = [e]
                return True
            if base.startswith("ou=users"):
                self.entries = []          # not a person; not there
                return True
            self.entries = []
            return True

    conn = _TwoBase()
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)

    ident = _auth_against(ldap_auth, cfg, "svc-worker", "pw", "alpha")
    assert ident.authenticated            # found, and exempt as a service principal
    assert "ou=users,dc=x" in conn.searched      # user base is tried FIRST
    assert "ou=services,dc=x" in conn.searched   # and the service base only after


def test_without_a_service_base_nothing_extra_is_searched(monkeypatch):
    cfg = _cfg(agent_user="svc-worker")   # ldap_service_base unset
    conn = _FakeConn({})
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)
    ident = _auth_against(ldap_auth, cfg, "nobody", "pw", "alpha")
    assert not ident.authenticated
