"""Domain errors for Linear workspace board operations."""

from pynchy.plugins.integrations.linear_board_payloads import LinearBoardPayloadError


class LinearBoardError(LinearBoardPayloadError):
    """Raised when Linear board reconciliation cannot continue."""
