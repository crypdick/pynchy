"""Small browser contracts shared by the X integration helpers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class XLocator(Protocol):
    """The locator operations used by Pynchy's X automation."""

    @property
    def first(self) -> XLocator: ...

    def locator(self, selector: str) -> XLocator: ...

    def filter(self, **kwargs: object) -> XLocator: ...

    async def wait_for(self, *, timeout: int) -> None: ...  # noqa: ASYNC109 - mirrors Playwright's locator API.

    async def click(self) -> None: ...

    async def fill(self, content: str) -> None: ...

    async def get_attribute(self, name: str) -> str | None: ...

    async def is_visible(self) -> bool: ...


@runtime_checkable
class XPage(Protocol):
    """The Playwright page operations used by Pynchy's X automation."""

    def locator(self, selector: str) -> XLocator: ...

    def get_by_role(self, role: str) -> XLocator: ...

    async def goto(self, url: str, **kwargs: object) -> object: ...

    async def wait_for_timeout(self, _milliseconds: int) -> None: ...
