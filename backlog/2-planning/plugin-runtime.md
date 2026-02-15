# Runtime Plugins

## Overview

Enable alternative container runtimes (Apple Container, Podman, etc.) to be installed as plugins. Docker remains the built-in default.

## Dependencies

- Plugin discovery system (plugin-discovery.md)

## Design

### ContainerRuntime Protocol

```python
@runtime_checkable
class ContainerRuntime(Protocol):
    """Contract that every runtime must implement."""

    name: str                      # e.g. "docker", "apple", "podman"
    cli: str                       # binary name, e.g. "docker", "container"

    def ensure_running(self) -> None:
        """Verify/start the runtime daemon."""
        ...

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        """Return names of running containers matching prefix."""
        ...

    def build_run_args(
        self, mounts: list[VolumeMount], container_name: str, image: str
    ) -> list[str]:
        """Return full CLI args for `run` command (after the binary name).

        Default OCI-compatible implementation provided by base class.
        Override only if CLI diverges from Docker.
        """
        ...
```

### RuntimePlugin Class

```python
class RuntimePlugin(PluginBase):
    """Base class for runtime plugins."""

    categories = ["runtime"]  # Fixed

    @abstractmethod
    def create_runtime(self) -> ContainerRuntime:
        """Return a ContainerRuntime instance."""
        ...

    def platform_matches(self) -> bool:
        """Return True if this runtime applies to current platform.

        Called during discovery — non-matching runtimes are skipped.
        Default: True (always available).
        """
        return True

    @property
    def priority(self) -> int:
        """Higher = preferred when multiple runtimes match.

        Docker has priority 0. Plugins default to 10.
        Only matters when CONTAINER_RUNTIME env var is not set.
        """
        return 10
```

### Runtime Selection Logic

Replaces current `detect_runtime()`:

```python
def select_runtime(registry: PluginRegistry) -> ContainerRuntime:
    """Select which runtime to use."""

    # 1. Explicit env var override
    override = os.environ.get("CONTAINER_RUNTIME", "").lower()
    if override:
        # Check plugins first
        for rp in registry.runtimes:
            rt = rp.create_runtime()
            if rt.name == override:
                return rt
        # Then built-in
        if override == "docker":
            return DockerRuntime()
        raise RuntimeError(
            f"Unknown CONTAINER_RUNTIME={override!r}. "
            f"Available: docker, {', '.join(rp.create_runtime().name for rp in registry.runtimes)}"
        )

    # 2. Auto-detect: highest-priority plugin that matches platform
    matching = [rp for rp in registry.runtimes if rp.platform_matches()]
    if matching:
        best = sorted(matching, key=lambda rp: rp.priority, reverse=True)[0]
        return best.create_runtime()

    # 3. Fallback: always Docker
    return DockerRuntime()
```

## Example: Apple Container Plugin

Extract existing Apple Container code into a plugin:

**pyproject.toml:**
```toml
[project]
name = "pynchy-plugin-apple-container"
dependencies = ["pynchy"]

[project.entry-points."pynchy.plugins"]
apple-container = "pynchy_plugin_apple_container:AppleContainerPlugin"
```

**plugin.py:**
```python
import sys
from pynchy.plugin import RuntimePlugin, ContainerRuntime
from .runtime import AppleContainerRuntime

class AppleContainerPlugin(RuntimePlugin):
    name = "apple-container"
    version = "0.1.0"
    description = "Apple Container runtime for macOS"

    def create_runtime(self) -> ContainerRuntime:
        return AppleContainerRuntime()

    def platform_matches(self) -> bool:
        return sys.platform == "darwin"

    @property
    def priority(self) -> int:
        return 20  # Prefer over Docker on macOS
```

**runtime.py:**
Contains the `_ensure_apple()`, `_list_apple()` logic currently in `src/pynchy/runtime.py`.

## Implementation Steps

1. Define `ContainerRuntime` Protocol in `plugin/runtime.py`
2. Define `RuntimePlugin` base class
3. Rewrite `src/pynchy/runtime.py`:
   - Remove Apple Container code
   - Convert to `DockerRuntime` class implementing Protocol
   - Update `detect_runtime()` to use `select_runtime()`
4. Move `_build_container_args()` logic from `container_runner.py` into `runtime.build_run_args()`
5. Simplify `container/build.sh` to Docker-only
6. Create `pynchy-plugin-apple-container` repo (separate from core)
7. Tests: runtime selection, priority ordering, platform matching

## Integration Points

- `app.py:_ensure_container_system_running()` — calls `runtime.ensure_running()`
- `container_runner.py:run_container_agent()` — uses `runtime.cli`, `runtime.build_run_args()`
- `runtime.py:get_runtime()` — singleton, accepts optional PluginRegistry

## Open Questions

- Should plugins be able to provide custom build scripts?
- How to handle runtime version compatibility checks?
- Should we support hot-swapping runtimes without restart?
- Do we need runtime-specific configuration validation?

## Verification

1. On macOS: `uv pip install pynchy-plugin-apple-container`
2. Verify auto-detection: `python -c "from pynchy.runtime import get_runtime; print(get_runtime().name)"`
   - Should print `apple` on macOS
3. Verify env override: `CONTAINER_RUNTIME=docker python -c "..."`
   - Should print `docker`
4. Uninstall and verify fallback: `uv pip uninstall pynchy-plugin-apple-container`
   - Should fall back to Docker
