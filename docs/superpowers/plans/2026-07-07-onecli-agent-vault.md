# OneCLI Agent Vault Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OneCLI-native credential routing for Pynchy agent containers and opt-in MCP sidecars without exposing raw secrets to containers.

**Architecture:** Add a focused OneCLI host client that talks directly to OneCLI's documented HTTP API, materializes proxy env vars, CA certificates, and credential stubs under `data/onecli/`, and returns normal `VolumeMount` objects. Existing container env-file and mount construction consume that material; MCP sidecars opt in through explicit config fields.

**Tech Stack:** Python 3.13, Pydantic settings, stdlib `urllib.request`, existing `VolumeMount` dataclass, pytest with mocked HTTP/file behavior.

---

### Task 1: Config Model

**Files:**
- Modify: `src/pynchy/config/models.py`
- Modify: `src/pynchy/config/settings.py`
- Test: `tests/test_config.py`

- [x] **Step 1: Write config parsing tests**

Add tests that construct `Settings` with `OneCliConfig()` defaults and with explicit values:

```python
from pynchy.config.models import OneCliConfig


def test_onecli_config_defaults_disabled():
    cfg = OneCliConfig()
    assert cfg.enabled is False
    assert cfg.url == "http://localhost:10254"
    assert cfg.api_key_env == "ONECLI_API_KEY"  # pragma: allowlist secret - env var name
    assert cfg.project_id_env == "ONECLI_PROJECT_ID"
    assert cfg.fail_closed is True
    assert cfg.agent_identifier_prefix == "pynchy"
```

- [x] **Step 2: Run the config test**

Run: `uv run pytest tests/test_config.py::test_onecli_config_defaults_disabled --no-cov -q`
Expected: fail because `OneCliConfig` is not defined.

- [x] **Step 3: Add the model and Settings field**

Add `OneCliConfig(_StrictModel)` with the fields in Step 1, import it in `settings.py`, and add `onecli: OneCliConfig = OneCliConfig()` to `Settings`.

- [x] **Step 4: Re-run the config test**

Run: `uv run pytest tests/test_config.py::test_onecli_config_defaults_disabled --no-cov -q`
Expected: pass.

### Task 2: OneCLI Client and Materialization

**Files:**
- Create: `src/pynchy/host/container_manager/onecli.py`
- Test: `tests/test_onecli.py`

- [x] **Step 1: Write tests for identifier normalization and disabled mode**

Use `make_settings()` with a temporary `data_dir`. Assert `normalize_agent_identifier("pynchy", "Research Group!") == "pynchy-research-group"` and `prepare_onecli_material("group") is None` when disabled.

- [x] **Step 2: Write tests for container config materialization**

Mock `urllib.request.urlopen` to return:

```json
{
  "env": {"HTTPS_PROXY": "http://proxy", "SSL_CERT_FILE": "/tmp/onecli-ca.pem"},
  "caCertificate": "-----BEGIN CERTIFICATE-----\\nCA\\n-----END CERTIFICATE-----\\n",
  "caCertificateContainerPath": "/tmp/onecli-ca.pem",
  "credentialStubs": [
    {"containerPath": "/home/agent/.codex/auth.json", "content": "{\\"token\\":\\"onecli-managed\\"}"}
  ],
  "warnings": ["connected"]
}
```

Assert the returned material contains the env vars and two read-only mounts: one CA file mount to `/tmp/onecli-ca.pem`, and one stub file mount to `/home/agent/.codex/auth.json`.

- [x] **Step 3: Implement the client**

Implement:

```python
@dataclass(frozen=True)
class OneCliMaterial:
    env_vars: dict[str, str]
    mounts: list[VolumeMount]
    warnings: list[str]
```

Implement `OneCliClient.get_container_config()`, `OneCliClient.create_agent()`, `normalize_agent_identifier()`, and `prepare_onecli_material(group_folder: str) -> OneCliMaterial | None`.

- [x] **Step 4: Run client tests**

Run: `uv run pytest tests/test_onecli.py --no-cov -q`
Expected: pass.

### Task 3: Agent Container Env and Mount Integration

