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

"""Locate and import the reused FileEngine Python client (``fileengine``).

Prefers an installed package; otherwise falls back to the sibling
``python_interface`` checkout (override with FILEENGINE_PYTHON_CLIENT). Same
bootstrap strategy as CSAI / the MCP server. Imported lazily by core_client so the
rest of the package (config, auth, health) imports without the gRPC stack present.
"""
import os
import sys


def _ensure_on_path() -> None:
    try:
        import fileengine  # noqa: F401
        return
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("FILEENGINE_PYTHON_CLIENT", ""),
        # discussion_threaded_communication/src/folder_actions/ -> ../../../python_interface
        os.path.join(here, "..", "..", "..", "python_interface"),
        os.path.join(here, "..", "..", "..", "..", "python_interface"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "fileengine", "__init__.py")):
            sys.path.insert(0, os.path.abspath(c))
            return
    raise ImportError(
        "Could not import 'fileengine'. Install ../python_interface "
        "(`pip install ../python_interface`) or set FILEENGINE_PYTHON_CLIENT."
    )


_ensure_on_path()

from fileengine import (  # noqa: E402
    ManagedFiles, FileEngineError, NotFoundError, WriteUnavailableError,
)

__all__ = ["ManagedFiles", "FileEngineError", "NotFoundError", "WriteUnavailableError"]
