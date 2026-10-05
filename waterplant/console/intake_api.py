"""Request field conversion shared by console handlers and cycles."""

from __future__ import annotations

from waterplant.intake import PumpContext

from .http import Request


def pump_context(request: Request) -> PumpContext:
    if "expected_flow" in request.payload:
        expected_flow: float | None = request.float_field("expected_flow")
    else:
        expected_flow = None
    return PumpContext.from_values(
        group=request.str_field("pump_group", "default"),
        switching=request.bool_field("pump_switching", False),
        expected_flow=expected_flow,
    )
