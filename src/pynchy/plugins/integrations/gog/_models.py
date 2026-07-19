"""Strict request models for Pynchy's reviewed Gog operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_SEARCH_RESULTS = 100
_MAX_TEXT_CHARS = 100_000
type CellValue = str | int | float | bool | None


class StrictModel(BaseModel):
    """Reject unknown user-supplied fields at the host boundary."""

    model_config = ConfigDict(extra="forbid")


class SearchArguments(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=20, ge=1, le=_MAX_SEARCH_RESULTS)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return single_line(value, "query")


class MessageArguments(StrictModel):
    message_id: str = Field(min_length=1, max_length=1_000)

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        return single_line(value, "message_id")


class MailArguments(StrictModel):
    to: list[str] = Field(min_length=1, max_length=100)
    cc: list[str] = Field(default_factory=list, max_length=100)
    bcc: list[str] = Field(default_factory=list, max_length=100)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=_MAX_TEXT_CHARS)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def validate_recipients(cls, value: list[str]) -> list[str]:
        return [mail_address(address) for address in value]

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        return single_line(value, "subject")

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be empty")
        return value


class DraftArguments(StrictModel):
    draft_id: str = Field(min_length=1, max_length=1_000)

    @field_validator("draft_id")
    @classmethod
    def validate_draft_id(cls, value: str) -> str:
        return single_line(value, "draft_id")


class DocumentArguments(StrictModel):
    document_id: str = Field(min_length=1, max_length=1_000)
    tab: str | None = Field(default=None, max_length=500)

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return single_line(value, "document_id")

    @field_validator("tab")
    @classmethod
    def validate_tab(cls, value: str | None) -> str | None:
        return single_line(value, "tab") if value is not None else None


class DocumentExportArguments(StrictModel):
    document_id: str = Field(min_length=1, max_length=1_000)
    format: Literal["txt", "md", "html"] = "txt"

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return single_line(value, "document_id")


class SheetArguments(StrictModel):
    spreadsheet_id: str = Field(min_length=1, max_length=1_000)
    range: str = Field(min_length=1, max_length=1_000)

    @field_validator("spreadsheet_id", "range")
    @classmethod
    def validate_sheet_reference(cls, value: str) -> str:
        return single_line(value, "spreadsheet_id or range")


class SheetUpdateArguments(SheetArguments):
    values: list[list[CellValue]] = Field(min_length=1, max_length=10_000)
    input_mode: Literal["RAW", "USER_ENTERED"] = "RAW"

    @field_validator("values")
    @classmethod
    def validate_values(cls, value: list[list[CellValue]]) -> list[list[CellValue]]:
        if any(not row for row in value):
            raise ValueError("values must not contain empty rows")
        return value


class OAuthRedirectArguments(StrictModel):
    redirect_url: str = Field(min_length=1, max_length=8_000)

    @field_validator("redirect_url")
    @classmethod
    def validate_redirect_url(cls, value: str) -> str:
        normalized = single_line(value, "redirect_url")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("redirect_url must be an HTTP(S) URL returned by Google")
        return normalized


def request_arguments[T: StrictModel](model: type[T], data: dict[str, object]) -> T:
    """Discard IPC metadata before strict parsing of the tool's public arguments."""
    public_arguments = {key: value for key, value in data.items() if key in model.model_fields}
    return model.model_validate(public_arguments)


def single_line(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError(f"{name} must be a single non-empty line")
    return normalized


def mail_address(value: str) -> str:
    normalized = single_line(value, "recipient")
    if "," in normalized or "@" not in normalized:
        raise ValueError("each recipient must be one email address")
    return normalized
