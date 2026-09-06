"""Pynchy — Personal AI assistant."""

# This package initializer is the only place that can install the runtime type
# hook before Pynchy submodules load.
import warnings

from beartype import BeartypeConf
from beartype.claw import beartype_this_package
from beartype.roar import (
    BeartypeClawDecorWarning,
    BeartypeDecorHintPep585DeprecationWarning,
    BeartypeWarning,
)

warnings.filterwarnings("ignore", category=BeartypeClawDecorWarning)  # noqa: RUF067
warnings.filterwarnings(  # noqa: RUF067
    "ignore",
    message=r".*typing\.MutableMapping\[.*",
    category=BeartypeDecorHintPep585DeprecationWarning,
)

beartype_this_package(  # noqa: RUF067
    conf=BeartypeConf(
        claw_is_pep526=False,
        warning_cls_on_decorator_exception=BeartypeClawDecorWarning,
        violation_type=BeartypeWarning,
    )
)
