<!-- Agent instruction: Keep this document current-state-only. Record implementation history and completed work in Git or Linear. -->

# Service Boundaries

Pynchy stays a modular monolith. This page explains when an internal dependency
needs a contract and how the repository prevents accidental coupling. It does
not require a port for every direct import or a zero-entry architecture
baseline.

## When to introduce a boundary

Use a direct import inside a cohesive subsystem when one module clearly owns
the behavior, the dependency follows that ownership, and the import does not
cross a protected runtime or trust boundary.

Reusable implementation can live in an inward shared package when the behavior
has one stable meaning and multiple packages genuinely use it. Give that
package an explicit role and public surface. Do not hide adapter-to-adapter
coupling behind a generic helpers package.

Introduce a contract when it does at least one concrete job:

- keeps the host and agent runner separated by their serialized wire protocol;
- separates the plugin API from a plugin implementation;
- prevents application policy from depending on an infrastructure choice;
- preserves security, identity, or lifecycle authority at its owner; or
- supports multiple implementations that already exist.

Do not add a single-use `Protocol`, forwarding method, callback, or dependency
factory only to remove an architecture-baseline entry. Prefer, in order:

1. reuse an existing contract;
2. move behavior to the module that owns it;
3. pass the resolved value or concrete dependency directly;
4. combine modules split at the wrong seam; or
5. add the smallest port that represents a real boundary.

Prefer ordinary function or constructor injection for resolved configuration
and external collaborators. Inject the concrete dependency unless the caller
needs a stable abstraction; injection alone does not require a `Protocol` or
container.

Use a composition root to select or own the lifecycle of an external
implementation. Do not turn it into a proxy for ordinary calls between
modules. This work does not call for network services, a generic
dependency-injection framework, or generic CRUD repositories.

## Boundaries and invariants to preserve

- The agent runner remains a separate runtime reached through typed,
  serialized input and output.
- Provider adapters establish authenticated provenance before application code
  makes security or lifecycle decisions.
- Plugin contracts remain independent from built-in plugin implementations.
- One stable runtime and queue own each routed conversation.
- Turn, cursor, and routed-delivery completion remain one semantic SQLite
  transaction.
- Every provider stream passes through the shared terminal-event validator.

See [message routing](message-routing.md), [routed
conversations](conversation-routing.md), and [security](security.md) for the
runtime behavior behind these invariants.

## Dependency enforcement

`architecture.toml` defines roles, owned package roots, and exact public
modules. Every cross-package import must pass two independent checks:

1. **Visibility:** The imported module appears in the target package's
   `public_modules` allowlist.
2. **Direction:** The importer's role lists the target role in
   `allowed_dependencies`.

Imports within one owned package remain implementation details. Packages with
the same role do not receive automatic permission to depend on each other.
Named composition-root modules may import private implementations to construct
and wire them.

Prefer one package-local `api.py` when a package needs a façade. The façade
provides encapsulation, not dependency inversion: use an application-owned port
when lifecycle, transaction, trust, or implementation ownership requires the
dependency to point inward. The built-in SQLite package exposes
`pynchy.state.api` as its public surface.

`package_families` removes repeated declarations for deliberately extensible
namespaces. A family pattern ends in one-level `.*`; for example,
`pynchy.plugins.channels.*` creates one owned package for each direct channel
plugin and exposes only its `{root}.api`. Nested helper packages remain
implementation details of that plugin. Recursive family patterns are invalid.

`architecture-baseline.toml` records exact current exceptions separately for
visibility and direction. The blocking `check-architecture-boundaries` prek and
CI steps reject a new exception, a larger exception, or a stale baseline entry.
Do not regenerate the baseline as a fix.

Treat the baseline as an exception inventory, not an automatically authorized
refactoring queue. Zero entries is not a completion criterion. When a touched
dependency needs review, choose the smallest accurate outcome:

1. remove or move a dependency that crosses real ownership;
2. reuse or introduce a contract for a real boundary;
3. reclassify a package whose policy role is wrong; or
4. allow a stable monolith dependency in the policy and remove its baseline
   entry.

Review policy changes as architecture decisions. Do not weaken the policy for
one expedient import, but do not add indirection merely to keep the graph
theoretically pure. Metrics and generated analysis do not decide architecture.

## Current priority

`NewMessage.metadata` still carries authority for routed turn identity,
authenticated external-route status, public-source provenance, provider
identity, and conversation claims. Move those fields into an `InboundEnvelope`
with semantic identity types at the provider boundary. Keep metadata for
optional presentation details such as attachments and reply rendering.

Narrow broad dependency protocols only when a concrete change benefits from a
smaller capability. Do not run a package-by-package baseline burn-down
campaign. Track each justified refactoring slice in Linear and remove completed
work from this page.
