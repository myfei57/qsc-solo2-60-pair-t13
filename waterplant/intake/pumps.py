"""Pump groups feeding the intake meter.

Each group declares what the meter should read while its pumps run and how
short term spikes are to be handled. Switching the active group (a pump
changeover) widens spike tolerance for the first readings afterwards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.store.store import Store

from .quality import SpikePolicy

GROUPS_KEY = "intake:pump-groups"
ACTIVE_GROUP_KEY = "intake:pump-groups:active"
# Readings right after a changeover are known to carry switch transients.
SWITCH_TRANSIENT_READINGS = 3

POLICIES = (SpikePolicy.DROP, SpikePolicy.FLAG)


@dataclass(frozen=True)
class PumpGroup:
    """One configured pump group."""

    id: str
    pumps: int
    nominal_flow: float
    spike_policy: str = SpikePolicy.DROP

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pumps": self.pumps,
            "nominal_flow": self.nominal_flow,
            "spike_policy": self.spike_policy,
        }


def _validate(group_id: str, pumps: int, nominal_flow: float, policy: str) -> None:
    if not group_id:
        raise ValueError("pump group id is required")
    if pumps <= 0:
        raise ValueError("pump group must run at least one pump")
    if nominal_flow <= 0:
        raise ValueError("pump group nominal flow must be positive")
    if policy not in POLICIES:
        raise ValueError(f"spike policy must be one of {POLICIES}")


class PumpGroups:
    """Store backed registry of pump groups and the active group pointer."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # -- configuration -----------------------------------------------------

    def configure(
        self,
        group_id: str,
        pumps: int,
        nominal_flow: float,
        spike_policy: str = SpikePolicy.DROP,
    ) -> PumpGroup:
        _validate(group_id, pumps, nominal_flow, spike_policy)
        groups = self._load()
        groups[group_id] = PumpGroup(
            id=group_id,
            pumps=pumps,
            nominal_flow=float(nominal_flow),
            spike_policy=spike_policy,
        )
        self._save(groups)
        return groups[group_id]

    def remove(self, group_id: str) -> None:
        groups = self._load()
        if group_id not in groups:
            raise ValueError(f"pump group {group_id} not found")
        del groups[group_id]
        self._save(groups)
        if self.active_id() == group_id:
            self._store.delete(ACTIVE_GROUP_KEY)

    def groups(self) -> list[PumpGroup]:
        return [self._load()[key] for key in sorted(self._load())]

    def get(self, group_id: str) -> PumpGroup | None:
        return self._load().get(group_id)

    # -- active group / changeover ----------------------------------------

    def active_id(self) -> str | None:
        raw, present = self._store.get(ACTIVE_GROUP_KEY)
        return raw or None if present else None

    def active(self) -> PumpGroup | None:
        group_id = self.active_id()
        if group_id is None:
            return None
        return self._load().get(group_id)

    def activate(self, group_id: str) -> PumpGroup:
        """Mark a group as running. Idempotent: same group is a no-op."""

        groups = self._load()
        if group_id not in groups:
            raise ValueError(f"pump group {group_id} not found")
        if self.active_id() == group_id:
            return groups[group_id]
        self._store.put(ACTIVE_GROUP_KEY, group_id)
        return groups[group_id]

    def switch_to(self, group_id: str) -> tuple[PumpGroup, bool]:
        """Change over to another group, reporting whether it was a change.

        Re-selecting the group already on duty is an idempotent no-op and
        reports ``False`` so no transient window or valve action refires.
        """

        groups = self._load()
        if group_id not in groups:
            raise ValueError(f"pump group {group_id} not found")
        changed = self.active_id() != group_id
        if changed:
            self._store.put(ACTIVE_GROUP_KEY, group_id)
        return groups[group_id], changed

    def describe(self) -> str:
        active = self.active()
        if active is None:
            return f"pump groups={len(self._load())} active="
        return (
            f"pump groups={len(self._load())} active={active.id} "
            f"pumps={active.pumps} nominal={active.nominal_flow:.4f} "
            f"spike_policy={active.spike_policy}"
        )

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, PumpGroup]:
        raw, present = self._store.get(GROUPS_KEY)
        if not present:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        groups: dict[str, PumpGroup] = {}
        for key, item in parsed.items():
            if not isinstance(item, dict):
                continue
            try:
                groups[str(key)] = PumpGroup(
                    id=str(item["id"]),
                    pumps=int(item["pumps"]),
                    nominal_flow=float(item["nominal_flow"]),
                    spike_policy=str(item.get("spike_policy", SpikePolicy.DROP)),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return groups

    def _save(self, groups: dict[str, PumpGroup]) -> None:
        self._store.put(
            GROUPS_KEY,
            json.dumps(
                {key: group.as_dict() for key, group in groups.items()},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
