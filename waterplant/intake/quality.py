"""Fixed intake reading quality rules.

Every threshold used to judge raw flow readings lives here so that the
quality gate, the console and the reports all apply the same口径.
"""

from __future__ import annotations

import math

# --- Hard instrument limits (mirror validate_flow) ------------------------
MIN_FLOW = 0.0
MAX_FLOW = 1_000_000.0

# --- Fault / fall-off rules ------------------------------------------------
# Consecutive implausible readings after which the meter is declared failed.
# "Implausible" means out of instrument range or below the plausibility floor.
FAULT_LIMIT = 3
# Consecutive plausible-but-low readings required before a real flow drop is
# accepted as genuine rather than a failing meter.
LOW_CONFIRM_LIMIT = 3
# Fraction of the pump group nominal flow below which a reading counts as a
# hard implausibility rather than a real production drop.
PLAUSIBILITY_FRACTION = 0.05

# --- Spike rules -----------------------------------------------------------
# Relative deviation from the accepted baseline that marks a reading as a
# short term spike (the pump switching bumps described by operations).
SPIKE_FRACTION = 0.3
# Wider deviation tolerated while the pump group configuration is changing.
SWITCH_SPIKE_FRACTION = 0.6
# Number of consecutive spike-sized readings after which a new level is
# accepted as a sustained change instead of a spike.
SPIKE_HOLD_LIMIT = 2

# --- Recovery --------------------------------------------------------------
# Consecutive plausible readings needed in DEGRADED mode before normal
# control resumes.
RECOVER_LIMIT = 3


class Verdict:
    """Classification labels attached to every ingested reading."""

    WARMUP = "warmup"
    ACCEPTED = "accepted"
    SPIKE = "spike"
    SUSPECT = "suspect"
    INVALID = "invalid"
    SUSTAINED_HIGH = "sustained_high"
    SUSTAINED_LOW = "sustained_low"
    METER_FAILED = "meter_failed"
    RECOVERING = "recovering"


class Mode:
    """Quality gate operating mode."""

    NORMAL = "normal"
    OBSERVING = "observing"
    DEGRADED = "degraded"


class SpikePolicy:
    """How a pump group wants short term spikes handled."""

    DROP = "drop"
    FLAG = "flag"


def is_finite_number(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def hard_invalid(value: float) -> bool:
    """True for readings the instrument could never produce."""

    return value < MIN_FLOW or value > MAX_FLOW


def plausibility_floor(nominal_flow: float) -> float:
    """Minimum flow that can still be a real production level."""

    if nominal_flow <= 0:
        return 0.0
    return nominal_flow * PLAUSIBILITY_FRACTION


def is_implausible(value: float, nominal_flow: float) -> bool:
    """Hard range violation or below the floor for the running group."""

    if hard_invalid(value):
        return True
    floor = plausibility_floor(nominal_flow)
    return floor > 0 and value < floor


def deviation_fraction(value: float, baseline: float) -> float:
    """Absolute relative distance from the accepted baseline."""

    if baseline <= 0:
        return 0.0
    return abs(value - baseline) / baseline


def is_spike_sized(
    value: float, baseline: float, nominal_flow: float, switching: bool
) -> bool:
    """Whether a single reading looks like a short term jump.

    Spike filtering only exists to suppress pump changeover transients, so
    it stays disabled until a pump group with a nominal flow is configured;
    without a reference group every plausible reading is accepted.
    """

    if nominal_flow <= 0 or baseline <= 0 or value <= 0:
        return False
    if is_implausible(value, nominal_flow):
        return False
    tolerance = SWITCH_SPIKE_FRACTION if switching else SPIKE_FRACTION
    return deviation_fraction(value, baseline) > tolerance
