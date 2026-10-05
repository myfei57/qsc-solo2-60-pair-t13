"""Intake acquisition pipeline.

This is the only place raw readings enter the control system. For every
reading it enforces the ordering the line depends on:

    1. quality gate judges the reading
    2. the judgment is audited
    3. only then do persistence / trend / dosing see the *working* value
    4. only then does the intake valve react to the judgment

A meter judged failed is a partial failure: dosing freezes on the last
good flow, the trend is not fed bad data, and the valve holds its safe
position. Fault and recovery transitions are edge triggered, so replays
and repeated recovery readings never double-act or double-audit.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .flow import FlowRepository
from .gate import QualityGate, QualityResult
from .pumps import PumpGroups
from .quality import Verdict
from .sensor import Sensor
from .trend import Trend
from .valve import IntakeValve, ValveAction

AUDIT_QUALITY = "intake.quality"

# Verdicts worth an audit trail entry. Routine accepted/warmup readings do
# not spam the trail; every non-routine judgment and every mode edge does.
_AUDITED_VERDICTS = frozenset(
    {
        Verdict.SPIKE,
        Verdict.SUSPECT,
        Verdict.INVALID,
        Verdict.SUSTAINED_HIGH,
        Verdict.SUSTAINED_LOW,
        Verdict.METER_FAILED,
        Verdict.RECOVERING,
    }
)


@dataclass(frozen=True)
class AcquisitionReport:
    """Outcome of ingesting one raw sensor reading."""

    sensor: Sensor
    quality: QualityResult
    valve: ValveAction
    coag_dose: float

    def as_dict(self) -> dict[str, object]:
        return {
            "flow": self.sensor.flow,
            "turbidity": self.sensor.turbidity,
            "working_flow": self.quality.working_flow,
            "coag_dose": self.coag_dose,
            "quality": self.quality.as_dict(),
            "valve": self.valve.as_dict(),
        }


class Acquisition:
    """Coordinates quality gate, persistence, dosing and the intake valve."""

    def __init__(
        self,
        store: Store,
        gate: QualityGate | None = None,
        groups: PumpGroups | None = None,
        repository: FlowRepository | None = None,
        trend: Trend | None = None,
        valve: IntakeValve | None = None,
        auditor: Auditor | None = None,
        coag_ratio: "object | None" = None,
    ) -> None:
        self._store = store
        self._groups = groups or PumpGroups(store)
        self._gate = gate or QualityGate(store, self._groups)
        self._repository = repository or FlowRepository(store)
        self._trend = trend or Trend(store)
        self._auditor = auditor or Auditor(store)
        self._valve = valve or IntakeValve(store, self._auditor)
        # Ratio provider returning the current coagulant ratio; injected to
        # avoid a hard dependency on the coag package at construction time.
        self._coag_ratio = coag_ratio
        self._lock = threading.RLock()

    @property
    def gate(self) -> QualityGate:
        return self._gate

    @property
    def groups(self) -> PumpGroups:
        return self._groups

    @property
    def valve(self) -> IntakeValve:
        return self._valve

    @property
    def intake_valve(self) -> IntakeValve:
        return self._valve

    @property
    def repository(self) -> FlowRepository:
        return self._repository

    @property
    def trend(self) -> Trend:
        return self._trend

    def set_coag_ratio_provider(self, provider: object) -> None:
        self._coag_ratio = provider

    def ingest(self, sensor: Sensor) -> AcquisitionReport:
        """Judge, audit, propagate the working value, then move the valve."""

        with self._lock:
            # 1. Judge first. Nothing downstream runs against the raw value.
            result = self._gate.observe(sensor.flow)
            # 2. Audit the judgment itself.
            self._audit_verdict(result)
            # 3. Propagate only the accepted working value.
            self._repository.record_judged(
                raw=sensor.flow,
                working=result.working_flow,
                turbidity=sensor.turbidity,
                accepted=result.accepted,
            )
            if result.accepted:
                self._trend.record(result.working_flow)
            coag_dose = self._dose(result.working_flow)
            # 4. Valve reacts to the judgment, never to the raw reading.
            valve_action = self._valve.apply_verdict(result)
            return AcquisitionReport(
                sensor=sensor,
                quality=result,
                valve=valve_action,
                coag_dose=coag_dose,
            )

    def switch_pumps(self, group_id: str) -> tuple[object, bool]:
        """Change the active pump group and arm the switch transient window."""

        with self._lock:
            group, changed = self._groups.switch_to(group_id)
            self._gate.note_switch(group_id, changed)
            if changed:
                self._auditor.record(AUDIT_QUALITY, f"pump switch -> {group_id}")
            return group, changed

    def reset_quality(self) -> None:
        """Operator reset of the quality gate (audited, deliberately manual)."""

        with self._lock:
            mode = self._gate.state()["mode"]
            self._gate.reset()
            self._auditor.record(AUDIT_QUALITY, f"quality gate reset from mode {mode}")

    # -- internals ---------------------------------------------------------

    def _dose(self, working_flow: float) -> float:
        if self._coag_ratio is None:
            return 0.0
        ratio = float(self._coag_ratio())
        return working_flow * ratio

    def _audit_verdict(self, result: QualityResult) -> None:
        if result.verdict not in _AUDITED_VERDICTS and not result.fault_edge:
            return
        detail = (
            f"seq={result.sequence} verdict={result.verdict} "
            f"raw={result.raw:.4f} working={result.working_flow:.4f} "
            f"mode={result.mode_after} reason={result.reason}"
        )
        self._auditor.record(AUDIT_QUALITY, detail)
