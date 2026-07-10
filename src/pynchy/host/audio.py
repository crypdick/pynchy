"""Host-side audio transcription helpers.

Pynchy treats STT as optional host infrastructure. Inbound channel adapters can
cache audio and call this module without importing heavyweight model packages at
startup; if no provider is installed, the caller receives an explicit failure.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import shlex
import shutil
import subprocess  # noqa: S404, RUF100 - local STT providers are host tools.
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pynchy.logger import logger

SUPPORTED_AUDIO_SUFFIXES = frozenset(
    {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"}
)
MAX_AUDIO_BYTES = 25 * 1024 * 1024
DEFAULT_LOCAL_MODEL = "base"
DEFAULT_LOCAL_LANGUAGE = "en"
LOCAL_COMMAND_ENV = "PYNCHY_LOCAL_STT_COMMAND"
LOCAL_MODEL_ENV = "PYNCHY_LOCAL_STT_MODEL"
LOCAL_LANGUAGE_ENV = "PYNCHY_LOCAL_STT_LANGUAGE"


class _LocalModelCache:
    model: object | None = None
    name: str | None = None


@dataclass(frozen=True)
class AudioTranscriptionResult:
    success: bool
    transcript: str = ""
    provider: str = "none"
    model: str | None = None
    error: str | None = None


def is_supported_audio_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_AUDIO_SUFFIXES


async def transcribe_audio_file(path: Path) -> AudioTranscriptionResult:
    """Transcribe an audio file using the best available host STT provider."""
    return await asyncio.to_thread(_transcribe_audio_file_sync, path)


def _transcribe_audio_file_sync(path: Path) -> AudioTranscriptionResult:
    validation_error = _validate_audio_path(path)
    if validation_error is not None:
        return validation_error

    model = os.getenv(LOCAL_MODEL_ENV, DEFAULT_LOCAL_MODEL).strip() or DEFAULT_LOCAL_MODEL
    if _has_faster_whisper():
        return _transcribe_faster_whisper(path, model)
    if _has_local_command():
        return _transcribe_local_command(path, model)
    return AudioTranscriptionResult(
        success=False,
        error=(
            f"No STT provider available. Install faster-whisper or configure {LOCAL_COMMAND_ENV}."
        ),
    )


def _validate_audio_path(path: Path) -> AudioTranscriptionResult | None:
    if not path.exists():
        return AudioTranscriptionResult(success=False, error=f"Audio file not found: {path}")
    if not path.is_file():
        return AudioTranscriptionResult(success=False, error=f"Path is not a file: {path}")
    if path.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        return AudioTranscriptionResult(
            success=False,
            error=f"Unsupported audio format: {path.suffix}",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        return AudioTranscriptionResult(success=False, error=f"Failed to stat audio file: {exc}")
    if size > MAX_AUDIO_BYTES:
        return AudioTranscriptionResult(
            success=False,
            error=f"Audio file is too large: {size} bytes (max {MAX_AUDIO_BYTES})",
        )
    return None


def _has_faster_whisper() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _resolve_whisper_model() -> type[Any] | None:
    if not _has_faster_whisper():
        return None
    module = importlib.import_module("faster_whisper")
    return cast("type[Any]", module.WhisperModel)


def _get_faster_whisper_model(model: str) -> object:
    whisper_model = _resolve_whisper_model()
    if whisper_model is None:
        msg = "faster-whisper not installed"
        raise RuntimeError(msg)
    if _LocalModelCache.model is None or _LocalModelCache.name != model:
        logger.info("Loading faster-whisper model", model=model)
        _LocalModelCache.model = whisper_model(model, device="auto", compute_type="auto")
        _LocalModelCache.name = model
    return _LocalModelCache.model


def _transcribe_cached_model(path: Path, model: str) -> tuple[str, object]:
    local_model = _get_faster_whisper_model(model)
    language = os.getenv(LOCAL_LANGUAGE_ENV, DEFAULT_LOCAL_LANGUAGE).strip() or None
    kwargs: dict[str, object] = {"beam_size": 5}
    if language:
        kwargs["language"] = language
    segments, info = local_model.transcribe(str(path), **kwargs)
    transcript = " ".join(segment.text.strip() for segment in segments).strip()
    return transcript, info


def _transcribe_faster_whisper(path: Path, model: str) -> AudioTranscriptionResult:
    try:
        transcript, info = _transcribe_cached_model(path, model)
    except Exception as exc:  # noqa: BLE001, RUF100  # allow: exception-handling - STT provider failures must not break message ingestion.
        logger.warning("Local audio transcription failed", filename=path.name, err=str(exc))
        return AudioTranscriptionResult(
            success=False,
            provider="local",
            model=model,
            error=f"Local transcription failed: {exc}",
        )
    logger.info(
        "Audio transcribed",
        provider="local",
        model=model,
        language=getattr(info, "language", None),
        filename=path.name,
    )
    return AudioTranscriptionResult(
        success=True,
        transcript=transcript,
        provider="local",
        model=model,
    )


def _has_local_command() -> bool:
    return bool(os.getenv(LOCAL_COMMAND_ENV, "").strip()) or shutil.which("whisper") is not None


def _transcribe_local_command(path: Path, model: str) -> AudioTranscriptionResult:
    language = (
        os.getenv(LOCAL_LANGUAGE_ENV, DEFAULT_LOCAL_LANGUAGE).strip() or DEFAULT_LOCAL_LANGUAGE
    )
    command_template = os.getenv(LOCAL_COMMAND_ENV, "").strip()
    try:
        with tempfile.TemporaryDirectory(prefix="pynchy-local-stt-") as output_dir:
            command = _local_command_argv(
                path=path,
                output_dir=output_dir,
                model=model,
                language=language,
                command_template=command_template,
            )
            if command is None:
                return AudioTranscriptionResult(
                    success=False,
                    error=f"{LOCAL_COMMAND_ENV} is unset and no whisper executable was found",
                )
            _run_local_command(command)
            transcript = _read_local_command_transcript(Path(output_dir))
    except (
        KeyError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        logger.warning("Local STT command failed", filename=path.name, err=str(exc))
        return AudioTranscriptionResult(
            success=False,
            provider="local_command",
            model=model,
            error=f"Local STT command failed: {exc}",
        )
    if transcript is None:
        return AudioTranscriptionResult(
            success=False,
            provider="local_command",
            model=model,
            error="Local STT command did not produce a .txt transcript",
        )
    return AudioTranscriptionResult(
        success=True,
        transcript=transcript,
        provider="local_command",
        model=model,
    )


def _local_command_argv(
    *,
    path: Path,
    output_dir: str,
    model: str,
    language: str,
    command_template: str,
) -> list[str] | None:
    if command_template:
        return shlex.split(
            command_template.format(
                input_path=str(path),
                output_dir=output_dir,
                model=model,
                language=language,
            )
        )
    whisper_bin = shutil.which("whisper")
    if whisper_bin is None:
        return None
    return [
        whisper_bin,
        str(path),
        "--model",
        model,
        "--language",
        language,
        "--output_format",
        "txt",
        "--output_dir",
        output_dir,
    ]


def _run_local_command(command: list[str]) -> None:
    subprocess.run(  # noqa: S603, RUF100 - argv comes from explicit local STT configuration; shell is never used.
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )


def _read_local_command_transcript(output_dir: Path) -> str | None:
    txt_files = sorted(output_dir.glob("*.txt"))
    if not txt_files:
        return None
    return txt_files[0].read_text(encoding="utf-8").strip()
