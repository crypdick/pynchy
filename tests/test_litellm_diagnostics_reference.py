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


def test_shell_recipes_exclude_unsafe_health_route() -> None:
    document = REFERENCE_PATH.read_text(encoding="utf-8")
    unsafe_blocks = [
        match.group("body")
        for match in SHELL_FENCE.finditer(document)
        if "curl" in match.group("body")
        and re.search(
            r"http://localhost:4000/health(?!/readiness(?:[\\\"'\s]|$))",
            match.group("body"),
        )
    ]

    assert not unsafe_blocks, "Shell recipes must not call /health."


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


def test_optional_canary_uses_bounded_typed_input_without_retries() -> None:
    document = REFERENCE_PATH.read_text(encoding="utf-8")
    canary_blocks = [
        match.group("body")
        for match in SHELL_FENCE.finditer(document)
        if "/v1/responses" in match.group("body")
    ]

    assert len(canary_blocks) == 1
    canary = canary_blocks[0]
    assert "Reply with OK." not in canary
    assert (
        '\\"input\\":[{\\"role\\":\\"user\\",\\"content\\":[{\\"type\\":\\"input_text\\",'
        '\\"text\\":\\".\\"}]}]'
    ) in canary
    assert '\\"stream\\":true' in canary
    assert '\\"max_output_tokens\\":1' in canary
    assert 'status == "200" && terminal_done' in canary
    assert 'terminal_done = ($0 == "data: [DONE]")' in canary
    assert "mktemp" not in canary
    assert "CANARY_BODY" not in canary
    assert "--output" not in canary
    assert "retry" not in canary.lower()
