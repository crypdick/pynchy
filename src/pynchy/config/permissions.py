"""User-facing permission policy configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

type PermissionDecision = Literal["allow", "deny", "needs_human"]


class PermissionConfig(BaseModel):
    """Group explicit capability patterns by their operator decision."""

    model_config = {"extra": "forbid"}

    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @field_validator("allow", "ask", "deny")
    @classmethod
    def validate_patterns(cls, patterns: list[str]) -> list[str]:
        if any(not pattern.strip() for pattern in patterns):
            raise ValueError("permission patterns must not be empty")
        if len(patterns) != len(set(patterns)):
            raise ValueError("permission cannot appear more than once")
        return patterns

    @model_validator(mode="after")
    def validate_disjoint_buckets(self) -> PermissionConfig:
        patterns = [*self.allow, *self.ask, *self.deny]
        if len(patterns) != len(set(patterns)):
            raise ValueError("permission cannot appear more than once across buckets")
        return self

    @property
    def decisions(self) -> dict[str, PermissionDecision]:
        return {
            **dict.fromkeys(self.allow, "allow"),
            **dict.fromkeys(self.ask, "needs_human"),
            **dict.fromkeys(self.deny, "deny"),
        }
