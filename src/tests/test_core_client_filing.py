"""CoreClient filing operations used by the extraction rules (move with a new
name, rename, copy that reports the copy's uid, folder by name)."""
from types import SimpleNamespace as NS

from folder_actions.core_client import CoreClient


class FakeFiles:
    """A tiny tree: uid -> {name, parent, dir}."""
    def __init__(self):
        self.nodes = {"F": {"name": "Inbox", "parent": "", "dir": True},
                      "D": {"name": "Filed", "parent": "", "dir": True},
                      "x": {"name": "sheet.pdf", "parent": "F", "dir": False}}
        self.n = 0

    def stat(self, uid, tenant=None):
        return NS(uid=uid, name=self.nodes[uid]["name"])

    def dir(self, uid, tenant=None):
        return [NS(uid=u, name=n["name"], type=1 if n["dir"] else 0)
                for u, n in self.nodes.items() if n["parent"] == uid]

    def move(self, uid, dest, new_name=None, tenant=None):
        self.nodes[uid]["parent"] = dest
        if new_name:
            self.nodes[uid]["name"] = new_name
        return True

    def rename(self, uid, new_name, tenant=None):
        self.nodes[uid]["name"] = new_name
        return True

    def copy(self, uid, dest, tenant=None):
        self.n += 1
        self.nodes[f"c{self.n}"] = {**self.nodes[uid], "parent": dest}
        return True

    def mkdir(self, parent, name, tenant=None):
        self.n += 1
        self.nodes[f"d{self.n}"] = {"name": name, "parent": parent, "dir": True}
        return f"d{self.n}"


def _client():
    c = CoreClient(NS(tenant="default", agent_user="svc", agent_password=""))
    c._mf = FakeFiles()
    return c


def test_move_with_a_new_name():
    c = _client()
    assert c.move("x", "D", new_name="EI60 door.pdf", normalize=False)
    assert c._mf.nodes["x"] == {"name": "EI60 door.pdf", "parent": "D", "dir": False}


def test_copy_returns_the_copys_uid_and_renames_it():
    c = _client()
    c._mf.nodes["old"] = {"name": "sheet.pdf", "parent": "D", "dir": False}   # same name already there
    uid = c.copy("x", "D", new_name="copy.pdf")
    assert uid == "c1" and c._mf.nodes["c1"]["name"] == "copy.pdf"
    assert c._mf.nodes["old"]["name"] == "sheet.pdf"                         # the old one untouched
    assert c._mf.nodes["x"]["parent"] == "F"                                 # source stays


def test_ensure_folder_reuses_then_creates():
    c = _client()
    assert c.ensure_folder("", "Filed") == "D"
    new = c.ensure_folder("D", "Acme")
    assert c._mf.nodes[new] == {"name": "Acme", "parent": "D", "dir": True}
    assert c.ensure_folder("D", "Acme") == new
    # a FILE with that name is not a folder to file into
    c._mf.nodes["f"] = {"name": "Beta", "parent": "D", "dir": False}
    assert c.ensure_folder("D", "Beta") != "f"
