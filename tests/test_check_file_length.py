from pathlib import Path

from scripts.prek_hooks.check_file_length import check_file_length


def write_python(path: Path, lines: int) -> None:
    path.write_text("\n".join(f"value_{line} = {line}" for line in range(lines)), encoding="utf-8")


def test_file_length_baseline_allows_only_current_size(tmp_path: Path) -> None:
    path = tmp_path / "oversized.py"
    write_python(path, 3)
    baseline = {path.as_posix(): 3}

    assert check_file_length(path, 2, baseline) is None

    write_python(path, 4)

    assert check_file_length(path, 2, baseline) == (
        4,
        "File grew from its 3-logical-line baseline to 4 (limit 2) — split it instead",
    )


def test_file_length_baseline_requires_removal_after_split(tmp_path: Path) -> None:
    path = tmp_path / "split.py"
    write_python(path, 2)

    assert check_file_length(path, 2, {path.as_posix(): 3}) == (
        2,
        "File is within the 2-logical-line limit; remove its baseline entry",
    )


def test_file_length_rejects_unlisted_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "new_oversized.py"
    write_python(path, 3)

    assert check_file_length(path, 2) is not None
