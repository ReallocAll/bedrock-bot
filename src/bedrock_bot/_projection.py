from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Self

import bedrock_protocol as bp


class Record:
    __slots__ = ()

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            projected = project(value)
            if value is not projected:
                object.__setattr__(self, field.name, projected)

    def copy(self) -> Self:
        return type(self)(**{field.name: detach(getattr(self, field.name)) for field in fields(self)})

    def to_dict(self) -> dict[str, object]:
        return {field.name: materialize(getattr(self, field.name)) for field in fields(self)}


class ReadonlySequence[T](Sequence[T]):
    __slots__ = ("_values",)

    def __init__(self, values=()):
        object.__setattr__(self, "_values", values if isinstance(values, bp.SequenceView) else tuple(values))

    def __setattr__(self, name, value):
        raise AttributeError("collection is immutable")

    def __delattr__(self, name):
        raise AttributeError("collection is immutable")

    def __len__(self):
        return len(self._values)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return type(self)(self[i] for i in range(*index.indices(len(self))))
        return project(self._values[index])

    def __iter__(self):
        return (project(value) for value in self._values)

    def copy(self) -> Self:
        if isinstance(self._values, bp.SequenceView):
            return type(self)(self._values.copy())
        return type(self)(detach(value) for value in self._values)

    def to_list(self) -> list[object]:
        return [materialize(value) for value in self]


class ReadonlyMap[K, V](Mapping[K, V]):
    __slots__ = ("_values",)

    def __init__(self, values=()):
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __setattr__(self, name, value):
        raise AttributeError("collection is immutable")

    def __delattr__(self, name):
        raise AttributeError("collection is immutable")

    def __len__(self):
        return len(self._values)

    def __getitem__(self, key):
        return project(self._values[key])

    def __iter__(self):
        return iter(self._values)

    def copy(self) -> Self:
        return type(self)((key, detach(value)) for key, value in self._values.items())

    def to_dict(self) -> dict[K, object]:
        return {key: materialize(value) for key, value in self.items()}


class _Payload:
    __slots__ = ("_native",)
    _fields: tuple[str, ...] = ()

    def __init__(self, value):
        object.__setattr__(self, "_native", value)

    def __setattr__(self, name, value):
        raise AttributeError("payload is immutable")

    def __delattr__(self, name):
        raise AttributeError("payload is immutable")

    def __getattr__(self, name):
        value = getattr(self._native, name)
        return project(value) if name in self._fields else value

    def copy(self) -> Self:
        return type(self)(self._native.copy())

    def to_dict(self) -> dict[str, object]:
        return {name: materialize(getattr(self, name)) for name in self._fields}

    def wire_bytes(self) -> bytes:
        return self._native.wire_bytes()


class _Versioned(_Payload):
    __slots__ = ()

    @property
    def protocol(self) -> int:
        return 2193 if type(self._native).__module__.endswith("v2193") else 1001

    def to_dict(self) -> dict[str, object]:
        return {**super().to_dict(), "protocol": self.protocol}


class ItemDescriptor(_Payload):
    __slots__ = ()
    _fields = (
        "protocol",
        "kind",
        "id",
        "stack_size",
        "aux_value",
        "block_runtime_id",
        "user_data_buffer",
        "net_id",
        "net_id_kind",
    )

    @property
    def protocol(self) -> int | None:
        if isinstance(self._native, bp.v1001.NetworkItemStackDescriptor):
            return None
        return 2193 if isinstance(self._native, bp.v2193.SerializedNetworkItemStackDescriptor) else 1001

    @property
    def kind(self) -> str:
        return "network" if self.protocol is None else f"serialized_{self.protocol}"

    @property
    def net_id(self) -> int | None:
        value = self._native.net_id if self.protocol is None else self._native.net_id_variant
        if isinstance(value, bp.Variant):
            value = value.value
        return getattr(value, "raw_id", value)

    @property
    def net_id_kind(self) -> str | None:
        if self.net_id is None:
            return None
        if self.protocol is None:
            return "stack"
        if self.protocol == 2193:
            return "raw"
        return ("stack", "request", "legacy_request")[self._native.net_id_variant.index]


class MetadataEntry(_Payload):
    __slots__ = ()
    _fields = ("id", "kind", "value")

    @property
    def kind(self) -> str:
        return ("byte", "short", "int", "float", "string", "compound", "block_pos", "int64", "vec3")[
            self._native.payload.index
        ]

    @property
    def value(self):
        return project(self._native.payload.value.value)


class MetadataEntry1001(MetadataEntry):
    __slots__ = ()


class MetadataEntry2193(MetadataEntry):
    __slots__ = ()


class ActorMetadata(_Versioned):
    __slots__ = ()
    _fields = ("entries",)

    @property
    def entries(self) -> ReadonlySequence[MetadataEntry]:
        return ReadonlySequence(self._native.data)


class ContainerOpen(_Versioned):
    __slots__ = ()
    _fields = ("container_id", "type", "pos", "entity_unique_id")


class UpdateTrade(_Versioned):
    __slots__ = ()
    _fields = (
        "container_id",
        "type",
        "size",
        "trader_tier",
        "entity_unique_id",
        "last_trading_player",
        "display_name",
        "use_new_trade_screen",
        "using_economy_trade",
        "data",
    )


