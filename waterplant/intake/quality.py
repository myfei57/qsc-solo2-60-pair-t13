"""Plausibility rules for intake flow readings.

The rules in this module are deliberately fixed: the HTTP layer and the control
cycle must classify the same raw reading in the same way.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum

from waterplant.store.store import Store

STATE_KEY = "intake:acquisition:state"

# A steady reading is accepted when it is within this relative band.
LOW_FACTOR = 0.80
HIGH_FACTOR = 1.25

# Three unusable low/invalid readings are the fixed meter-failure threshold.
FAILURE_SAMPLES = 3

# A changed pump group, or an asserted pump switch, gets this many held samples.
SWITCH_OBSERVATION_SAMPLES = 2

# Independent pump-reported flow must be within this tolerance to corroborate.
EXPECTED_TOLERANCE = 0.20


class Quality(str, Enum):
    """Result of applying the fixed intake quality rules."""

    ACCEPTED = "accepted"
    REAL_REDUCTION = "real_reduction"
    RECOVERED = "recovered"
    SPIKE_DISCARDED = "spike_discarded"
    SPIKE_OBSERVED = "spike_observed"
    LOW_OBSERVED = "low_observed"
    SUSPECT_LOW = "suspect_low"
    METER_FAULT = "meter_fault"
    INVALID = "invalid"

    @property
    def usable(self) -> bool:
        return self in (Quality.ACCEPTED, Quality.REAL_REDUCTION, Quality.RECOVERED)

    @property
    def fault(self) -> bool:
        return self is Quality.METER_FAULT


@dataclass(frozen=True)
class PumpContext:
    """Pump information used to distinguish a transient from a real change."""

    group: str = "default"
    switching: bool = False
    expected_flow: float | None = None

    @classmethod
    def from_values(
        cls, group: str = "default", switching: bool = False, expected_flow: float | None = None
    ) -> "PumpContext":
        group = group or "default"
        if expected_flow is not None:
            if not math.isfinite(expected_flow) or expected_flow < 0.0:
                raise ValueError("pump expected flow must be a finite non-negative number")
        return cls(group=group, switching=switching, expected_flow=expected_flow)


@dataclass
class AcquisitionState:
    """Persisted classifier state."""

    initialized: bool = False
    baseline: float = 0.0
    last_accepted: float | None = None
    last_raw: float | None = None
    last_turbidity: float = 0.0
    status: Quality = Quality.ACCEPTED
    low_streak: int = 0
    invalid_streak: int = 0
    observed_remaining: int = 0
    observed_values: list[float] | None = None
    pump_group: str = "default"
    meter_fault: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "initialized": self.initialized,
            "baseline": self.baseline,
            "last_accepted": self.last_accepted,
            "last_raw": self.last_raw,
            "last_turbidity": self.last_turbidity,
            "status": self.status.value,
            "low_streak": self.low_streak,
            "invalid_streak": self.invalid_streak,
            "observed_remaining": self.observed_remaining,
            "observed_values": self.observed_values or [],
            "pump_group": self.pump_group,
            "meter_fault": self.meter_fault,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "AcquisitionState":
        if not isinstance(payload, dict):
            return cls()
        try:
            status = Quality(str(payload.get("status", Quality.ACCEPTED.value)))
        except ValueError:
            status = Quality.ACCEPTED
        baseline = _optional_float(payload.get("baseline"))
        last_accepted = _optional_float(payload.get("last_accepted"))
        last_raw = _optional_float(payload.get("last_raw"))
        turbidity = _optional_float(payload.get("last_turbidity"))
        observed_values: list[float] = []
        raw_values = payload.get("observed_values", [])
        if isinstance(raw_values, list):
            for item in raw_values:
                value = _optional_float(item)
                if value is not None and valid_range(value):
                    observed_values.append(value)
        return cls(
            initialized=bool(payload.get("initialized", False)),
            baseline=baseline or 0.0,
            last_accepted=last_accepted,
            last_raw=last_raw,
            last_turbidity=turbidity or 0.0,
            status=status,
            low_streak=max(int(payload.get("low_streak", 0) or 0), 0),
            invalid_streak=max(int(payload.get("invalid_streak", 0) or 0), 0),
            observed_remaining=max(int(payload.get("observed_remaining", 0) or 0), 0),
            observed_values=observed_values,
            pump_group=str(payload.get("pump_group", "default")),
            meter_fault=bool(payload.get("meter_fault", False)),
        )


@dataclass(frozen=True)
class ReadingVerdict:
    """One classification and the state it produced."""

    raw_flow: float | None
    accepted_flow: float | None
    turbidity: float
    baseline: float
    quality: Quality
    low_streak: int
    invalid_streak: int
    observed_remaining: int
    observed_values: list[float]
    pump_group: str
    meter_fault: bool
    reason: str

    @property
    def usable(self) -> bool:
        return self.quality.usable

    def as_dict(self) -> dict[str, object]:
        return {
            "raw_flow": self.raw_flow,
            "accepted_flow": self.accepted_flow,
            "flow": self.accepted_flow,
            "turbidity": self.turbidity,
            "baseline": self.baseline,
            "quality": self.quality.value,
            "usable": self.usable,
            "meter_fault": self.meter_fault,
            "low_streak": self.low_streak,
            "invalid_streak": self.invalid_streak,
            "observed_remaining": self.observed_remaining,
            "observed_values": self.observed_values,
            "low_factor": LOW_FACTOR,
            "high_factor": HIGH_FACTOR,
            "failure_samples": FAILURE_SAMPLES,
            "switch_observation_samples": SWITCH_OBSERVATION_SAMPLES,
            "pump_group": self.pump_group,
            "reason": self.reason,
        }


def load_state(store: Store) -> AcquisitionState:
    raw, present = store.get(STATE_KEY)
    if not present:
        return AcquisitionState()
    try:
        return AcquisitionState.from_dict(json.loads(raw))
    except (ValueError, TypeError):
        return AcquisitionState()


def save_state(store: Store, state: AcquisitionState) -> None:
    store.put(STATE_KEY, json.dumps(state.as_dict(), ensure_ascii=False))


def valid_range(value: float) -> bool:
    return math.isfinite(value) and 0.0 <= value <= 1_000_000.0


def expected_supports(value: float, expected: float | None) -> bool:
    """Return whether the pump's own flow indication corroborates the meter."""

    if expected is None or not valid_range(expected):
        return False
    tolerance = max(expected * EXPECTED_TOLERANCE, 1e-9)
    return abs(value - expected) <= tolerance


