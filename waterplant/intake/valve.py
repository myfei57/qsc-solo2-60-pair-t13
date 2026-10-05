"""Intake valve safety state.

The valve is a control state rather than a physical driver abstraction. Its
only safety positions used by acquisition are ``open`` and ``safe``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from waterplant.store.store import Store

VALVE_KEY = "intake:valve:position"


class ValvePosition(str, Enum):
    OPEN = "open"
    SAFE = "safe"


@dataclass(frozen=True)
class ValveAction:
    """Result of asking the intake valve to occupy a position."""

    requested: ValvePosition
    position: ValvePosition
    changed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested.value,
            "position": self.position.value,
            "changed": self.changed,
        }


class IntakeValve:
    """Persists the current intake valve position and makes moves idempotent."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def position(self) -> ValvePosition:
        raw, present = self._store.get(VALVE_KEY)
        if not present:
            return ValvePosition.OPEN
        try:
            return ValvePosition(raw)
        except ValueError:
            return ValvePosition.OPEN

    def move_to(self, position: ValvePosition) -> ValveAction:
        current = self.position()
        changed = current is not position
        if changed:
            self._store.put(VALVE_KEY, position.value)
        return ValveAction(requested=position, position=position, changed=changed)

    def hold_safe(self) -> ValveAction:
        return self.move_to(ValvePosition.SAFE)

    def reopen(self) -> ValveAction:
        return self.move_to(ValvePosition.OPEN)

    def state(self) -> dict[str, object]:
        position = self.position()
        return {"position": position.value, "safe": position is ValvePosition.SAFE}

    def describe(self) -> str:
        return f"intake valve position={self.position().value}"
