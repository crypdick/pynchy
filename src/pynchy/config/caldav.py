"""CalDAV tool configuration models."""

from __future__ import annotations

from pydantic import BaseModel


class _CalDAVModel(BaseModel):
    model_config = {"extra": "forbid"}


class CalDAVServerConfig(_CalDAVModel):
    url: str
    username: str
    password_env: str | None = None
    default_calendar: str | None = None
    allow: list[str] | None = None
    ignore: list[str] | None = None


class CalDAVConfig(_CalDAVModel):
    default_server: str = ""
    servers: dict[str, CalDAVServerConfig] = {}