class FlowQualityMonitor:
    """Stateful, deterministic application of the fixed intake rules."""

    def read(
        self, state: AcquisitionState, raw_flow: float, turbidity: float, pump: PumpContext
    ) -> tuple[AcquisitionState, ReadingVerdict]:
        finite_raw = raw_flow if isinstance(raw_flow, (int, float)) and math.isfinite(raw_flow) else None
        state.last_raw = finite_raw
        state.last_turbidity = turbidity if math.isfinite(turbidity) else 0.0

        group_changed = state.initialized and pump.group != state.pump_group
        state.pump_group = pump.group
        # A stale ``switching`` flag must not be able to defer the fixed
        # failure threshold forever. Only a group change or a fresh edge starts
        # a new observation window.
        starting_switch = pump.switching and state.observed_remaining == 0
        if group_changed or starting_switch:
            state.observed_remaining = SWITCH_OBSERVATION_SAMPLES
            state.observed_values = []

        if not valid_range(raw_flow):
            state.invalid_streak += 1
            state.low_streak = 0
            if state.invalid_streak >= FAILURE_SAMPLES or state.meter_fault:
                return self._fault(
                    state,
                    finite_raw,
                    f"invalid reading for {state.invalid_streak} consecutive samples",
                )
            state.status = Quality.INVALID
            state.meter_fault = False
            return state, self._verdict(state, None, "reading is outside the instrument range")

        if not state.initialized:
            return self._accept(state, raw_flow, Quality.ACCEPTED, "cold start established baseline")

        if state.meter_fault:
            return self._accept(state, raw_flow, Quality.RECOVERED, "valid reading recovered meter")

        low_limit = state.baseline * LOW_FACTOR
        high_limit = state.baseline * HIGH_FACTOR
        supported = expected_supports(raw_flow, pump.expected_flow)

        if supported and raw_flow < low_limit:
            return self._accept(
                state,
                raw_flow,
                Quality.REAL_REDUCTION,
                "pump-indicated flow corroborates reduction",
                reset_baseline=True,
            )
        if supported and raw_flow > high_limit:
            return self._accept(
                state,
                raw_flow,
                Quality.ACCEPTED,
                "pump-indicated flow corroborates increase",
                reset_baseline=True,
            )

        if raw_flow < low_limit:
            if state.observed_remaining > 0:
                state.observed_values = [*state.observed_values, raw_flow]
                state.observed_remaining -= 1
                state.status = Quality.LOW_OBSERVED
                state.meter_fault = False
                self._finish_observation_window(state)
                return state, self._verdict(state, None, "low reading held during pump switch observation")

            state.low_streak += 1
            state.invalid_streak = 0
            if state.low_streak >= FAILURE_SAMPLES:
                return self._fault(
                    state,
                    raw_flow,
                    f"low for {FAILURE_SAMPLES} consecutive readings without pump corroboration",
                )
            state.status = Quality.SUSPECT_LOW
            return state, self._verdict(state, None, "low reading awaits consecutive confirmation")

        state.low_streak = 0
        state.invalid_streak = 0

        if raw_flow > high_limit:
            if state.observed_remaining > 0:
                state.observed_values = [*state.observed_values, raw_flow]
                state.observed_remaining -= 1
                state.status = Quality.SPIKE_OBSERVED
                self._finish_observation_window(state)
                return state, self._verdict(state, None, "high reading marked during pump switch observation")
            state.status = Quality.SPIKE_DISCARDED
            return state, self._verdict(state, None, "isolated spike discarded for steady pump group")

        state.observed_remaining = 0
        return self._accept(state, raw_flow, Quality.ACCEPTED, "reading within accepted band")

    def _finish_observation_window(self, state: AcquisitionState) -> None:
        """Adopt the observed pump-group level after the fixed hold count."""

        if state.observed_remaining == 0 and state.observed_values:
            first, second = state.observed_values[0], state.observed_values[-1]
            tolerance = max(first * EXPECTED_TOLERANCE, 1e-9)
            if abs(first - second) <= tolerance:
                state.baseline = (first + second) / 2.0
                state.low_streak = 0
                state.invalid_streak = 0
            else:
                state.observed_values = []

    def _accept(
        self,
        state: AcquisitionState,
        value: float,
        quality: Quality,
        reason: str,
        reset_baseline: bool = False,
    ) -> tuple[AcquisitionState, ReadingVerdict]:
        state.meter_fault = False
        state.low_streak = 0
        state.invalid_streak = 0
        state.observed_remaining = 0
        state.observed_values = []
        state.last_accepted = value
        if reset_baseline or not state.initialized or quality is Quality.RECOVERED:
            state.baseline = value
            state.initialized = True
        elif quality is Quality.ACCEPTED and state.baseline > 0:
            state.baseline = state.baseline * 0.75 + value * 0.25
        else:
            state.baseline = value
        state.status = quality
        return state, self._verdict(state, value, reason)

    def _fault(
        self, state: AcquisitionState, raw: float | None, reason: str
    ) -> tuple[AcquisitionState, ReadingVerdict]:
        state.initialized = True
        state.meter_fault = True
        state.status = Quality.METER_FAULT
        state.observed_remaining = 0
        state.observed_values = []
        return state, self._verdict(state, None, reason, raw=raw)

    def _verdict(
        self,
        state: AcquisitionState,
        accepted: float | None,
        reason: str,
        raw: float | None | object = ...,
    ) -> ReadingVerdict:
        if raw is ...:
            raw = state.last_raw
        return ReadingVerdict(
            raw_flow=raw if raw is None or isinstance(raw, float) else float(raw),
            accepted_flow=accepted,
            turbidity=state.last_turbidity,
            baseline=state.baseline,
            quality=state.status,
            low_streak=state.low_streak,
            invalid_streak=state.invalid_streak,
            observed_remaining=state.observed_remaining,
            observed_values=list(state.observed_values or []),
            pump_group=state.pump_group,
            meter_fault=state.meter_fault,
            reason=reason,
        )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
