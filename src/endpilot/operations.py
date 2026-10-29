import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from threading import Lock

from ._projection import ItemStackResponse, ReadonlySequence, Record
from .observations import SlotObservation
from .types import ItemRequestKey


class ItemOperationPhase(IntEnum):
    queued = 0
    submitted = 1
    terminal = 2


class ItemRequestOutcome(IntEnum):
    completed = 0
    rejected = 1
    cancelled = 2
    indeterminate = 3


class ItemCancellation(IntEnum):
    cancelled = 0
    already_submitted = 1
    already_terminal = 2


@dataclass(frozen=True, slots=True)
class ItemOperationResult(Record):
    key: ItemRequestKey
    outcome: ItemRequestOutcome
    server_result: int | None = None
    touched_slots: ReadonlySequence[SlotObservation] = field(default_factory=ReadonlySequence)
    response: ItemStackResponse | None = None
    reason: str = ""


class ItemOperation:
    __slots__ = ("_done", "_key", "_lock", "_loop", "_record")

    def __init__(self, key: ItemRequestKey, loop):
        self._key = key
        self._lock = Lock()
        self._record = (ItemOperationPhase.queued, None)
        self._loop = loop
        self._done = loop.create_future()

    @property
    def key(self) -> ItemRequestKey:
        return self._key

    @property
    def phase(self) -> ItemOperationPhase:
        with self._lock:
            return self._record[0]

    @property
    def result(self) -> ItemOperationResult | None:
        with self._lock:
            return self._record[1]

    def _submit(self):
        with self._lock:
            if self._record[0] != ItemOperationPhase.queued:
                raise RuntimeError("operation is no longer queued")
            self._record = (ItemOperationPhase.submitted, None)

    def _finish(self, result):
        with self._lock:
            if self._record[0] == ItemOperationPhase.terminal:
                return False
            self._record = (ItemOperationPhase.terminal, result)
        self._done.set_result(result)
        return True

    async def wait(self, timeout: float | None = None) -> ItemOperationResult:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("operation wait requires its owner loop")
        async with asyncio.timeout(timeout):
            return await asyncio.shield(self._done)
