"""Stateful intake flow quality gate.

Consumes one raw reading at a time and produces a verdict before any
downstream calculation or valve movement is allowed to happen. The gate
holds the single accepted baseline used by every consumer, so dosing,
trend and valve control all apply the same judgment.

Modes:

* ``normal``    - readings are accepted against the current baseline
* ``observing`` - an out-of-band jump arrived; repeated readings decide
                  whether it was a transient spike or a sustained change
* ``degraded``  - too many implausible readings in a row: treated as a
                  partial failure (suspect meter), the valve holds a safe
                  position until the meter produces calm plausible readings
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.store.store import Store

from . import quality as rules
from .pumps import SWITCH_TRANSIENT_READINGS, PumpGroup, PumpGroups
from .quality import Mode, Verdict

QUALITY_KEY = "intake:quality"
SUSPECTS_KEY = "intake:quality:suspects"
MAX_SUSPECTS = 20

DIRECTION_LOW = "low"
DIRECTION_HIGH = "high"


@dataclass(frozen=True)
class QualityResult:
    """The judgment for one raw reading."""

    sequence: int
    raw: float
    verdict: str
    mode_before: str
    mode_after: str
    accepted: bool
    """Whether the reading participates in downstream calculations."""
    working_flow: float
    """The flow value downstream (dosing, trend, valve) must use."""
    baseline: float
    last_good: float
    invalid_streak: int
    suspect_streak: int
    suspect_direction: str
    switching: bool
    group_id: str
    spike_policy: str
    reason: str

    @property
    def degraded(self) -> bool:
        return self.mode_after == Mode.DEGRADED

    @property
    def fault_edge(self) -> bool:
        """True on the single reading that enters DEGRADED mode."""

        return self.mode_before != Mode.DEGRADED and self.degraded

    @property
    def recovery_edge(self) -> bool:
        """True on the single reading that leaves DEGRADED mode."""

        return self.mode_before == Mode.DEGRADED and not self.degraded

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "raw": self.raw,
            "verdict": self.verdict,
            "mode": self.mode_after,
            "accepted": self.accepted,
            "working_flow": self.working_flow,
            "baseline": self.baseline,
            "last_good": self.last_good,
            "invalid_streak": self.invalid_streak,
            "suspect_streak": self.suspect_streak,
            "suspect_direction": self.suspect_direction,
            "switching": self.switching,
            "group": self.group_id,
            "spike_policy": self.spike_policy,
            "reason": self.reason,
        }


@dataclass
class _State:
    mode: str = Mode.NORMAL
    baseline: float = 0.0
    last_good: float = 0.0
    invalid_streak: int = 0
    suspect_streak: int = 0
    suspect_direction: str = ""
    recover_streak: int = 0
    switch_remaining: int = 0
    active_group: str = ""
    raw: float = 0.0
    sequence: int = 0


class QualityGate:
    """Applies :mod:`intake.quality` rules with persisted state."""

    def __init__(self, store: Store, groups: PumpGroups | None = None) -> None:
        self._store = store
        self._groups = groups or PumpGroups(store)
        self._state = self._load()

    # -- public API --------------------------------------------------------

    def observe(self, raw: float) -> QualityResult:
        """Judge one reading and persist the new gate state."""

        state = self._state
        mode_before = state.mode
        state.sequence += 1
        state.raw = float(raw)
        self._track_switch()
        group = self._groups.active()
        switching = state.switch_remaining > 0
        if switching:
            state.switch_remaining -= 1
        nominal = group.nominal_flow if group is not None else 0.0
        policy = group.spike_policy if group is not None else rules.SpikePolicy.DROP

        if not rules.is_finite_number(raw) or rules.is_implausible(raw, nominal):
            verdict, accepted, reason = self._implausible(state, raw, nominal)
        elif state.baseline <= 0:
            verdict, accepted, reason = self._warmup(state, raw)
        elif state.mode == Mode.DEGRADED:
            verdict, accepted, reason = self._in_degraded(state, raw)
        elif switching and nominal > 0 and self._near_nominal(raw, nominal):
            verdict, accepted, reason = self._settle(state, raw, group, switching)
        else:
            verdict, accepted, reason = self._live(state, raw, group, switching)

        working_flow = raw if accepted else state.last_good
        result = QualityResult(
            sequence=state.sequence,
            raw=state.raw,
            verdict=verdict,
            mode_before=mode_before,
            mode_after=state.mode,
            accepted=accepted,
            working_flow=working_flow,
            baseline=state.baseline,
            last_good=state.last_good,
            invalid_streak=state.invalid_streak,
            suspect_streak=state.suspect_streak,
            suspect_direction=state.suspect_direction,
            switching=switching,
            group_id=group.id if group is not None else "",
            spike_policy=policy,
            reason=reason,
        )
        if verdict in (Verdict.SPIKE, Verdict.SUSPECT):
            self._append_suspect(result)
        self._save()
        return result

    def note_switch(self, group_id: str, changed: bool) -> None:
        """Open the transient window after a real pump changeover."""

        if changed:
            self._state.switch_remaining = SWITCH_TRANSIENT_READINGS
            self._state.active_group = group_id
            self._state.suspect_streak = 0
            self._state.suspect_direction = ""
            self._save()

    def reset(self) -> None:
        """Return the gate to a cold start (operator reset)."""

        self._store.delete(QUALITY_KEY)
        self._store.delete(SUSPECTS_KEY)
        self._state = _State()

    def state(self) -> dict[str, object]:
        state = self._state
        group = self._groups.active()
        return {
            "mode": state.mode,
            "baseline": state.baseline,
            "last_good": state.last_good,
            "raw": state.raw,
            "invalid_streak": state.invalid_streak,
            "suspect_streak": state.suspect_streak,
            "suspect_direction": state.suspect_direction,
            "recover_streak": state.recover_streak,
            "switching": state.switch_remaining > 0,
            "switch_remaining": state.switch_remaining,
            "sequence": state.sequence,
            "group": state.active_group,
            "plausibility_floor": (
                rules.plausibility_floor(group.nominal_flow) if group is not None else 0.0
            ),
        }

    def suspects(self) -> list[dict[str, object]]:
        raw, present = self._store.get(SUSPECTS_KEY)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []

    def describe(self) -> str:
        state = self.state()
        return (
            f"intake quality mode={state['mode']} baseline={state['baseline']:.4f} "
            f"last_good={state['last_good']:.4f} invalid={state['invalid_streak']} "
            f"suspect={state['suspect_streak']} switching={state['switching']}"
        )

    # -- branches ----------------------------------------------------------

    def _warmup(self, state: _State, raw: float) -> tuple[str, bool, str]:
        state.baseline = raw
        state.last_good = raw
        state.invalid_streak = 0
        state.suspect_streak = 0
        state.suspect_direction = ""
        state.recover_streak = 0
        state.mode = Mode.NORMAL
        return Verdict.WARMUP, True, "first plausible reading sets the baseline"

    def _implausible(
        self, state: _State, raw: float, nominal: float
    ) -> tuple[str, bool, str]:
        state.suspect_streak = 0
        state.suspect_direction = ""
        if state.mode == Mode.DEGRADED:
            # Keep waiting for calm readings; no new fault transition.
            state.recover_streak = 0
            return (
                Verdict.INVALID,
                False,
                "invalid reading while meter is in degraded mode",
            )
        state.invalid_streak += 1
        state.recover_streak = 0
        detail = (
            "reading is not a finite number"
            if not rules.is_finite_number(raw)
            else f"reading below plausibility floor {rules.plausibility_floor(nominal):.4f}"
        )
        if state.invalid_streak >= rules.FAULT_LIMIT:
            state.mode = Mode.DEGRADED
            return (
                Verdict.METER_FAILED,
                False,
                f"{rules.FAULT_LIMIT} consecutive invalid readings: {detail}",
            )
        return Verdict.INVALID, False, f"invalid reading held ({detail})"

    def _live(
        self,
        state: _State,
        raw: float,
        group: PumpGroup | None,
        switching: bool,
    ) -> tuple[str, bool, str]:
        nominal = group.nominal_flow if group is not None else 0.0
        if not rules.is_spike_sized(raw, state.baseline, nominal, switching):
            return self._accept_level(state, raw, Verdict.ACCEPTED, "reading within tolerance")

        direction = (
            DIRECTION_LOW if raw < state.baseline else DIRECTION_HIGH
        )
        if state.suspect_streak and state.suspect_direction != direction:
            state.suspect_streak = 0
        state.suspect_direction = direction
        state.suspect_streak += 1
        state.invalid_streak = 0
        state.mode = Mode.OBSERVING

        hold_limit = (
            rules.LOW_CONFIRM_LIMIT
            if direction == DIRECTION_LOW
            else rules.SPIKE_HOLD_LIMIT
        )
        if state.suspect_streak >= hold_limit:
            verdict = (
                Verdict.SUSTAINED_LOW
                if direction == DIRECTION_LOW
                else Verdict.SUSTAINED_HIGH
            )
            return self._accept_level(
                state,
                raw,
                verdict,
                f"{direction} level held for {state.suspect_streak} readings; "
                "accepted as a real sustained change",
            )

        window = "switch transient" if switching else "steady state"
        policy = group.spike_policy if group is not None else rules.SpikePolicy.DROP
        if state.suspect_streak == 1:
            if policy == rules.SpikePolicy.DROP:
                return (
                    Verdict.SPIKE,
                    False,
                    f"single-sample {direction} jump during {window}; "
                    "dropped per pump group policy",
                )
            return (
                Verdict.SUSPECT,
                False,
                f"single-sample {direction} jump during {window}; "
                "flagged for observation per pump group policy",
            )
        return (
            Verdict.SUSPECT,
            False,
            f"{direction} jump during {window}; held for confirmation "
            f"({state.suspect_streak}/{hold_limit})",
        )

    def _settle(
        self,
        state: _State,
        raw: float,
        group: PumpGroup | None,
        switching: bool,
    ) -> tuple[str, bool, str]:
        return self._accept_level(
            state,
            raw,
            Verdict.ACCEPTED,
            "reading settled near the switched pump group nominal flow",
        )

    def _in_degraded(
        self, state: _State, raw: float
    ) -> tuple[str, bool, str]:
        # While the meter is suspect, spikes are not judged at all: only
        # calm plausible readings count towards recovery.
        state.recover_streak += 1
        if state.recover_streak < rules.RECOVER_LIMIT:
            return (
                Verdict.RECOVERING,
                False,
                f"plausible reading in degraded mode "
                f"({state.recover_streak}/{rules.RECOVER_LIMIT})",
            )
        state.mode = Mode.NORMAL
        state.invalid_streak = 0
        state.suspect_streak = 0
        state.suspect_direction = ""
        state.recover_streak = 0
        state.baseline = raw
        state.last_good = raw
        return (
            Verdict.ACCEPTED,
            True,
            f"{rules.RECOVER_LIMIT} calm readings; meter recovered, control resumed",
        )

    # -- helpers -----------------------------------------------------------

    def _accept_level(
        self, state: _State, raw: float, verdict: str, reason: str
    ) -> tuple[str, bool, str]:
        state.invalid_streak = 0
        state.suspect_streak = 0
        state.suspect_direction = ""
        state.recover_streak = 0
        state.baseline = raw
        state.last_good = raw
        state.mode = Mode.NORMAL
        return verdict, True, reason

    def _near_nominal(self, raw: float, nominal: float) -> bool:
        return rules.deviation_fraction(raw, nominal) <= rules.SPIKE_FRACTION

    def _track_switch(self) -> None:
        active = self._groups.active_id()
        if active is not None and active != self._state.active_group:
            if self._state.active_group:
                self._state.switch_remaining = SWITCH_TRANSIENT_READINGS
            self._state.active_group = active
            self._state.suspect_streak = 0
            self._state.suspect_direction = ""

    def _append_suspect(self, result: QualityResult) -> None:
        suspects = self.suspects()
        suspects.append(result.as_dict())
        self._store.put(SUSPECTS_KEY, json.dumps(suspects[-MAX_SUSPECTS:]))

    def _load(self) -> _State:
        raw, present = self._store.get(QUALITY_KEY)
        if not present:
            return _State()
        try:
            parsed = json.loads(raw)
        except ValueError:
            return _State()
        if not isinstance(parsed, dict):
            return _State()

        def number(key: str) -> float:
            try:
                return float(parsed[key])
            except (KeyError, TypeError, ValueError):
                return 0.0

        def integer(key: str) -> int:
            return int(number(key))

        return _State(
            mode=str(parsed.get("mode", Mode.NORMAL)),
            baseline=number("baseline"),
            last_good=number("last_good"),
            invalid_streak=integer("invalid_streak"),
            suspect_streak=integer("suspect_streak"),
            suspect_direction=str(parsed.get("suspect_direction", "")),
            recover_streak=integer("recover_streak"),
            switch_remaining=integer("switch_remaining"),
            active_group=str(parsed.get("active_group", "")),
            raw=number("raw"),
            sequence=integer("sequence"),
        )

    def _save(self) -> None:
        state = self._state
        self._store.put(
            QUALITY_KEY,
            json.dumps(
                {
                    "mode": state.mode,
                    "baseline": state.baseline,
                    "last_good": state.last_good,
                    "invalid_streak": state.invalid_streak,
                    "suspect_streak": state.suspect_streak,
                    "suspect_direction": state.suspect_direction,
                    "recover_streak": state.recover_streak,
                    "switch_remaining": state.switch_remaining,
                    "active_group": state.active_group,
                    "raw": state.raw,
                    "sequence": state.sequence,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
