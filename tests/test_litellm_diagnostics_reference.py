from pathlib import Path


def test_litellm_diagnostics_do_not_execute_unsafe_spend_log_routes() -> None:
    reference = (
        Path(__file__).parents[1] / ".claude/skills/pynchy-ops/references/litellm-diagnostics.md"
    ).read_text()

    unsafe_commands = [
        line
        for line in reference.splitlines()
        if line.lstrip().startswith("curl ") and "/spend/logs" in line
    ]

    assert not unsafe_commands
    assert "exhausted the proxy container" in reference
