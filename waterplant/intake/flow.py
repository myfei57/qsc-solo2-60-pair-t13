"""Persisted intake flow and turbidity readings.

The repository keeps two flow values:

* ``raw``     - the last value exactly as the instrument reported it
* ``working`` - the last value the quality gate accepted for calculation

Downstream consumers (dosing, trend, valve) must read ``working``; the raw
value is kept for the console and for forensic comparison.
"""

from __future__ import annotations

from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .report import FlowState
from .sensor import Sensor

FLOW_KEY = "intake:flow"
RAW_FLOW_KEY = "intake:flow:raw"
TURBIDITY_KEY = "intake:turbidity"


class FlowRepository:
    """Stores the latest flow reading and the turbidity that came with it."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def persist_flow(self, value: float) -> None:
        """Write the accepted working flow before downstream consumes it."""

        save_float(self._store, FLOW_KEY, value)

    def persist_raw_flow(self, value: float) -> None:
        """Record the raw instrument value alongside the working flow."""

        save_float(self._store, RAW_FLOW_KEY, value)

    def load_flow(self) -> tuple[float, bool]:
        return load_float(self._store, FLOW_KEY)

    def load_raw_flow(self) -> tuple[float, bool]:
        return load_float(self._store, RAW_FLOW_KEY)

    def record(self, sensor: Sensor) -> None:
        """Persist a full sensor reading atomically enough for the console."""

        self.persist_flow(sensor.flow)
        self.persist_raw_flow(sensor.flow)
        save_float(self._store, TURBIDITY_KEY, sensor.turbidity)

    def record_judged(
        self, raw: float, working: float, turbidity: float, accepted: bool
    ) -> None:
        """Persist raw and working flow; working only changes when accepted."""

        self.persist_raw_flow(raw)
        if accepted:
            self.persist_flow(working)
        save_float(self._store, TURBIDITY_KEY, turbidity)

    def turbidity(self) -> float:
        value, _ = load_float(self._store, TURBIDITY_KEY)
        return value

    def state(self) -> FlowState:
        flow, present = self.load_flow()
        raw, raw_present = self.load_raw_flow()
        return FlowState(
            flow=flow,
            raw=raw if raw_present else flow,
            turbidity=self.turbidity(),
            present=present,
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"intake flow={state.flow:.4f} raw={state.raw:.4f} "
            f"turbidity={state.turbidity:.4f} present={state.present}"
        )