class ItemStackResponse(_Versioned):
    __slots__ = ()
    _fields = ("result", "client_request_id", "containers")


class ResponseContainer(_Payload):
    __slots__ = ()
    _fields = ("full_container_name", "slots")


class ResponseSlot(_Payload):
    __slots__ = ()
    _fields = ("requested_slot", "slot", "amount", "item_stack_net_id", "custom_name", "durability_correction")


class ResponseSlot1001(ResponseSlot):
    __slots__ = ()


class ResponseSlot2193(ResponseSlot):
    __slots__ = ()


class ResponseContainer1001(ResponseContainer):
    __slots__ = ()


class ResponseContainer2193(ResponseContainer):
    __slots__ = ()


class FullContainerName(_Payload):
    __slots__ = ()
    _fields = ("name", "dynamic_id")


class ActorLink(_Payload):
    __slots__ = ()
    _fields = ("a", "b", "type", "immediate", "passenger_initiated", "vehicle_angular_velocity")


class ActorSignal(_Payload):
    __slots__ = ()
    _fields = ("runtime_id", "event_id", "data", "fire_at_position")


class RegistryItem(_Payload):
    __slots__ = ()
    _fields = ("name", "id", "is_component_based", "item_version", "component_data")


def unwrap(value):
    return value._native if isinstance(value, _Payload) else value


def project(value):
    if value is None:
        return value
    value_type = type(value)
    if value_type is int or value_type is float or value_type is bool or value_type is str:
        return value
    if isinstance(value, (_Payload, ReadonlySequence, ReadonlyMap)):
        return value
    from .types import Vec3

    if value_type is Vec3:
        return value
    if isinstance(value, bp.v1001.Vec3):
        return Vec3(value.x, value.y, value.z)
    if isinstance(value, Enum) and type(value).__module__.startswith("bedrock_protocol"):
        return int(value.value)
    if isinstance(value, (tuple, list, bp.SequenceView)):
        return ReadonlySequence(value)
    if isinstance(value, Mapping):
        return ReadonlyMap(value)
    if isinstance(
        value,
        (
            bp.v1001.NetworkItemStackDescriptor,
            bp.v1001.SerializedNetworkItemStackDescriptor,
            bp.v2193.SerializedNetworkItemStackDescriptor,
        ),
    ):
        return ItemDescriptor(value)
    if isinstance(value, (bp.v1001.SynchedActorData.CopyableDataList, bp.v2193.SynchedActorData.CopyableDataList)):
        return ActorMetadata(value)
    if isinstance(value, bp.v1001.DataItemEntry):
        return MetadataEntry1001(value)
    if isinstance(value, bp.v2193.DataItemEntry):
        return MetadataEntry2193(value)
    if isinstance(value, (bp.v1001.ContainerOpenPacket, bp.v2193.ContainerOpenPacket)):
        return ContainerOpen(value)
    if isinstance(value, (bp.v1001.UpdateTradePacket, bp.v2193.UpdateTradePacket)):
        return UpdateTrade(value)
    if isinstance(value, (bp.v1001.ItemStackResponseInfo, bp.v2193.ItemStackResponseInfo)):
        return ItemStackResponse(value)
    if isinstance(value, bp.v1001.ItemStackResponseContainerInfo):
        return ResponseContainer1001(value)
    if isinstance(value, bp.v2193.ItemStackResponseContainerInfo):
        return ResponseContainer2193(value)
    if isinstance(value, bp.v1001.ItemStackResponseSlotInfo):
        return ResponseSlot1001(value)
    if isinstance(value, bp.v2193.ItemStackResponseSlotInfo):
        return ResponseSlot2193(value)
    if isinstance(value, bp.v1001.FullContainerName):
        return FullContainerName(value)
    if isinstance(value, bp.v1001.ActorLink):
        return ActorLink(value)
    if isinstance(value, bp.v1001.ActorEventPacket):
        return ActorSignal(value)
    if isinstance(value, bp.v1001.ItemData):
        return RegistryItem(value)
    if isinstance(value, (bp.v1001.ItemStackRequestId, bp.v1001.ItemStackNetId, bp.v1001.ItemStackLegacyRequestId)):
        return value.raw_id
    return value


def detach(value):
    if isinstance(value, (Record, _Payload, ReadonlySequence, ReadonlyMap, bp.Value)):
        return value.copy()
    if type(value) is list:
        return [detach(entry) for entry in value]
    if type(value) is dict:
        return {key: detach(entry) for key, entry in value.items()}
    if type(value) is tuple:
        return tuple(detach(entry) for entry in value)
    if is_dataclass(value):
        return type(value)(**{field.name: detach(getattr(value, field.name)) for field in fields(value)})
    return value


def materialize(value):
    value = project(value)
    if isinstance(value, ReadonlySequence):
        return value.to_list()
    if isinstance(value, bp.BinaryView):
        return value.to_bytes()
    if isinstance(value, (Record, _Payload, ReadonlyMap, bp.Nbt)):
        return value.to_dict()
    if is_dataclass(value):
        return {field.name: materialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, bp.Value):
        return {key: materialize(entry) for key, entry in value.to_dict().items()}
    return value
