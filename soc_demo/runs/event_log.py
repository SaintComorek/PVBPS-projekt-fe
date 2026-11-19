"""História udalostí behu s možnosťou čakať na nové udalosti (pre SSE)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from soc_demo.domain.events import RunEvent


class EventLog:
    def __init__(self) -> None:
        self._events: list[RunEvent] = []
        self._closed = False
        self._changed = asyncio.Condition()

    @property
    def events(self) -> list[RunEvent]:
        return list(self._events)

    @property
    def closed(self) -> bool:
        return self._closed

    def next_seq(self) -> int:
        return len(self._events) + 1

    async def append(self, event: RunEvent) -> None:
        async with self._changed:
            self._events.append(event)
            self._changed.notify_all()

    async def close(self) -> None:
        async with self._changed:
            self._closed = True
            self._changed.notify_all()

    async def follow(self, *, after_seq: int = 0, heartbeat_s: float = 15.0) -> AsyncIterator[RunEvent | None]:
        """Vráti históriu od `after_seq` a potom nové udalosti. `None` znamená heartbeat."""
        index = after_seq
        while True:
            async with self._changed:
                try:
                    await asyncio.wait_for(
                        self._changed.wait_for(lambda seen=index: len(self._events) > seen or self._closed),
                        timeout=heartbeat_s,
                    )
                except TimeoutError:
                    pending: list[RunEvent] = []
                    timed_out = True
                else:
                    pending = self._events[index:]
                    timed_out = False
                finished = self._closed and len(self._events) <= index + len(pending)
            if timed_out:
                yield None
                continue
            for event in pending:
                index += 1
                yield event
            if finished:
                return
