"""Intake valve guard sitting behind the quality gate.

The valve only moves *after* the quality gate has judged a reading. While
the gate is in degraded mode (partial meter failure) the valve is held at
its last commanded position - the safe hold position - and movement
requests are refused. Entering and leaving the safe hold are edge
triggered, so repeated readings or repeated recovery signals perform the
action and write the audit entry exactly once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .gate import QualityResult
from .quality import Mode

VALVE_KEY = "intake:valve"

MODE_AUTO = "auto"
MODE_SAFE_HOLD = "safe_hold"

AUDIT_VALVE = "intake.valve"


@dataclass(frozen=True)
class ValveAction:
    """What the guard decided for one request."""

    action: str
    """One of ``hold``, ``move``, ``refused``, ``resume`` (or ``""``)."""
    requested: float
    position: float
    mode: str
    detail: str

    @property
    def moved(self) -> bool:
        return self.action in ("move", "resume")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "requested": self.requested,
            "position": self.position,
            "mode": self.mode,
            "detail": self.detail,
        }


class IntakeValve:
    """Store backed valve position guarded by the quality gate mode."""

    def __init__(self, store: Store, auditor: Auditor) -> None:
        self._store = store
        self._auditor = auditor

    def position(self) -> float:
        state = self._load()
        return state["position"]

    def mode(self) -> str:
        return self._load()["mode"]

    def state(self) -> dict[str, object]:
        state = self._load()
        return {
            "position": state["position"],
            "mode": state["mode"],
            "last_action": state["last_action"],
            "last_detail": state["last_detail"],
        }

    # -- driven by the quality gate ---------------------------------------

    def apply_verdict(self, result: QualityResult) -> ValveAction:
        """React to a quality judgment, idempotently on mode edges."""

        if result.fault_edge:
            return self._enter_safe_hold(result)
        if result.recovery_edge:
            return self._resume(result)
        state = self._load()
        return ValveAction(
            action="",
            requested=state["position"],
            position=state["position"],
            mode=state["mode"],
            detail="no valve action; quality mode unchanged",
        )

    def command(self, target: float) -> ValveAction:
        """Move the valve to ``target`` percent, unless it must hold safe."""

        if not 0.0 <= target <= 100.0:
            raise ValueError("valve position must be between 0 and 100")
        state = self._load()
        if state["mode"] == MODE_SAFE_HOLD:
            # Idempotent refusal: the valve stays put and nothing is logged
            # on every denied request, so a storm of commands cannot flood
            # the audit trail.
            return ValveAction(
                action="refused",
                requested=target,
                position=state["position"],
                mode=MODE_SAFE_HOLD,
                detail="meter degraded; valve held at safe position",
            )
        self._write(
            position=target,
            mode=MODE_AUTO,
            last_action="move",
            last_detail=f"valve moved from {state['position']:.4f} to {target:.4f}",
        )
        self._auditor.record(AUDIT_VALVE, f"move {state['position']:.4f}->{target:.4f}")
        return ValveAction(
            action="move",
            requested=target,
            position=target,
            mode=MODE_AUTO,
            detail=f"valve moved from {state['position']:.4f} to {target:.4f}",
        )

    # -- edges -------------------------------------------------------------

    def _enter_safe_hold(self, result: QualityResult) -> ValveAction:
        state = self._load()
        # Idempotent guard: fault edge should only arrive once, but never
        # re-enter (and re-audit) if the guard is replayed.
        if state["mode"] == MODE_SAFE_HOLD and state["last_action"] == "hold":
            return ValveAction(
                action="hold",
                requested=state["position"],
                position=state["position"],
                mode=MODE_SAFE_HOLD,
                detail="already at safe position",
            )
        detail = (
            f"meter failure suspected; valve held at {state['position']:.4f} "
            f"(sequence {result.sequence})"
        )
        self._write(
            position=state["position"],
            mode=MODE_SAFE_HOLD,
            last_action="hold",
            last_detail=detail,
        )
        self._auditor.record(AUDIT_VALVE, f"hold {state['position']:.4f} degraded")
        return ValveAction(
            action="hold",
            requested=state["position"],
            position=state["position"],
            mode=MODE_SAFE_HOLD,
            detail=detail,
        )

    def _resume(self, result: QualityResult) -> ValveAction:
        state = self._load()
        if state["mode"] == MODE_AUTO:
            return ValveAction(
                action="resume",
                requested=state["position"],
                position=state["position"],
                mode=MODE_AUTO,
                detail="valve already in auto",
            )
        detail = f"meter recovered; valve control resumed (sequence {result.sequence})"
        self._write(
            position=state["position"],
            mode=MODE_AUTO,
            last_action="resume",
            last_detail=detail,
        )
        self._auditor.record(AUDIT_VALVE, f"resume {state['position']:.4f} auto")
        return ValveAction(
            action="resume",
            requested=state["position"],
            position=state["position"],
            mode=MODE_AUTO,
            detail=detail,
        )

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, object]:
        raw, present = self._store.get(VALVE_KEY)
        if not present:
            return {
                "position": 0.0,
                "mode": MODE_AUTO,
                "last_action": "",
                "last_detail": "",
            }
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {
                "position": 0.0,
                "mode": MODE_AUTO,
                "last_action": "",
                "last_detail": "",
            }
        if not isinstance(parsed, dict):
            return {
                "position": 0.0,
                "mode": MODE_AUTO,
                "last_action": "",
                "last_detail": "",
            }
        try:
            position = float(parsed.get("position", 0.0))
        except (TypeError, ValueError):
            position = 0.0
        mode = str(parsed.get("mode", MODE_AUTO))
        if mode not in (MODE_AUTO, MODE_SAFE_HOLD):
            mode = MODE_AUTO
        return {
            "position": position,
            "mode": mode,
            "last_action": str(parsed.get("last_action", "")),
            "last_detail": str(parsed.get("last_detail", "")),
        }

    def _write(
        self, position: float, mode: str, last_action: str, last_detail: str
    ) -> None:
        self._store.put(
            VALVE_KEY,
            json.dumps(
                {
                    "position": position,
                    "mode": mode,
                    "last_action": last_action,
                    "last_detail": last_detail,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
