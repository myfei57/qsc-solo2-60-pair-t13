"""Tests for the hardened intake acquisition path."""

from __future__ import annotations

import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.intake import (
    AUDIT_QUALITY,
    AUDIT_VALVE,
    Acquisition,
    IntakeValve,
    Mode,
    PumpGroups,
    QualityGate,
    Sensor,
    SpikePolicy,
    Verdict,
)
from waterplant.intake.quality import (
    FAULT_LIMIT,
    LOW_CONFIRM_LIMIT,
    RECOVER_LIMIT,
    SPIKE_HOLD_LIMIT,
    deviation_fraction,
    is_implausible,
    is_spike_sized,
    plausibility_floor,
)
from waterplant.store import Store

GROUP_A = "A"
GROUP_B = "B"
NOMINAL = 1000.0


class QualityRuleTest(unittest.TestCase):
    def test_thresholds_are_fixed(self) -> None:
        # The口径 must be explicit and stable across the service.
        self.assertEqual(FAULT_LIMIT, 3)
        self.assertEqual(LOW_CONFIRM_LIMIT, 3)
        self.assertEqual(RECOVER_LIMIT, 3)
        self.assertEqual(SPIKE_HOLD_LIMIT, 2)

    def test_implausibility_uses_pump_group_floor(self) -> None:
        self.assertTrue(is_implausible(-1.0, NOMINAL))
        self.assertTrue(is_implausible(20.0, NOMINAL))  # below 5% floor
        self.assertFalse(is_implausible(100.0, NOMINAL))
        self.assertEqual(plausibility_floor(NOMINAL), 50.0)

    def test_spike_detection_disabled_without_group(self) -> None:
        # No pump group configured: every plausible reading is accepted, so
        # legacy behaviour without pump metadata is preserved.
        self.assertFalse(is_spike_sized(300.0, 100.0, 0.0, False))

    def test_switch_window_widens_tolerance(self) -> None:
        # 45% jump is a spike in steady state but tolerated during switch.
        value, baseline = 1450.0, 1000.0
        self.assertGreater(deviation_fraction(value, baseline), 0.3)
        self.assertTrue(is_spike_sized(value, baseline, NOMINAL, False))
        self.assertFalse(is_spike_sized(value, baseline, NOMINAL, True))


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.groups = PumpGroups(self.store)
        self.gate = QualityGate(self.store, self.groups)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _configure(self, policy: str = SpikePolicy.DROP) -> None:
        self.groups.configure(GROUP_A, pumps=2, nominal_flow=NOMINAL, spike_policy=policy)
        self.groups.activate(GROUP_A)
        # Rebuild the gate so it picks up the active group pointer.
        self.gate = QualityGate(self.store, self.groups)

    def _baseline(self) -> None:
        self._configure()
        warm = self.gate.observe(NOMINAL)
        self.assertEqual(warm.verdict, Verdict.WARMUP)
        self.assertTrue(warm.accepted)

    def test_single_spike_dropped_does_not_move_baseline(self) -> None:
        self._baseline()
        result = self.gate.observe(2000.0)
        self.assertEqual(result.verdict, Verdict.SPIKE)
        self.assertFalse(result.accepted)
        self.assertEqual(result.working_flow, NOMINAL)
        self.assertEqual(self.gate.state()["baseline"], NOMINAL)

        back = self.gate.observe(1010.0)
        self.assertEqual(back.verdict, Verdict.ACCEPTED)
        self.assertTrue(back.accepted)

    def test_flag_policy_keeps_spike_under_observation(self) -> None:
        self._configure(SpikePolicy.FLAG)
        self.gate.observe(NOMINAL)
        first = self.gate.observe(500.0)
        self.assertEqual(first.verdict, Verdict.SUSPECT)
        self.assertFalse(first.accepted)
        suspects = self.gate.suspects()
        self.assertEqual(len(suspects), 1)
        self.assertEqual(suspects[0]["verdict"], Verdict.SUSPECT)

    def test_repeated_spike_adopts_new_high_level(self) -> None:
        self._baseline()
        self.gate.observe(2000.0)  # dropped spike
        second = self.gate.observe(2010.0)  # held: sustained high
        self.assertEqual(second.verdict, Verdict.SUSTAINED_HIGH)
        self.assertTrue(second.accepted)
        self.assertAlmostEqual(second.working_flow, 2010.0)
        self.assertEqual(self.gate.state()["mode"], Mode.NORMAL)

    def test_consecutive_low_readings_confirm_real_drop(self) -> None:
        self._baseline()
        readings = []
        for _ in range(LOW_CONFIRM_LIMIT):
            readings.append(self.gate.observe(600.0))
        self.assertEqual(readings[0].verdict, Verdict.SPIKE)
        self.assertFalse(readings[0].accepted)
        self.assertEqual(readings[-1].verdict, Verdict.SUSTAINED_LOW)
        self.assertTrue(readings[-1].accepted)
        self.assertAlmostEqual(self.gate.state()["baseline"], 600.0)

    def test_three_invalid_readings_fail_the_meter(self) -> None:
        self._baseline()
        first = self.gate.observe(0.0)
        second = self.gate.observe(0.0)
        third = self.gate.observe(0.0)
        self.assertEqual(first.verdict, Verdict.INVALID)
        self.assertEqual(second.verdict, Verdict.INVALID)
        self.assertEqual(third.verdict, Verdict.METER_FAILED)
        self.assertTrue(third.fault_edge)
        self.assertEqual(self.gate.state()["mode"], Mode.DEGRADED)
        # Working flow freezes at the last good reading.
        self.assertEqual(third.working_flow, NOMINAL)

    def test_degraded_recovers_after_calm_readings_and_invalid_resets(self) -> None:
        self._baseline()
        for _ in range(FAULT_LIMIT):
            self.gate.observe(0.0)
        # Two calm readings: still recovering.
        first = self.gate.observe(990.0)
        second = self.gate.observe(991.0)
        self.assertEqual(first.verdict, Verdict.RECOVERING)
        self.assertEqual(second.verdict, Verdict.RECOVERING)
        # An invalid reading interrupts the recovery wait.
        invalid = self.gate.observe(0.0)
        self.assertEqual(invalid.verdict, Verdict.INVALID)
        calm = [self.gate.observe(992.0 + i) for i in range(RECOVER_LIMIT)]
        self.assertEqual(calm[0].verdict, Verdict.RECOVERING)
        self.assertEqual(calm[-1].verdict, Verdict.ACCEPTED)
        self.assertTrue(calm[-1].recovery_edge)
        self.assertEqual(self.gate.state()["mode"], Mode.NORMAL)

    def test_invalid_in_degraded_does_not_refire_fault(self) -> None:
        self._baseline()
        for _ in range(FAULT_LIMIT):
            self.gate.observe(0.0)
        again = self.gate.observe(0.0)
        self.assertEqual(again.verdict, Verdict.INVALID)
        self.assertFalse(again.fault_edge)

    def test_switch_transient_settles_at_new_group_nominal(self) -> None:
        self._baseline()
        self.groups.configure(GROUP_B, pumps=1, nominal_flow=500.0)
        group, changed = self.groups.switch_to(GROUP_B)
        self.assertTrue(changed)
        self.gate.note_switch(GROUP_B, changed)
        result = self.gate.observe(480.0)
        self.assertEqual(result.verdict, Verdict.ACCEPTED)
        self.assertTrue(result.switching)
        self.assertAlmostEqual(result.working_flow, 480.0)

    def test_switch_is_idempotent(self) -> None:
        self._baseline()
        self.groups.switch_to(GROUP_A)
        _, changed = self.groups.switch_to(GROUP_A)
        self.assertFalse(changed)

    def test_gate_state_persists_across_instances(self) -> None:
        self._baseline()
        for _ in range(FAULT_LIMIT):
            self.gate.observe(0.0)
        reopened = QualityGate(self.store, self.groups)
        self.assertEqual(reopened.state()["mode"], Mode.DEGRADED)


class AcquisitionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(self.store)
        self.acq = Acquisition(self.store, auditor=self.auditor)
        self.acq.set_coag_ratio_provider(lambda: 1.0)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _configured(self, policy: str = SpikePolicy.DROP) -> None:
        self.acq.groups.configure(
            GROUP_A, pumps=2, nominal_flow=NOMINAL, spike_policy=policy
        )
        self.acq.switch_pumps(GROUP_A)

    def test_spike_excluded_from_repository_trend_and_dose(self) -> None:
        self._configured()
        self.acq.ingest(Sensor(flow=NOMINAL, turbidity=1.0))
        self.acq.ingest(Sensor(flow=5000.0, turbidity=1.0))
        working, _ = self.acq.repository.load_flow()
        self.assertEqual(working, NOMINAL)
        stats = self.acq.trend.stats()
        self.assertEqual(stats.samples, 1)

    def test_judgment_runs_before_valve_and_is_audited(self) -> None:
        self._configured()
        self.acq.ingest(Sensor(flow=NOMINAL, turbidity=1.0))
        self.acq.intake_valve.command(40.0)
        for _ in range(FAULT_LIMIT):
            report = self.acq.ingest(Sensor(flow=0.0, turbidity=1.0))
        self.assertTrue(report.quality.degraded)
        self.assertEqual(report.valve.action, "hold")
        self.assertAlmostEqual(report.valve.position, 40.0)
        self.assertEqual(self.acq.intake_valve.mode(), "safe_hold")
        valve_entries = self.auditor.filter(AUDIT_VALVE)
        self.assertEqual(len(valve_entries), 2)  # one move, one hold
        self.assertIn("hold", valve_entries[-1].detail)

    def test_safe_hold_refuses_movement_idempotently(self) -> None:
        self._configured()
        self.acq.ingest(Sensor(flow=NOMINAL, turbidity=1.0))
        for _ in range(FAULT_LIMIT):
            self.acq.ingest(Sensor(flow=0.0, turbidity=1.0))
        before = len(self.auditor.filter(AUDIT_VALVE))
        refused_1 = self.acq.valve.command(80.0)
        refused_2 = self.acq.valve.command(80.0)
        self.assertEqual(refused_1.action, "refused")
        self.assertEqual(refused_2.action, "refused")
        self.assertAlmostEqual(self.acq.valve.position(), 0.0)
        self.assertEqual(len(self.auditor.filter(AUDIT_VALVE)), before)

    def test_recovery_resumes_valve_once(self) -> None:
        self._configured()
        self.acq.ingest(Sensor(flow=NOMINAL, turbidity=1.0))
        for _ in range(FAULT_LIMIT):
            self.acq.ingest(Sensor(flow=0.0, turbidity=1.0))
        for i in range(RECOVER_LIMIT):
            report = self.acq.ingest(Sensor(flow=990.0 + i, turbidity=1.0))
        self.assertEqual(report.valve.action, "resume")
        self.assertEqual(self.acq.valve.mode(), "auto")
        # One hold entry and one resume entry; replays do not duplicate.
        details = [entry.detail for entry in self.auditor.filter(AUDIT_VALVE)]
        self.assertEqual(len([d for d in details if d.startswith("hold")]), 1)
        self.assertEqual(len([d for d in details if d.startswith("resume")]), 1)

    def test_recovery_readings_are_idempotent_around_edge(self) -> None:
        self._configured()
        self.acq.ingest(Sensor(flow=NOMINAL, turbidity=1.0))
        for _ in range(FAULT_LIMIT):
            self.acq.ingest(Sensor(flow=0.0, turbidity=1.0))
        edge = None
        for i in range(RECOVER_LIMIT):
            edge = self.acq.ingest(Sensor(flow=1000.0, turbidity=1.0))
        self.assertTrue(edge.quality.recovery_edge)
        # Replaying the same verdict via the guard must not double act.
        valve = IntakeValve(self.store, self.auditor)
        replay = valve.apply_verdict(edge.quality)
        self.assertEqual(replay.action, "resume")
        self.assertEqual(
            len(self.auditor.filter(AUDIT_VALVE)), 2  # one hold, one resume
        )

    def test_audit_trail_carries_judgment_details(self) -> None:
        self._configured()
        self.acq.ingest(Sensor(flow=NOMINAL, turbidity=1.0))
        self.acq.ingest(Sensor(flow=0.0, turbidity=1.0))
        quality_entries = self.auditor.filter(AUDIT_QUALITY)
        self.assertTrue(any("invalid" in entry.detail for entry in quality_entries))

    def test_dose_tracks_working_flow_while_degraded(self) -> None:
        self._configured()
        self.acq.ingest(Sensor(flow=NOMINAL, turbidity=1.0))
        for _ in range(FAULT_LIMIT):
            report = self.acq.ingest(Sensor(flow=0.0, turbidity=1.0))
        self.assertEqual(report.coag_dose, NOMINAL)


if __name__ == "__main__":
    unittest.main()
