import re
from pathlib import Path

REFERENCE_PATH = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "pynchy-ops"
    / "references"
    / "litellm-diagnostics.md"
)
SHELL_FENCE = re.compile(r"```(?:bash|sh|shell)\n(?P<body>.*?)```", re.DOTALL)


def test_shell_recipes_do_not_call_quarantined_spend_logs() -> None:
    document = REFERENCE_PATH.read_text(encoding="utf-8")
    unsafe_blocks = [
        match.group("body")
        for match in SHELL_FENCE.finditer(document)
        if "curl" in match.group("body") and "/spend/logs" in match.group("body")
    ]

    assert not unsafe_blocks, "Shell recipes must not call quarantined /spend/logs endpoints."


def test_reference_declares_safe_diagnostics_contract() -> None:
    document = REFERENCE_PATH.read_text(encoding="utf-8")
    required_text = (
        "unsafe for routine live diagnostics regardless of requested limit",
        "/health/readiness",
        "/v1/responses",
        "data: [DONE]",
    )
    missing = [text for text in required_text if text not in document]

    assert not missing, f"Missing safe diagnostics guidance: {missing}"


def test_reference_records_the_spend_log_failure() -> None:
    assert "exhausted the proxy container" in REFERENCE_PATH.read_text(encoding="utf-8")
