# Authenticated webhook flows

## Goal

Allow a trusted external service to submit a bounded, typed event that starts
or advances work in one explicitly configured Pynchy workspace.

## Scope

- Provide plugin-owned route configuration that binds each route to one
  workspace and a declared event schema.
- Authenticate requests with a per-route secret reference and HMAC where the
  upstream supports signatures. Enforce body-size, rate, replay, and
  idempotency limits before any task creation.
- Persist a receipt and route event handling through Pynchy's normal queue,
  task authority, audit, and delivery paths.
- Mark webhook payloads as public-source input and prohibit routes to admin
  workspaces unless a later, explicit security design justifies one.
- Support a narrow initial action: start a configured agent task with structured
  event context. Integrate wait/resume semantics with the managed-flow model
  when that product contract exists.

## Boundary

This must not expose arbitrary prompt-over-HTTP, dynamic prompt templates that
elevate payload text to instructions, arbitrary channel delivery, or a bypass of
Pynchy approval and service-trust policy. The stale vault webhook-subscriptions
skill is reference material only; it does not describe the current runtime or
the required security boundary.
