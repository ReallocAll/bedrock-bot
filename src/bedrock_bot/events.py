from dataclasses import dataclass, field
from enum import IntEnum
from typing import TypeAlias

from ._projection import ActorSignal, ItemStackResponse, ReadonlySequence, Record, materialize
from .observations import (
    ActorSnapshot,
    ContainerSnapshot,
    InventorySnapshot,
    ItemDescriptor,
    RegistryItems,
)
from .operations import ItemRequestOutcome
from .types import BotState, BotToken, ItemRequestKey, Vec3, WindowRef


class EventKind(IntEnum):
    state = 0
    tick = 1
    position = 2
    chunk = 3
    death = 4
    respawn = 5
    dimension = 6
    latency = 7
    actor = 8
    actor_signal = 9
    take_item = 10
    inventory = 11
    item_registry = 12
    container = 13
    health = 14
    item_request = 15
    actor_motion = 16
    inventory_slot = 17
    count = 18


def event_mask(kind: EventKind) -> int:
    return 1 << int(kind) if 0 <= int(kind) < 18 else 0


LEGACY_EVENTS = (1 << 16) - 1
DEFAULT_EVENTS = (
    event_mask(EventKind.state)
    | event_mask(EventKind.item_request)
    | event_mask(EventKind.container)
    | event_mask(EventKind.death)
    | event_mask(EventKind.respawn)
    | event_mask(EventKind.dimension)
)
ALL_EVENTS = (1 << 18) - 1


def validate_mask(mask: int) -> int:
    if type(mask) is not int or mask < 0 or mask & ~ALL_EVENTS:
        raise ValueError("invalid event interests")
    return mask


@dataclass(frozen=True, slots=True)
class StateEvent(Record):
    state: BotState
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TickEvent(Record):
    tick: int
    lateness_us: int


@dataclass(frozen=True, slots=True)
class PositionEvent(Record):
    position: Vec3
    teleport: bool = False
    correction: bool = False


@dataclass(frozen=True, slots=True)
class DeathEvent(Record):
    pass


@dataclass(frozen=True, slots=True)
class RespawnEvent(Record):
    pass


@dataclass(frozen=True, slots=True)
class DimensionEvent(Record):
    dimension: int
    position: Vec3


@dataclass(frozen=True, slots=True)
class LatencyEvent(Record):
    round_trip_us: int


@dataclass(frozen=True, slots=True)
class HealthEvent(Record):
    health: float


@dataclass(frozen=True, slots=True)
class ActorEvent(Record):
    class Kind(IntEnum):
        added = 0
        moved = 1
        metadata = 2
        removed = 3

    kind: Kind
    actor: ActorSnapshot


@dataclass(frozen=True, slots=True)
class ActorMotionEvent(Record):
    session_id: int
    runtime_id: int
    unique_id: int
    position: Vec3
    velocity: Vec3
    pitch: float
    yaw: float
    head_yaw: float
    body_yaw: float


@dataclass(frozen=True, slots=True)
class InventoryEvent(Record):
    inventory_id: int
    inventory: InventorySnapshot


@dataclass(frozen=True, slots=True)
class InventorySlotEvent(Record):
    session_id: int
    inventory_id: int
    slot: int
    item: ItemDescriptor


@dataclass(frozen=True, slots=True)
class ItemRegistryEvent(Record):
    items: RegistryItems


@dataclass(frozen=True, slots=True)
class ContainerEvent(Record):
    container: ContainerSnapshot | None
    reference: WindowRef | None
    server_closed: bool = False

    @property
    def has_container(self) -> bool:
        return self.container is not None

    @property
    def closing(self) -> bool:
        return self.container is not None and self.container.closing


@dataclass(frozen=True, slots=True)
class ItemRequestEvent(Record):
    key: ItemRequestKey
    outcome: ItemRequestOutcome
    response: ItemStackResponse | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            name: materialize(getattr(self, name))
            for name in ("session_id", "request_id", "outcome", "response", "reason", "bot", "window")
        }

    @property
    def session_id(self) -> int:
        return self.key.session_id

    @property
    def request_id(self) -> int:
        return self.key.request_id

    @property
    def response_code(self) -> int | None:
        return None if self.response is None else int(self.response.result)

    @property
    def bot(self) -> BotToken:
        return self.key.bot

    @property
    def window(self) -> WindowRef | None:
        return self.key.window


@dataclass(frozen=True, slots=True)
class ActorSignalEvent(Record):
    packet: ActorSignal


@dataclass(frozen=True, slots=True)
class TakeItemEvent(Record):
    item_runtime_id: int
    actor_runtime_id: int


@dataclass(frozen=True, slots=True)
class ChunkEvent(Record):
    class Kind(IntEnum):
        column = 0
        subchunks = 1

    kind: Kind
    dimension: int
    x: int
    z: int
    entries: int = 0
    subchunk_limit: int | None = None
    blob_ids: ReadonlySequence[int] = field(default_factory=ReadonlySequence)


Event: TypeAlias = (
    StateEvent
    | TickEvent
    | PositionEvent
    | ChunkEvent
    | DeathEvent
    | RespawnEvent
    | DimensionEvent
    | LatencyEvent
    | ActorEvent
    | ActorSignalEvent
    | TakeItemEvent
    | InventoryEvent
    | ItemRegistryEvent
    | ContainerEvent
    | HealthEvent
    | ItemRequestEvent
    | ActorMotionEvent
    | InventorySlotEvent
)
