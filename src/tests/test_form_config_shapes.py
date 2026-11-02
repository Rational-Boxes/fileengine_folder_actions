"""The binding editor saves the shape of a plug-in's ``config_fields``; the
plug-in's ``ConfigModel`` must accept it.

FieldRenderer turns a ``group`` field into a list of row objects and keeps every
field at the top level. notify (``recipients``) and webhook (``context`` and the
flat ``auth_type`` / ``token_url`` / ``client_id`` keys) declared models that
only took the API shape, so every notify or webhook binding made in the UI was
refused at save with 422, and a webhook's OAuth settings were never read. Both
shapes are accepted now; these tests pin it.
"""
from folder_actions.api import _split_secrets
from folder_actions.plugins.base import registry
from folder_actions.plugins.notify import NotifyAction
from folder_actions.plugins.webhook import WebhookAction


def _form_defaults(plugin) -> dict:
    """What the editor would save for a plug-in with only its required fields
    filled in (groups as empty row lists, as FieldRenderer starts them)."""
    out = {}
    for f in plugin.config_fields():
        if f.type == "group":
            out[f.key] = []
        elif f.default is not None:
            out[f.key] = f.default
    return out


def test_notify_accepts_the_editors_principal_rows():
    m = NotifyAction.ConfigModel(recipients=[{"principal": "bob"}, {"principal": " role:qa "},
                                             {"principal": ""}], template=None)
    assert m.recipients == ["bob", "role:qa"]


def test_notify_still_accepts_plain_strings():
    assert NotifyAction.ConfigModel(recipients=["bob", "role:qa"]).recipients == ["bob", "role:qa"]


def test_webhook_accepts_the_editors_flat_shape():
    form = {"url": "https://x.example/h", "auth_type": "oauth2_client_credentials",
            "token_url": "https://idp.example/token", "client_id": "fe",
            "client_secret": "s3cret", "context": [{"key": "site", "value": "north"},
                                                   {"key": "", "value": "dropped"}],
            "timeout_s": 10, "max_retries": 5}
    clean, secrets = _split_secrets(form)
    assert secrets == {"client_secret": "s3cret"}
    m = WebhookAction.ConfigModel(**clean)
    assert m.auth == {"type": "oauth2_client_credentials",
                      "token_url": "https://idp.example/token", "client_id": "fe"}
    assert m.context == {"site": "north"}


def test_webhook_still_accepts_the_api_shape():
    m = WebhookAction.ConfigModel(url="https://x.example/h",
                                  auth={"type": "bearer"}, context={"a": "b"})
    assert m.auth == {"type": "bearer"} and m.context == {"a": "b"}


def test_explicit_auth_wins_over_flat_keys():
    m = WebhookAction.ConfigModel(url="https://x.example/h", auth_type="bearer",
                                  auth={"type": "oauth2_client_credentials"})
    assert m.auth["type"] == "oauth2_client_credentials"


def test_every_plugin_accepts_what_its_own_form_saves():
    """The general rule: an untouched form (required fields aside) must
    validate, for every registered plug-in."""
    required_samples = {"url": "https://x.example/h", "classifier_set_id": "cs1",
                        "prompt_set": "ps_1"}
    for name, plugin in registry().items():
        cfg = _form_defaults(plugin)
        for f in plugin.config_fields():
            if f.required and f.key not in cfg and f.key in required_samples:
                cfg[f.key] = required_samples[f.key]
        clean, _ = _split_secrets(cfg)
        plugin.ConfigModel(**clean)        # raises if the shapes disagree
