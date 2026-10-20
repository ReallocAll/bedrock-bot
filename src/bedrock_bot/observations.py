from dataclasses import dataclass, field

from bedrock_protocol import v1001

from ._projection import (
    ActorLink,
    ActorMetadata,
    ActorSignal,
    ContainerOpen,
    FullContainerName,
    ItemDescriptor,
    ReadonlyMap,
    ReadonlySequence,
    Record,
    RegistryItem,
    UpdateTrade,
)
from .types import BotStatus, InventorySlot, SlotState, TradeOffer, Vec3, WindowRef

type RegistryItems = ReadonlySequence[RegistryItem]


def _stack_id(item):
    if item is None:
        return None
    if not item.id:
        return 0
    value = getattr(item, "net_id_variant", getattr(item, "net_id", None))
    if hasattr(value, "index"):
        if value.index != 0:
            return None
        value = value.value
    value = getattr(value, "raw_id", value)
    return value if value is not None and value > 0 else None


@dataclass(frozen=True, slots=True)
class SlotObservation(Record):
    slot: InventorySlot
    item: ItemDescriptor | None = None
    identifier: str | None = None

    @property
    def state(self) -> SlotState:
        return SlotState.unknown if self.item is None else SlotState.empty if not self.item.id else SlotState.occupied

    @property
    def count(self) -> int | None:
        return None if self.item is None else 0 if not self.item.id else self.item.stack_size

    @property
    def network_id(self) -> int | None:
        return None if self.item is None else self.item.id

    @property
    def stack_id(self) -> int | None:
        return _stack_id(self.item)


@dataclass(frozen=True, slots=True)
class ActorSnapshot(Record):
    runtime_id: int
    unique_id: int
    type: str
    position: Vec3
    velocity: Vec3 = Vec3()
    pitch: float = 0
    yaw: float = 0
    head_yaw: float = 0
    body_yaw: float = 0
    metadata: ActorMetadata | None = None
    properties: v1001.PropertySyncData | None = None
    attributes: ReadonlySequence[v1001.SyncedAttribute] = field(default_factory=ReadonlySequence)
    links: ReadonlySequence[ActorLink] = field(default_factory=ReadonlySequence)
    item: ItemDescriptor | None = None
    last_event: ActorSignal | None = None
    from_fishing: bool = False


@dataclass(frozen=True, slots=True)
class InventorySnapshot(Record):
    slots: ReadonlyMap[int, ItemDescriptor]
    complete: bool = False
    full_container_name: FullContainerName | None = None
    storage_item: ItemDescriptor | None = None


@dataclass(frozen=True, slots=True)
class ContainerSnapshot(Record):
    open: ContainerOpen | None = None
    trade: UpdateTrade | None = None
    closing: bool = False


@dataclass(frozen=True, slots=True)
class ItemRegistrySnapshot(Record):
    observed: bool
    items: RegistryItems


@dataclass(frozen=True, slots=True)
class TradeOffers(Record):
    window: WindowRef
    revision: int
    source: UpdateTrade
    offers: ReadonlySequence[TradeOffer]


@dataclass(frozen=True, slots=True)
class Snapshot(BotStatus):
    actors: ReadonlyMap[int, ActorSnapshot] = field(default_factory=ReadonlyMap)
    inventories: ReadonlyMap[int, InventorySnapshot] = field(default_factory=ReadonlyMap)
    item_registry: RegistryItems = field(default_factory=ReadonlySequence)
    container: ContainerSnapshot | None = None


@dataclass(frozen=True, slots=True)
class Statistics(Record):
    packets_in: int = 0
    packets_out: int = 0
    batch_bytes_in: int = 0
    batch_bytes_out: int = 0
    corrections: int = 0
    column_packets: int = 0
    subchunks: int = 0
    deaths: int = 0
    respawns: int = 0
    probes_sent: int = 0
    probes_lost: int = 0
    decode_errors: int = 0
    connected_bytes_sent: int | None = None
    connected_bytes_received: int | None = None
    packet_counts_in: ReadonlySequence[int] = field(default_factory=lambda: ReadonlySequence((0,) * 1024))
    packet_counts_out: ReadonlySequence[int] = field(default_factory=lambda: ReadonlySequence((0,) * 1024))
