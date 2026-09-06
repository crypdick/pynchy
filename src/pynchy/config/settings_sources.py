"""Settings-source helpers for Pydantic configuration loading."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from pynchy.config.personalization import load_layered_settings_mapping

if TYPE_CHECKING:
    from collections.abc import Iterator


_HERMETIC_SETTINGS_SOURCES: ContextVar[bool] = ContextVar(
    "pynchy_hermetic_settings_sources", default=False
)
_REPOSITORY_SETTINGS_SOURCES: ContextVar[bool] = ContextVar(
    "pynchy_repository_settings_sources", default=True
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


class PersonalizationSettingsSource(PydanticBaseSettingsSource):
    """Read bundled defaults and the conventional personalization checkout."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] | None = None

    def __call__(self) -> dict[str, Any]:
        if self._data is None:
            self._data = load_layered_settings_mapping(Path.cwd())
        return self._data

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        del field
        value = self().get(field_name)
        return value, field_name, False


def hermetic_settings_sources_enabled() -> bool:
    """Return whether settings validation should use only explicit init data."""
    return _HERMETIC_SETTINGS_SOURCES.get()


def repository_settings_sources_enabled() -> bool:
    """Return whether repository-local files should contribute settings."""
    return _REPOSITORY_SETTINGS_SOURCES.get()


@contextmanager
def hermetic_settings_sources() -> Iterator[None]:
    """Temporarily disable ambient settings sources."""
    token = _HERMETIC_SETTINGS_SOURCES.set(True)
    try:
        yield
    finally:
        _HERMETIC_SETTINGS_SOURCES.reset(token)


@contextmanager
def repository_settings_sources(*, enabled: bool) -> Iterator[None]:
    """Temporarily include or exclude repository-local settings files."""
    token = _REPOSITORY_SETTINGS_SOURCES.set(enabled)
    try:
        yield
    finally:
        _REPOSITORY_SETTINGS_SOURCES.reset(token)
