"""Human readable component summaries."""

from __future__ import annotations

from waterplant.ns import treatment_line
from waterplant.store import describe_store

from .runtime import Runtime


def _describe_quality(rt: Runtime) -> str:
    quality = rt.acquisition.quality()
    return (
        f"intake quality={quality.quality.value} baseline={quality.baseline:.4f} "
        f"fault={quality.meter_fault} pump_group={quality.pump_group}"
    )


def collect(rt: Runtime) -> dict[str, str]:
    """Return one description line per component."""

    return {
        "pipeline": treatment_line().describe(),
        "store": describe_store(rt.store),
        "intake": rt.flow_repository.describe(),
        "intake_quality": _describe_quality(rt),
        "intake_valve": rt.intake_valve.describe(),
        "coag": rt.coag_doser.describe(),
        "chlor": rt.chlor_doser.describe(),
        "filter": rt.bank.describe(),
        "backwash": rt.backwash.describe(),
        "turbidity": rt.sampler.describe(),
        "flow": rt.calibration.describe(),
        "clearwell": rt.well.describe(),
        "quota": rt.accumulator.describe(),
        "audit": rt.auditor.describe(),
        "ph": rt.stabilizer.describe(),
        "schedule": rt.scheduler.describe(rt.bank),
        "trend": rt.trend.describe(),
        "inventory": rt.inventory.describe(),
    }
