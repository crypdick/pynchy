"""Pynchy — Personal Claude assistant."""

# ---------------------------------------------------------------------------
# Runtime type checking via beartype — CURRENTLY DISABLED, DO NOT RE-ENABLE
# without reading this note in full.
#
# beartype_this_package() was wired in and configured with
# violation_type=UserWarning so that *type-mismatch* violations degrade to
# warnings instead of crashing the running service. That covers one failure
# mode but not another: at least 39 modules in this package import types
# only under `if TYPE_CHECKING:` (to dodge circular imports) while also
# using `from __future__ import annotations`. Combined, those annotations
# become unresolvable string forward-references at runtime. The first real
# call through one of those signatures raises
# beartype.roar.BeartypeCallHintForwardRefException — a sibling of
# BeartypeCallHintViolation, not a subclass, so violation_type does NOT
# catch it. Running the full test suite (not just `import pynchy.*`) surfaced
# 16+ failures from this, several of them hard crashes.
#
# Before re-enabling whole-package instrumentation:
#   1. Audit the `if TYPE_CHECKING:` imports across the package (the real fix
#      is almost certainly to break the circular-import dependencies those
#      blocks are dodging, not to keep papering over them — TYPE_CHECKING-only
#      imports for types that are also used in *runtime-checked* signatures
#      is a design smell worth fixing on its own merits, independent of
#      beartype).
#   2. Re-verify against the full test suite, not just an import smoke test —
#      that's what missed this the first time.
#
# import warnings
#
# from beartype import BeartypeConf
# from beartype.claw import beartype_this_package
# from beartype.roar import BeartypeClawDecorWarning
#
# warnings.filterwarnings("ignore", category=BeartypeClawDecorWarning)
#
# beartype_this_package(
#     conf=BeartypeConf(
#         claw_is_pep526=False,
#         warning_cls_on_decorator_exception=BeartypeClawDecorWarning,
#         violation_type=UserWarning,
#     )
# )
# ---------------------------------------------------------------------------
