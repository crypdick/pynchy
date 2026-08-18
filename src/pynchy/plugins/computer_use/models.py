"""Typed configuration and request boundary for host computer use."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NewType

from pydantic import AfterValidator, BaseModel, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable

SourceGroup = NewType("SourceGroup", str)


class ComputerUseAction(StrEnum):
    """Allowlisted operations exposed through the computer-use boundary."""

    CAPTURE = "capture"
    LIST_APPS = "list_apps"
    LIST_WINDOWS = "list_windows"
    LAUNCH_APP = "launch_app"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    SET_VALUE = "set_value"
    PERFORM_ACTION = "perform_action"
    MENU_LIST = "menu_list"
    MENU_CLICK = "menu_click"
    DIALOG_LIST = "dialog_list"
    DIALOG_CLICK = "dialog_click"
    DIALOG_INPUT = "dialog_input"
    DIALOG_FILE = "dialog_file"
    DIALOG_DISMISS = "dialog_dismiss"
    CLIPBOARD_GET = "clipboard_get"
    CLIPBOARD_SET = "clipboard_set"
    CLIPBOARD_CLEAR = "clipboard_clear"
    CLIPBOARD_SAVE = "clipboard_save"
    CLIPBOARD_RESTORE = "clipboard_restore"
    SPACE_LIST = "space_list"
    SPACE_SWITCH = "space_switch"
    SPACE_MOVE_WINDOW = "space_move_window"
    WAIT = "wait"
    CHECK_PERMISSIONS = "check_permissions"


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be empty")
    return value


def _source_group(value: str) -> SourceGroup:
    parts = Path(value).parts
    if len(parts) != 1 or parts[0] in {"", ".", ".."}:
        raise ValueError("source_group must be one path component")
    return SourceGroup(value)


NonEmptyString = Annotated[str, AfterValidator(_non_empty)]
ValidatedSourceGroup = Annotated[SourceGroup, AfterValidator(_source_group)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
NonNegativeNumber = Annotated[float, Field(ge=0)]


class _StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class ComputerUseConfig(_StrictModel):
    """Explicit provider selection for the neutral computer-use tool."""

    # NOTE: Update docs/usage/host-capabilities/computer-use.md § Select a provider
    # if this changes.
    provider: NonEmptyString


class ComputerUseInput(_StrictModel):
    """Agent-controlled fields accepted by the computer-use tool."""

    action: ComputerUseAction
    app: NonEmptyString | None = None
    bundle_id: NonEmptyString | None = None
    pid: PositiveInt | None = None
    window_id: PositiveInt | None = None
    window_title: NonEmptyString | None = None
    window_index: NonNegativeInt | None = None
    snapshot_id: NonEmptyString | None = None
    element: PositiveInt | NonEmptyString | None = None
    query: NonEmptyString | None = None
    coordinate: tuple[NonNegativeInt, NonNegativeInt] | None = None
    text: str | None = None
    value: str | None = None
    keys: NonEmptyString | tuple[NonEmptyString, ...] | None = None
    accessibility_action: NonEmptyString | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: PositiveInt | None = None
    delta_y: int | None = None
    urls: tuple[NonEmptyString, ...] = ()
    menu_item: NonEmptyString | None = None
    menu_path: NonEmptyString | None = None
    button: NonEmptyString | None = None
    field: NonEmptyString | None = None
    index: NonNegativeInt | None = None
    path: NonEmptyString | None = None
    name: NonEmptyString | None = None
    select: NonEmptyString | None = None
    slot: NonEmptyString | None = None
    prefer: NonEmptyString | None = None
    space: PositiveInt | None = None
    seconds: NonNegativeNumber = 1.0
    capture_after: bool = False
    label: str | None = None
    foreground: bool = False
    clear: bool = False
    smooth: bool = False
    verify: bool = False
    include_disabled: bool = False
    detailed: bool = False
    ensure_expanded: bool = False
    force: bool = False
    follow: bool = False
    to_current: bool = False
    wait_until_ready: bool = False
    no_focus: bool = False

    @model_validator(mode="after")
    def validate_action_fields(self) -> ComputerUseInput:
        """Reject requests whose action-specific contract is incomplete."""
        validator = _ACTION_VALIDATORS.get(self.action)
        if validator is not None:
            validator(self)
        return self


class ComputerUseRequest(ComputerUseInput):
    """Host request carrying IPC attribution."""

    source_group: ValidatedSourceGroup
    type: str | None = None
    request_id: str | None = None
    # IpcRequestEnvelope.to_handler_data() adds these transport-owned fields to
    # every handler call after validating their types at the IPC boundary.
    reply_to: str | None = None
    deadline: str | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ComputerUseRequest:
        """Parse untrusted IPC input into the plugin's closed request contract."""
        return cls.model_validate(data)


