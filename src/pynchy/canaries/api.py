"""Curated canary-runner API."""

from pynchy.canaries import canary_run_to_dict, get_canary_report, run_declared_canaries
from pynchy.canaries._runner import CanaryRuntime, configure_canary_runtime

__all__ = [
    "CanaryRuntime",
    "canary_run_to_dict",
    "configure_canary_runtime",
    "get_canary_report",
    "run_declared_canaries",
]
