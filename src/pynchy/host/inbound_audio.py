"""Shared inbound audio transcription for channel adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pynchy.host.audio import (
    MAX_AUDIO_BYTES,
    AudioTranscriptionResult,
    is_supported_audio_filename,
    transcribe_audio_file,
)


@dataclass(frozen=True)
class InboundAudioAttachment:
    """Channel-neutral audio payload prepared by an inbound channel adapter."""

    id: str
    filename: str
    content_type: str | None
    size: int | None
    data: bytes | None


@dataclass(frozen=True)
class AudioMetadataPatch:
    """Metadata update for the attachment at ``index`` in the request list."""

    index: int
    cached_path: str | None
    transcription: dict[str, Any]


@dataclass(frozen=True)
class InboundAudioProcessingResult:
    content: str
    metadata_patches: tuple[AudioMetadataPatch, ...] = ()


@dataclass(frozen=True)
class InboundAudioProcessingRequest:
    attachments: tuple[InboundAudioAttachment, ...]
    content: str
    fallback_content: str
    cache_dir: Path
    message_id: str


def is_audio_attachment(attachment: InboundAudioAttachment) -> bool:
    content_type = attachment.content_type
    return (
        isinstance(content_type, str) and content_type.startswith("audio/")
    ) or is_supported_audio_filename(attachment.filename)


async def process_inbound_audio_attachments(
    request: InboundAudioProcessingRequest,
) -> InboundAudioProcessingResult:
    """Cache and transcribe inbound audio attachments."""
    notes: list[str] = []
    patches: list[AudioMetadataPatch] = []
    for index, attachment in enumerate(request.attachments):
        if not is_audio_attachment(attachment):
            continue
        if isinstance(attachment.size, int) and attachment.size > MAX_AUDIO_BYTES:
            result = AudioTranscriptionResult(
                success=False,
                error=(f"Audio file is too large: {attachment.size} bytes (max {MAX_AUDIO_BYTES})"),
            )
            patches.append(
                AudioMetadataPatch(
                    index=index,
                    cached_path=None,
                    transcription=_transcription_metadata(result),
                )
            )
            notes.append(_transcription_note(result))
            continue
        if attachment.data is None:
            continue

        path = _audio_cache_path(request.cache_dir, request.message_id, attachment)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(attachment.data)
        result = await transcribe_audio_file(path)
        patches.append(
            AudioMetadataPatch(
                index=index,
                cached_path=str(path),
                transcription=_transcription_metadata(result),
            )
        )
        notes.append(_transcription_note(result))

    if not notes:
        return InboundAudioProcessingResult(
            content=request.content,
            metadata_patches=tuple(patches),
        )
    return InboundAudioProcessingResult(
        content=_content_with_transcription_notes(
            content=request.content,
            fallback_content=request.fallback_content,
            notes=notes,
        ),
        metadata_patches=tuple(patches),
    )


def _audio_cache_path(
    cache_dir: Path,
    message_id: str,
    attachment: InboundAudioAttachment,
) -> Path:
    suffix = Path(attachment.filename).suffix.lower()
    return cache_dir / f"{_safe_cache_token(message_id)}-{_safe_cache_token(attachment.id)}{suffix}"


def _safe_cache_token(value: object) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value))
    return token or "unknown"


def _transcription_metadata(result: object) -> dict[str, Any]:
    metadata: dict[str, Any] = {"success": bool(getattr(result, "success", False))}
    provider = getattr(result, "provider", None)
    if provider:
        metadata["provider"] = provider
    model = getattr(result, "model", None)
    if model:
        metadata["model"] = model
    error = getattr(result, "error", None)
    if error:
        metadata["error"] = error
    return metadata


def _transcription_note(result: object) -> str:
    transcript = str(getattr(result, "transcript", "") or "").strip()
    if getattr(result, "success", False) and transcript:
        return f'[The user sent a voice message~ Here\'s what they said: "{transcript}"]'

    error = str(getattr(result, "error", "") or "unknown error")
    if "No STT provider available" in error:
        return (
            "[The user sent a voice message but I can't listen to it right now - "
            "no STT provider is configured.]"
        )
    return f"[The user sent a voice message but I had trouble transcribing it~ ({error})]"


def _content_with_transcription_notes(
    *,
    content: str,
    fallback_content: str,
    notes: list[str],
) -> str:
    prefix = "\n\n".join(notes)
    if content and content == fallback_content:
        return prefix
    if content:
        return f"{prefix}\n\n{content}"
    return prefix
