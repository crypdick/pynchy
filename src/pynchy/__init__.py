"""Pynchy — Personal Claude assistant."""

# ---------------------------------------------------------------------------
# Runtime type checking via beartype.
#
# violation_type=UserWarning makes *type-mismatch* violations degrade to
# warnings instead of crashing the running service.  Forward-reference
# resolution failures (beartype.roar.BeartypeCallHintForwardRefException) are
# a separate failure mode NOT covered by violation_type: a `TYPE_CHECKING:`-
# guarded import of a type used in a real runtime-checked signature leaves
# that name unbound at runtime, so the annotation string can never resolve.
# Every such import in this package is either a real top-level import (safe:
# `pynchy.types` has no reverse dependency on
# `host.orchestrator.messaging.formatters.base`, the only genuine import
# cycle in this codebase) or, where a dependency is genuinely optional, binds
# to a safe substitute at runtime when the dependency is absent (see
# x_integration/_actions.py and _browser.py for the playwright case).
#
# A `if TYPE_CHECKING:`-guarded import of a type used in a real
# (non-stub, actually-called) function signature requires re-running the full
# test suite with beartype active to catch — an import smoke test alone
# misses it, since forward refs resolve lazily at call time, not at import
# time.
#
import warnings

from beartype import BeartypeConf
from beartype.claw import beartype_this_package
from beartype.roar import BeartypeClawDecorWarning

warnings.filterwarnings("ignore", category=BeartypeClawDecorWarning)

beartype_this_package(
    conf=BeartypeConf(
        claw_is_pep526=False,
        warning_cls_on_decorator_exception=BeartypeClawDecorWarning,
        violation_type=UserWarning,
    )
)
# ---------------------------------------------------------------------------
