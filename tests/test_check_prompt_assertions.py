"""Tests for the hard-coded prompt assertion prek check."""

from scripts.prek_hooks.check_prompt_assertions import find_prompt_assertions


def test_reports_literal_membership_assertions_for_prompt_values() -> None:
    source = "\n".join(
        (
            'assert "literal" in prompt',
            'assert "literal" in system_prompt',
            'assert "literal" in task.prompt',
            'assert "literal" in review_prompt',
            'assert "literal" not in prompt',
            'assert "literal" in task["prompt"]',
            'assert "literal" not in continuation["resume_prompt"]',
            'assert "literal" in recovery_prompt["content"]',
            'assert "literal" in (event.instructions or "")',
        )
    )

    assert find_prompt_assertions(source) == list(range(1, 10))


def test_allows_dynamic_or_non_prompt_assertions() -> None:
    source = "\n".join(
        (
            'expected = "literal"',
            "assert expected in prompt",
            'assert f"{expected}" in prompt',
            'assert "literal" in request_text',
            'assert "literal" in prompts[0]',
            'assert "literal" in response["content"]',
        )
    )

    assert find_prompt_assertions(source) == []
