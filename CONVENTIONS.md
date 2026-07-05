# Design Conventions

Judgment-based design principles for pynchy — not mechanically-checkable rules. A
linter can't decide whether a given `str` is "really" a domain concept or whether some
inheritance is genuinely the right call; that takes reading the code and applying taste.
This doc is where those principles live so both humans and coding agents apply them
consistently. Enforce them with judgment; the caveats matter.

---

## Composition over inheritance

Build big things out of small, focused parts plus a combiner, rather than a class
hierarchy that has to know about every variant. Adding a feature becomes writing a new
part instead of editing the core, illegal combinations become unrepresentable, and each
part is testable on its own.

**pynchy already lives this pattern — keep following it.** The plugin system is
composition: `MemoryProvider` (`plugins/memory/__init__.py`), channel/runtime/tunnel
Protocols, and `BaseFormatter` are strategy interfaces that implementations satisfy
structurally. When you add a backend, write a new part that satisfies the Protocol —
don't subclass an existing provider or add a variant flag to the core.

**Two smells this replaces:**

*Subclass explosion* — variant chains and mixins (`SmsNotifierWithRetry(SmsNotifier)`)
that couple classes across files and add MRO surprises.

*Config explosion* — a god class of boolean feature-flags, which makes illegal states
representable (`Report(show_footer=False, footer_text="Confidential")` type-checks and
silently drops the text).

**Prefer a Protocol + a combiner that injects the strategy:**

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class Transport(Protocol):
    async def deliver(self, msg: str) -> None: ...

class Notifier:                              # combiner: shared logic lives here, once
    def __init__(self, transport: Transport):
        self.transport = transport
    async def send(self, msg: str) -> None:
        await self.transport.deliver(msg)    # not: class SmsNotifier(Notifier)
```

`mypy` (and `@runtime_checkable`) catch a malformed part at the boundary. Note that
`ServiceTrustConfig` (four named trust properties in `types.py`) is the *right* way to
model a fixed set of options — named fields with cautious defaults, not a bag of
booleans that can silently disagree.

**Caveat:** inheritance is sometimes right — framework base classes, `Enum`,
`Exception`, `BaseSettings`, and genuine is-a relationships with no combinatorial
variants. Reach for composition when a hierarchy starts to multiply or a constructor
sprouts flags.

---

## Parse, don't validate

Coerce unstructured data into constrained types at the boundary of the system, so
downstream code never re-validates. Don't check a condition and then discard the proof.

pynchy's boundaries: the Pydantic `BaseSettings` config layer (`config/`), the
`from_dict` classmethods (`ContainerConfig.from_dict`, etc.), and every untyped client
(SDK responses, IPC payloads, DB rows). Wrap weak `dict`/`Any` inputs in a typed model
at the edge so the untyped shape stops at the boundary instead of leaking through.

```python
# AVOID: validate and throw the evidence away
async def process(payload: dict) -> None:
    if "group_folder" not in payload:
        raise ValueError("missing group_folder")   # checked, then discarded

# PREFER: parse into a type that carries the proof
@dataclass(frozen=True)
class InboundMessage:
    group_folder: GroupFolder    # construction IS validation; if it exists, it's valid
```

**Caveat on Pydantic field validators:** a `@field_validator` runs at runtime but does
*not* change the static type — a validator confirming a `str` is well-formed still leaves
the field typed `str`, so the checker sees no proof and downstream code can re-validate
or misuse it. To make Pydantic validation *real parsing*, give the proven value a
distinct type: `Annotated[str, AfterValidator(...)]` bound to a `NewType`, or wrap the
model's output at the boundary. A plain validator is a check-and-discard, not a parse.

---

## Semantic types for domain concepts

Give domain values a distinct type instead of a bare primitive. `NewType` is zero-cost
at runtime and catches category errors at type-check time.

pynchy threads bare `str` through most of its surface: `group_folder`, `session_id`,
`key`, `category` appear in `state/`, the orchestrator, and the plugin Protocols. These
are prime candidates — a `session_id` passed where a `group_folder` is expected is a
silent bug today, a type error with `NewType`:

```python
from typing import NewType

GroupFolder = NewType("GroupFolder", str)   # workspace identity
SessionId = NewType("SessionId", str)       # agent session handle

async def get_session(group_folder: GroupFolder) -> SessionId | None: ...
```

Apply this at boundaries first (state layer signatures, plugin Protocol methods) where a
mix-up is most costly. **Caveat:** a genuinely raw primitive (a loop counter, a buffer
size, a free-text `msg` body) doesn't need wrapping. This is about intent, not blanket
wrapping of every scalar — don't retrofit the whole codebase in one pass.

---

## Keep code and its documentation coupled

pynchy has a published docs site (`docs/`, mkdocs → pynchy.ricardodecal.com). When a
concrete value in code is *also* stated in prose (the security trust model,
`MountAllowlist` blocked patterns, config keys, the architecture page), the two drift out
of sync unless they reference each other.

```python
# NOTE: Update docs/architecture.md § Security model if you change these defaults.
class ServiceTrustConfig: ...
```

- **Introducing a documented value:** add a `NOTE:` comment at the code site naming the
  doc file and section.
- **Changing a value that has a `NOTE:`:** read the referenced doc and update it in the
  same change. Don't merge code that silently contradicts its own docs.
- **Editing docs:** if you're restating a concrete value (a list, a table, an
  allowlist), consider whether the code site deserves a `NOTE:` back-pointer.
