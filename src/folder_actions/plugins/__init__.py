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

"""Built-in folder-action plug-ins (SPECIFICATIONS.md §7).

Importing this package imports each built-in module for its ``@register`` side
effect, so ``move_review`` / ``notify`` / ``sorter`` / ``webhook`` / ``raise_review`` are
present in the plug-in registry (base._REGISTRY) at startup. Third-party plug-ins
register instead through the ``folder_actions.actions`` entry-point group
(base.load_entrypoint_plugins)."""
from __future__ import annotations

from . import move_review, notify, raise_review, sorter, webhook  # noqa: F401
