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

"""Periodic reconcile sweep (SPECIFICATIONS.md §8).

The event stream is at-least-once and *fail-open*: the core publisher drops-oldest
on Redis backpressure and retention is capped, so events can be missed during a
Redis outage or beyond stream retention. This process runs a periodic sweep that
re-evaluates recent state against core to recover that work (e.g. re-run sorter
routing for recently-converted files in bound folders), independently of live
consumption. It mirrors the reconcile sweeps in ``convert_search_ai`` / ``discussion``."""
from __future__ import annotations

import logging
import time

from .config import Config, load_dotenv
from .core_client import CoreClient
from .stores import Store

log = logging.getLogger("folder_actions.reconcile")


def reconcile_once(config: Config, store: Store, core: CoreClient) -> None:
    """Run a single reconcile sweep.

    TODO (SPECIFICATIONS.md §8): re-evaluate recent state against core to recover
    events missed during a Redis outage or beyond stream retention — walk enabled
    bindings, list recently-changed / recently-converted files in each bound folder
    (via core / CSAI), and dispatch any binding whose ``action_run`` row is missing
    (the ``(event_id, binding_id)`` unique key keeps this idempotent with the live
    consumer). This is a minimal, correct stub: it performs no mutations yet."""
    log.info("reconcile sweep (stub) — no-op; see SPECIFICATIONS.md §8 for the "
             "planned re-evaluation of recently-changed files in bound folders")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    config = Config()
    store = Store(config)
    core = CoreClient(config)
    log.info("folder_actions reconcile started (interval=%ss)",
             config.reconcile_interval_s)
    try:
        while True:
            try:
                reconcile_once(config, store, core)
            except Exception:
                log.exception("reconcile sweep failed")
            time.sleep(config.reconcile_interval_s)
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        log.info("folder_actions reconcile stopping")


if __name__ == "__main__":  # pragma: no cover
    main()