_CLICK_ACTIONS = frozenset(
    {
        ComputerUseAction.CLICK,
        ComputerUseAction.DOUBLE_CLICK,
        ComputerUseAction.RIGHT_CLICK,
    }
)


def _validate_click(request: ComputerUseInput) -> None:
    if not (request.element or request.query or request.coordinate):
        raise ValueError("click actions require element, query, or coordinate")


def _validate_type(request: ComputerUseInput) -> None:
    if request.text is None:
        raise ValueError("type requires text")


def _validate_key(request: ComputerUseInput) -> None:
    if not request.keys or (
        isinstance(request.keys, str) and not any(part.strip() for part in request.keys.split("+"))
    ):
        raise ValueError("key requires keys")


def _validate_scroll(request: ComputerUseInput) -> None:
    if request.direction is None and request.delta_y is None:
        raise ValueError("scroll requires direction or delta_y")


def _validate_launch(request: ComputerUseInput) -> None:
    if not (request.app or request.bundle_id):
        raise ValueError("launch_app requires app or bundle_id")


def _validate_set_value(request: ComputerUseInput) -> None:
    _require_target_and(request, "value", request.value)


def _validate_perform_action(request: ComputerUseInput) -> None:
    _require_target_and(request, "accessibility_action", request.accessibility_action)


def _require_target_and(request: ComputerUseInput, field: str, value: object) -> None:
    if not (request.element or request.query):
        raise ValueError(f"{request.action.value} requires element or query")
    if value is None:
        raise ValueError(f"{request.action.value} requires {field}")


def _validate_menu_click(request: ComputerUseInput) -> None:
    if not (request.menu_item or request.menu_path):
        raise ValueError("menu_click requires menu_item or menu_path")


def _validate_dialog_click(request: ComputerUseInput) -> None:
    if request.button is None:
        raise ValueError("dialog_click requires button")


def _validate_dialog_input(request: ComputerUseInput) -> None:
    if request.text is None:
        raise ValueError("dialog_input requires text")


def _validate_dialog_file(request: ComputerUseInput) -> None:
    if request.path is None:
        raise ValueError("dialog_file requires path")


def _validate_clipboard_set(request: ComputerUseInput) -> None:
    if request.text is None:
        raise ValueError("clipboard_set requires text")


def _validate_space_switch(request: ComputerUseInput) -> None:
    if request.space is None:
        raise ValueError("space_switch requires space")


def _validate_space_move(request: ComputerUseInput) -> None:
    if not (request.space or request.to_current):
        raise ValueError("space_move_window requires space or to_current=true")


_ACTION_VALIDATORS: dict[ComputerUseAction, Callable[[ComputerUseInput], None]] = {
    **dict.fromkeys(_CLICK_ACTIONS, _validate_click),
    ComputerUseAction.TYPE: _validate_type,
    ComputerUseAction.KEY: _validate_key,
    ComputerUseAction.SCROLL: _validate_scroll,
    ComputerUseAction.LAUNCH_APP: _validate_launch,
    ComputerUseAction.SET_VALUE: _validate_set_value,
    ComputerUseAction.PERFORM_ACTION: _validate_perform_action,
    ComputerUseAction.MENU_CLICK: _validate_menu_click,
    ComputerUseAction.DIALOG_CLICK: _validate_dialog_click,
    ComputerUseAction.DIALOG_INPUT: _validate_dialog_input,
    ComputerUseAction.DIALOG_FILE: _validate_dialog_file,
    ComputerUseAction.CLIPBOARD_SET: _validate_clipboard_set,
    ComputerUseAction.SPACE_SWITCH: _validate_space_switch,
    ComputerUseAction.SPACE_MOVE_WINDOW: _validate_space_move,
}
