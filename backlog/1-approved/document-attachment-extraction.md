# Document attachment extraction

## Goal

Let a Pynchy workspace safely reason over text and page images from supported
document attachments without requiring every agent to implement its own parser.

## Scope

- Build a shared host-side attachment normalization boundary for channel plugins
  that can download document bytes.
- Start with bounded, deterministic PDF and office-document text extraction;
  render fallback page images only when text extraction cannot represent a page.
- Preserve attachment provenance, MIME type, hash, cache path, extraction state,
  and limits in message metadata while giving the agent a bounded extracted-text
  view.
- Apply size, page-count, MIME, and timeout limits before parsing. Treat both
  source documents and extracted text as untrusted input.
- Add hermetic tests for successful extraction, malformed input, limit handling,
  and channel-independent metadata behavior.

## Boundary

This is document ingestion, not document editing, cloud-file sync, or model-based
OCR at message ingress. It must not silently send attachment bytes to a remote
model or weaken Pynchy's source-taint and workspace-isolation policies.
