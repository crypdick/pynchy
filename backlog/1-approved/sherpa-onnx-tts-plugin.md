# Sherpa ONNX text-to-speech plugin

## Goal

Add opt-in, local Sherpa ONNX speech synthesis and the channel-facing media
delivery path needed to send generated audio to users.

## Scope

- Define a plugin-owned voice-model configuration and host-side synthesis
  service that turns text into a bounded audio artifact.
- Add a typed outbound-media event, artifact lifecycle, and channel adapters
  for the channels that can upload audio or voice notes.
- Preserve text fallback when a channel cannot deliver audio.
- Test synthesis with a local model and test supported channel payloads without
  exposing model files or host execution directly to agent containers.

## Boundary

This is a TTS feature, not a replacement for the existing inbound STT path.
It is incomplete unless Pynchy can safely deliver the synthesized artifact;
generating a WAV in the host alone is not useful product behavior.
