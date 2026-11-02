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

"""Consume the core publisher's file-activity events from a Redis stream.

Per EVENT_CONTRACT.md: a single shared stream, one JSON ``payload`` field per
entry, consumed with a named consumer group (at-least-once). The transport is
deliberately behind this small ``EventSource`` surface so it can be swapped
(Kafka/NATS/…) without touching ingestion. ``redis`` is imported lazily."""
from __future__ import annotations

import json
from typing import List, Tuple

from .config import Config

# A consumed entry: (stream message id, decoded event dict).
Entry = Tuple[str, dict]


class RedisEventSource:
    def __init__(self, config: Config, consumer_name: str = "worker-1", group: str = None):
        self.config = config
        self.stream = config.events_stream
        # A distinct group per independent consumer (the ingest worker and the
        # permission-cache invalidator each get every event — EVENT_CONTRACT §1).
        self.group = group or config.events_group
        self.consumer = consumer_name
        self._r = None

    def _redis(self):
        if self._r is None:
            import redis  # lazy
            self._r = redis.Redis(
                host=self.config.redis_host, port=self.config.redis_port,
                password=self.config.redis_password or None, db=self.config.redis_db,
            )
        return self._r

    def ensure_group(self) -> None:
        """Create the consumer group at the stream's tail (idempotent)."""
        import redis
        try:
            self._redis().xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def read(self, count: int = 32, block_ms: int = 5000) -> List[Entry]:
        """Read up to ``count`` new entries for this group; block up to ``block_ms``."""
        import redis  # lazy
        try:
            resp = self._redis().xreadgroup(
                self.group, self.consumer, {self.stream: ">"}, count=count, block=block_ms
            )
        except redis.exceptions.TimeoutError:
            # A blocking XREADGROUP that returns no new entries within block_ms can
            # surface as a socket-read timeout (redis-py/RESP3 sets the read timeout
            # to ~block_ms with no buffer). That just means "no events", so yield an
            # empty batch and let the poll loop continue instead of crashing.
            return []
        out: List[Entry] = []
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                out.append((_decode(msg_id), _parse_payload(fields)))
        return out

    def read_pending(self, count: int = 32) -> List[Entry]:
        """Re-read this consumer's already-delivered, un-acked entries (its PEL).

        Used by the ingest worker's read-only sleep/poll mode to retry events it
        deliberately left un-acked while the core was read-only — reading id "0"
        returns the consumer's pending entries rather than new (">") ones."""
        import redis  # lazy
        try:
            resp = self._redis().xreadgroup(
                self.group, self.consumer, {self.stream: "0"}, count=count
            )
        except redis.exceptions.TimeoutError:
            return []
        out: List[Entry] = []
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                out.append((_decode(msg_id), _parse_payload(fields)))
        return out

    def claim_stale(self, min_idle_ms: int, count: int = 32) -> List[Tuple[str, dict, int]]:
        """Pending entries (any consumer in the group) idle at least
        ``min_idle_ms``, claimed by this consumer, with how many times each has
        now been delivered.

        This is the retry loop: a retryable failure leaves its entry un-acked,
        and without this it was only seen again when the consumer restarted.
        XAUTOCLAIM resets the idle time and bumps the delivery count, so an entry
        comes back once per ``min_idle_ms``; the caller gives up after its
        maximum. A claimed entry whose payload has been trimmed from the stream
        comes back without fields and is skipped (acked) by the caller."""
        import redis  # lazy
        r = self._redis()
        try:
            resp = r.xautoclaim(self.stream, self.group, self.consumer,
                                min_idle_time=int(min_idle_ms), start_id="0-0", count=count)
        except redis.exceptions.TimeoutError:
            return []
        messages = resp[1] if resp and len(resp) > 1 else []
        out: List[Tuple[str, dict, int]] = []
        if not messages:
            return out
        ids = [_decode(m[0]) for m in messages]
        counts = {}
        try:
            for p in r.xpending_range(self.stream, self.group, min=ids[0], max=ids[-1],
                                      count=max(len(ids) * 4, 32), consumername=self.consumer):
                counts[_decode(p["message_id"])] = int(p.get("times_delivered") or 1)
        except Exception:
            pass
        for msg_id, fields in messages:
            mid = _decode(msg_id)
            out.append((mid, _parse_payload(fields) if fields else {}, counts.get(mid, 1)))
        return out

    def ack(self, msg_ids: List[str]) -> None:
        if msg_ids:
            self._redis().xack(self.stream, self.group, *msg_ids)


def _decode(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


def _parse_payload(fields: dict) -> dict:
    """Extract the JSON ``payload`` field (bytes-keyed from redis-py)."""
    raw = fields.get(b"payload") or fields.get("payload")
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}
