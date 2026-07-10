"""Settings-source helpers for Pydantic configuration loading."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic.fields import FieldInfo

_HERMETIC_SETTINGS_SOURCES: ContextVar[bool] = ContextVar(
    "pynchy_hermetic_settings_sources", default=False
)


class FilteredDotenvSettingsSource(PydanticBaseSettingsSource):
    """Drop bare dotenv secrets before root schema validation runs."""

    def __init__(
        self, wrapped: PydanticBaseSettingsSource, settings_cls: type[BaseSettings]
    ) -> None:
        super().__init__(settings_cls)
        self._wrapped = wrapped

    def __call__(self) -> dict[str, Any]:
        data = self._wrapped()
        allowed = set(self.settings_cls.model_fields)
        return {key: value for key, value in data.items() if key in allowed}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        return self._wrapped.get_field_value(field, field_name)


def hermetic_settings_sources_enabled() -> bool:
    """Return whether settings validation should use only explicit init data."""
    return _HERMETIC_SETTINGS_SOURCES.get()


@contextmanager
def hermetic_settings_sources() -> Iterator[None]:
    """Temporarily disable ambient settings sources."""
    token = _HERMETIC_SETTINGS_SOURCES.set(True)
    try:
        yield
    finally:
        _HERMETIC_SETTINGS_SOURCES.reset(token)