**Files:**
- Modify: `src/pynchy/host/container_manager/credentials.py`
- Modify: `src/pynchy/host/container_manager/mounts.py`
- Test: `tests/test_container_runner.py`

- [x] **Step 1: Write env-file and mount tests**

Add tests that patch `prepare_onecli_material()` to return:

```python
OneCliMaterial(
    env_vars={"HTTPS_PROXY": "http://proxy", "SSL_CERT_FILE": "/tmp/onecli-ca.pem"},
    mounts=[VolumeMount("/host/ca.pem", "/tmp/onecli-ca.pem", readonly=True)],
    warnings=[],
)
```

Assert `build_volume_mounts()` includes the CA mount, writes `HTTPS_PROXY` to the env file, and omits `GH_TOKEN` even for an admin group with a configured token.

- [x] **Step 2: Change `_write_env_file`**

Add keyword-only parameters:

```python
extra_env_vars: dict[str, str] | None = None
include_gh_token: bool = True
```

Merge `extra_env_vars` into the env file and only call `_gh_token_env_var()` when `include_gh_token` is true.

- [x] **Step 3: Change `build_volume_mounts`**

Call `prepare_onecli_material(group.folder)` before `_write_env_file()`. Append returned mounts. Pass returned env vars into `_write_env_file()` and set `include_gh_token` to false when material exists.

- [x] **Step 4: Run container tests**

Run: `uv run pytest tests/test_container_runner.py::TestWriteEnvFile tests/test_container_runner.py::TestMountBuilding --no-cov -q`
Expected: pass.

### Task 4: MCP Sidecar Opt-In

**Files:**
- Modify: `src/pynchy/config/mcp.py`
- Modify: `src/pynchy/host/container_manager/mcp/lifecycle.py`
- Test: `tests/test_mcp_port_allocation.py`

- [x] **Step 1: Write MCP config and lifecycle tests**

Add a test that `McpServerConfig(type="docker", image="img", port=8000, onecli=True)` is accepted. Add a test that `build_env_args(config, extra_env={"HTTPS_PROXY": "http://proxy"})` includes `-e HTTPS_PROXY=http://proxy`.

- [x] **Step 2: Add config fields**

Add `onecli: bool = False` and `onecli_agent: str = "workspace"` to `McpServerConfig`.

- [x] **Step 3: Extend lifecycle helpers**

Let `build_env_args()` accept `extra_env: dict[str, str] | None = None`. In `ensure_docker_running()`, when `server_config.onecli` is true, call `prepare_onecli_material()` using `instance.kwargs["workspace"]` when present, otherwise `instance.server_name`, then add env args and `-v` mounts for returned material.

- [x] **Step 4: Run MCP tests**

Run: `uv run pytest tests/test_mcp_port_allocation.py --no-cov -q`
Expected: pass.

### Task 5: Docs and Examples

**Files:**
- Modify: `config-examples/config.toml.EXAMPLE`
- Modify: `docs/architecture/security.md`
- Modify: `docs/architecture/container-isolation.md`
- Modify: `docs/usage/mcp.md`

- [x] **Step 1: Update docs**

Document `[onecli]`, the no-raw-secret invariant, `onecli-managed` stubs, and `env_forward` as the native compatibility path.

- [x] **Step 2: Build docs**

Run: `uv run mkdocs build --strict`
Expected: pass.

### Task 6: Verification

**Files:**
- All files above

- [x] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/test_onecli.py tests/test_container_runner.py::TestWriteEnvFile tests/test_container_runner.py::TestMountBuilding tests/test_mcp_port_allocation.py tests/test_config.py --no-cov -q
```

Expected: pass.

- [x] **Step 2: Run lint and type gates**

Run:

```bash
uvx ruff format src/pynchy/host/container_manager/onecli.py tests/test_onecli.py
uvx ruff check src/pynchy/host/container_manager/onecli.py tests/test_onecli.py
uv run mypy src/pynchy/host/container_manager/onecli.py
```

Expected: pass.

- [x] **Step 3: Run relevant full suite**

Run: `uv run pytest --no-cov`
Expected: pass or report the first unrelated failure with evidence.
