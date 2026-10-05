"""Acquisition facade: judge a reading, persist it, then act on the valve."""

from __future__ import annotations

from dataclasses import dataclass

from waterplant.audit import Auditor

from .flow import FlowRepository
from .quality import (
    AcquisitionState,
    FlowQualityMonitor,
    PumpContext,
    Quality,
    ReadingVerdict,
    load_state,
    save_state,
)
from .sensor import Sensor
from .trend import Trend
from .valve import IntakeValve, ValveAction

QUALITY_AUDIT = "intake-quality"
VALVE_AUDIT = "intake-valve"


@dataclass(frozen=True)
class AcquisitionReport:
    """Judgement and the safety action exposed to the console."""

    verdict: ReadingVerdict
    valve: ValveAction
    recorded: bool

    def as_dict(self) -> dict[str, object]:
        payload = self.verdict.as_dict()
        payload["recorded"] = self.recorded
        payload["valve"] = self.valve.as_dict()
        return payload


class AcquisitionService:
    """Applies the same acquisition sequence to HTTP and control-cycle calls."""

    def __init__(
        self,
        repository: FlowRepository,
        trend: Trend,
        valve: IntakeValve,
        auditor: Auditor,
        monitor: FlowQualityMonitor | None = None,
    ) -> None:
        self._repository = repository
        self._trend = trend
        self._valve = valve
        self._auditor = auditor
        self._monitor = monitor or FlowQualityMonitor()
        self._store = repository.store

    def collect(
        self, sensor: Sensor, pump: PumpContext | None = None
    ) -> AcquisitionReport:
        """Classify first; only usable readings enter storage, dosing and trend."""

        pump = pump or PumpContext()
        previous = load_state(self._store)
        state = AcquisitionState.from_dict(previous.as_dict())
        previous_status = previous.status
        state, verdict = self._monitor.read(state, sensor.flow, sensor.turbidity, pump)

        recorded = False
        if verdict.usable and verdict.accepted_flow is not None:
            usable_sensor = Sensor(
                flow=verdict.accepted_flow,
                turbidity=verdict.turbidity,
            )
            self._repository.record(usable_sensor)
            self._trend.record(verdict.accepted_flow)
            recorded = True
        save_state(self._store, state)

        if state.status is not Quality.ACCEPTED or state.status is not previous_status:
            self._auditor.record(
                QUALITY_AUDIT,
                " ".join(
                    (
                        state.status.value,
                        f"raw={_number(verdict.raw_flow)}",
                        f"accepted={_number(verdict.accepted_flow)}",
                        f"pump_group={state.pump_group}",
                        verdict.reason,
                    )
                ),
            )

        # The verdict is fully persisted before any valve movement is attempted.
        valve = self._valve.hold_safe() if verdict.meter_fault else self._valve.reopen()
        if valve.changed:
            self._auditor.record(
                VALVE_AUDIT,
                f"{valve.position.value} reason={state.status.value}",
            )

        return AcquisitionReport(verdict=verdict, valve=valve, recorded=recorded)

    def state(self) -> AcquisitionState:
        return load_state(self._store)

    def quality(self) -> ReadingVerdict:
        state = self.state()
        return ReadingVerdict(
            raw_flow=state.last_raw,
            accepted_flow=state.last_accepted,
            turbidity=state.last_turbidity,
            baseline=state.baseline,
            quality=state.status,
            low_streak=state.low_streak,
            invalid_streak=state.invalid_streak,
            observed_remaining=state.observed_remaining,
            observed_values=list(state.observed_values or []),
            pump_group=state.pump_group,
            meter_fault=state.meter_fault,
            reason="last persisted acquisition verdict",
        )


def _number(value: float | None) -> str:
    return "none" if value is None else f"{value:.4f}"
