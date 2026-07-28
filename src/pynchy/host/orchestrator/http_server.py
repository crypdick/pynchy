"""Embedded HTTP server for readiness, operator diagnostics, and deploys."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess  # noqa: S404, RUF100 - deploy validation uses fixed no-shell uv argv.
import time
from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves HTTP dependency annotations at runtime.
)
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from aiohttp import web

from pynchy.canaries.api import canary_run_to_dict, get_canary_report
from pynchy.conversation.api import notify_conversation_delivery_completed
from pynchy.deployments import DeployClaimStatus
from pynchy.host.git_ops.api import (
    files_changed_between,
    get_deploy_config_hash,
    get_head_commit_message,
    get_head_sha,
    is_repo_dirty,
    push_local_commits,
    run_git,
)
from pynchy.host.orchestrator.capability_status import (
    CapabilityStatusOperations,
    canary_outcomes_from_report,
    collect_capability_status,
    resolve_workspace_capabilities,
)
from pynchy.host.orchestrator.http_control import (
    ControlPlaneRuntime,
    build_control_plane_middleware,
    register_unix_socket_cleanup,
    start_control_plane_sites,
)
from pynchy.host.orchestrator.http_readiness import (
    ControlPlaneReadiness,
    readiness_key,
    readiness_middleware,
)
from pynchy.host.orchestrator.status import StatusDeps, collect_status
from pynchy.host.orchestrator.temporal.api import DeployRequest, start_deploy_workflow
from pynchy.host.orchestrator.webhook_ingress import (
    WebhookIngressDeps,
    build_webhook_ingress,
    install_webhook_ingress,
    recover_webhook_conversations,
)
from pynchy.logger import logger
from pynchy.plugins.api import WebhookRoute, collect_webhook_routes, validate_webhook_routes
from pynchy.plugins.integrations.api import work_item_execution_to_dict
from pynchy.state.api import (
    action_intent_to_dict,
    get_recent_canary_runs,
    list_action_intents,
    list_webhook_effects,
    list_work_item_executions,
    reconcile_webhook_effect_absent,
)
from pynchy.webhook_effects import WebhookEffect, WebhookEffectId, WebhookEffectStatus

if TYPE_CHECKING:
    from aiohttp.web_app import Application as AiohttpApplication
else:
    AiohttpApplication = object

_start_time = time.monotonic()
_RUNTIME_HARNESS_ENV = "PYNCHY_RUNTIME_HARNESS"
_RUNTIME_HARNESS_MESSAGE_PATH = "/__pynchy_runtime__/messages"

# Typed app key avoids aiohttp NotAppKeyWarning from plain-string lookups.
deps_key: web.AppKey[HttpDeps] = web.AppKey("deps")
status_deps_key: web.AppKey[StatusDeps] = web.AppKey("status_deps")


@dataclass(frozen=True, slots=True)
class PreparedHttpServer:
    """Prepared control plane whose listener and publication are explicit."""

    runner: web.AppRunner
    runtime: ControlPlaneRuntime
    app: web.Application
    readiness: ControlPlaneReadiness


def _write_boot_warning(data_dir: Path, message: str) -> None:
    """Append a warning to boot_warnings.json, picked up by _send_boot_notification on restart."""
    path = data_dir / "boot_warnings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        warnings = json.loads(path.read_text()) if path.exists() else []
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read boot warnings file, starting fresh", err=str(exc))
        warnings = []
    warnings.append(message)
    path.write_text(json.dumps(warnings))


@runtime_checkable
class HttpDeps(Protocol):
    """Dependencies injected by app.py."""

    capability_status_operations: CapabilityStatusOperations

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    def admin_chat_jid(self) -> str: ...


@runtime_checkable
class DeployHttpDeps(HttpDeps, Protocol):
    """Concrete filesystem paths needed only by the deploy endpoint."""

    data_dir: Path
    project_root: Path


@runtime_checkable
class RuntimeHarnessIngress(Protocol):
    """Test-only dependency that enters through the real inbound boundary."""

    async def ingest_runtime_harness_message(self, jid: str, content: str) -> None: ...


class HttpServerDeps(DeployHttpDeps, WebhookIngressDeps, Protocol):
    """Full process dependencies used while starting the HTTP server."""

    def get_plugin_manager(self) -> object: ...


async def _handle_health(request: web.Request) -> web.Response:  # noqa: RUF029, RUF100 - aiohttp route handlers are async.
    """Return a non-sensitive readiness response suitable for unauthenticated probes."""
    del request
    return web.json_response({"status": "ok"})


async def _handle_deploy(request: web.Request) -> web.Response:
    deps = cast("DeployHttpDeps", request.app[deps_key])
    old_sha = get_head_sha()

    # 1. Push any local commits before pulling (prevents divergence)
    run_git("fetch", "origin")
    if not push_local_commits(skip_fetch=True):
        logger.warning("Pre-deploy push failed, continuing with rebase")

    # 2. Stash dirty files so they don't block the rebase
    stash = run_git("stash")
    stashed = stash.returncode == 0 and "No local changes" not in stash.stdout

    # 3. Rebase to incorporate incoming remote changes
    pull = run_git("rebase", "origin/main")
    if pull.returncode != 0:
        # Abort failed rebase to leave repo clean, then continue with current code
        run_git("rebase", "--abort")
        logger.warning(
            "git rebase failed, restarting with current code", stderr=pull.stderr.strip()
        )
        _write_boot_warning(
            deps.data_dir,
            "Deploy rolled back to previous commit because incoming commits failed to rebase. "
            "Please reconcile the incoming changes into your local clone, push, then redeploy.",
        )

    # Restore stashed files regardless of rebase outcome
    if stashed:
        run_git("stash", "pop")

    new_sha = get_head_sha()
    has_new_code = new_sha != old_sha

    # 4. Validate import (only when the pull changed HEAD)
    if has_new_code:
        validate = await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", "python", "-c", "import pynchy"],
            cwd=str(deps.project_root),
            capture_output=True,
            text=True,
        )
        if validate.returncode != 0:
            err = validate.stderr.strip()[-300:]
            logger.error("Deploy validation failed, rolling back", error=err)
            run_git("reset", "--hard", old_sha)
            chat_jid = deps.admin_chat_jid()
            if chat_jid:
                msg = f"Deploy failed — import validation error, rolled back to {old_sha[:8]}."
                await deps.broadcast_host_message(chat_jid, msg)
            return web.json_response(
                {"error": "import validation failed", "rolled_back_to": old_sha},
                status=422,
            )

    # 5. Start deploy workflow. The activity owns rebuild, continuation, and restart.
    chat_jid = deps.admin_chat_jid()
    rebuild = has_new_code and files_changed_between(old_sha, new_sha, "src/pynchy/agent/")
    claim = await start_deploy_workflow(
        DeployRequest(
            chat_jid=chat_jid,
            commit_sha=new_sha,
            config_hash=get_deploy_config_hash(),
            previous_sha=old_sha,
            rebuild=rebuild,
            reason="http",
        )
    )

    restarting = claim.status is DeployClaimStatus.CLAIMED
    return web.json_response(
        {
            "status": "restarting" if restarting else claim.status.value,
            "sha": new_sha,
            "commit": get_head_commit_message(),
            "dirty": is_repo_dirty(),
            "previous_sha": old_sha,
        }
    )


async def _handle_status(request: web.Request) -> web.Response:
    """Comprehensive operational status from all subsystems."""
    status_deps: StatusDeps = request.app[status_deps_key]
    data = await collect_status(status_deps, _start_time)
    return web.json_response(data)


async def _handle_work_items(request: web.Request) -> web.Response:
    """Return bounded, read-only execution records for operator diagnosis."""
    raw_limit = request.query.get("limit", "100")
    if not raw_limit.isdecimal() or not 1 <= int(raw_limit) <= 200:
        return web.json_response({"error": "limit must be an integer from 1 to 200"}, status=400)
    workspace = request.query.get("workspace") or None
    executions = await list_work_item_executions(workspace=workspace, limit=int(raw_limit))
    return web.json_response(
        {
            "workspace": workspace,
            "work_items": [work_item_execution_to_dict(item) for item in executions],
        }
    )


async def _handle_actions(request: web.Request) -> web.Response:
    """Return bounded external-action state without exposing draft payloads."""
    raw_limit = request.query.get("limit", "100")
    if not raw_limit.isdecimal() or not 1 <= int(raw_limit) <= 200:
        return web.json_response({"error": "limit must be an integer from 1 to 200"}, status=400)
    workspace = request.query.get("workspace") or None
    actions = await list_action_intents(workspace=workspace, limit=int(raw_limit))
    return web.json_response(
        {
            "workspace": workspace,
            "actions": [action_intent_to_dict(action) for action in actions],
        }
    )


def _webhook_effect_to_dict(effect: WebhookEffect) -> dict[str, object]:
    scope = effect.scope
    return {
        "id": effect.id,
        "provider": scope.provider,
        "account": scope.account,
        "event_type": scope.event_type,
        "event_action": scope.event_action,
        "subject_id": scope.subject_id,
        "intent_fingerprint": scope.intent_fingerprint,
        "status": effect.status.value,
        "fingerprint": effect.fingerprint,
        "created_at": effect.created_at,
        "executing_at": effect.executing_at,
        "resolved_at": effect.resolved_at,
    }


async def _handle_webhook_effects(request: web.Request) -> web.Response:
    """Return bounded outbound-effect records for operator reconciliation."""
    raw_limit = request.query.get("limit", "100")
    if not raw_limit.isdecimal() or not 1 <= int(raw_limit) <= 200:
        return web.json_response({"error": "limit must be an integer from 1 to 200"}, status=400)
    raw_status = request.query.get("status", WebhookEffectStatus.OUTCOME_UNKNOWN.value)
    if raw_status == "all":
        status = None
    else:
        try:
            status = WebhookEffectStatus(raw_status)
        except ValueError:
            return web.json_response({"error": "unknown webhook effect status"}, status=400)
    effects = await list_webhook_effects(status=status, limit=int(raw_limit))
    return web.json_response(
        {"status": raw_status, "effects": [_webhook_effect_to_dict(effect) for effect in effects]}
    )


async def _handle_webhook_effect_absent(request: web.Request) -> web.Response:
    """Release one quarantine only after explicit external provider verification."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError):
        body = None
    if body != {"verified_absent": True}:
        return web.json_response(
            {"error": "verified_absent must be exactly true"},
            status=400,
        )
    try:
        resolution = await reconcile_webhook_effect_absent(
            WebhookEffectId(request.match_info["effect_id"])
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    for wakeup in resolution.wakeups:
        await notify_conversation_delivery_completed(wakeup)
    return web.json_response(
        {
            "status": WebhookEffectStatus.RECONCILED_ABSENT.value,
            "released_deliveries": len(resolution.wakeups),
        }
    )


async def _handle_capabilities(request: web.Request) -> web.Response:
    """Return effective host-action capabilities for one or every workspace."""
    deps: HttpDeps = request.app[deps_key]
    report = await get_canary_report(history_limit=10)
    workspace = request.query.get("workspace")
    if workspace:
        snapshot = await resolve_workspace_capabilities(
            workspace,
            operations=deps.capability_status_operations,
            canary_outcomes=canary_outcomes_from_report(report),
        )
        return web.json_response(snapshot.to_dict())
    return web.json_response(
        await collect_capability_status(
            report,
            operations=deps.capability_status_operations,
        )
    )


def _canary_history_limit(request: web.Request) -> int | None:
    raw_limit = request.query.get("limit", "50")
    if not raw_limit.isdecimal():
        return None
    limit = int(raw_limit)
    return limit if 1 <= limit <= 200 else None


async def _handle_canary_report(request: web.Request) -> web.Response:
    """Return current external-service canary evidence and regressions."""
    limit = _canary_history_limit(request)
    if limit is None:
        return web.json_response({"error": "limit must be an integer from 1 to 200"}, status=400)
    return web.json_response(await get_canary_report(history_limit=limit))


async def _handle_canary_runs(request: web.Request) -> web.Response:
    """Return result history for all canaries or one declared scenario."""
    limit = _canary_history_limit(request)
    if limit is None:
        return web.json_response({"error": "limit must be an integer from 1 to 200"}, status=400)
    scenario_id = request.query.get("scenario_id") or None
    runs = await get_recent_canary_runs(limit=limit, scenario_id=scenario_id)
    return web.json_response({"runs": [canary_run_to_dict(run) for run in runs]})


async def _handle_runtime_harness_message(request: web.Request) -> web.Response:
    """Drive the real channel-ingress boundary in isolated runtime tests."""
    body = await request.json()
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be an object"}, status=400)
    jid = body.get("jid")
    content = body.get("content")
    if not isinstance(jid, str) or not jid or not isinstance(content, str) or not content:
        return web.json_response({"error": "jid and content are required strings"}, status=400)
    deps = cast("RuntimeHarnessIngress", request.app[deps_key])
    await deps.ingest_runtime_harness_message(jid, content)
    return web.json_response({"status": "accepted"})


# ------------------------------------------------------------------
# Server setup
# ------------------------------------------------------------------


async def prepare_http_server(
    deps: HttpServerDeps,
    *,
    runtime: ControlPlaneRuntime,
    status_deps: StatusDeps | None = None,
    github_webhook_routes: tuple[WebhookRoute, ...] = (),
) -> PreparedHttpServer:
    """Prepare routes and cleanup hooks without opening a listener."""
    webhook_routes = validate_webhook_routes(
        (*collect_webhook_routes(cast("Any", deps.get_plugin_manager())), *github_webhook_routes)
    )
    app = create_http_app(
        deps,
        status_deps=status_deps,
        runtime=runtime,
        webhook_routes=webhook_routes,
        readiness=ControlPlaneReadiness(),
    )
    register_unix_socket_cleanup(app, runtime)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
    except BaseException:
        await runner.cleanup()
        raise
    return PreparedHttpServer(
        runner=runner,
        runtime=runtime,
        app=app,
        readiness=app[readiness_key],
    )


async def activate_http_server(prepared: PreparedHttpServer) -> web.AppRunner:
    """Bind the listener while the readiness gate still rejects requests."""
    await start_control_plane_sites(prepared.runner, prepared.runtime)
    runtime = prepared.runtime
    logger.info(
        "HTTP control plane listening behind startup gate",
        host=runtime.bind_host,
        port=runtime.port,
        unix_socket=str(runtime.unix_socket) if runtime.unix_socket else None,
        public_bind=runtime.public_bind,
        remote_deploy=runtime.allow_remote_deploy,
        remote_auth=runtime.remote_auth_required,
    )
    return prepared.runner


async def recover_http_routes(prepared: PreparedHttpServer) -> None:
    """Restore route ownership and wake durable deliveries."""
    await recover_webhook_conversations(prepared.app)


def publish_http_server(prepared: PreparedHttpServer) -> None:
    """Allow requests after all runtime owners are ready."""
    prepared.readiness.accepting_requests = True
    logger.info("HTTP control plane ready")


async def start_http_server(
    deps: HttpServerDeps,
    *,
    runtime: ControlPlaneRuntime,
    status_deps: StatusDeps | None = None,
) -> web.AppRunner:
    """Create, start, and return the HTTP server runner."""
    prepared = await prepare_http_server(deps, runtime=runtime, status_deps=status_deps)
    try:
        await activate_http_server(prepared)
        await recover_http_routes(prepared)
    except BaseException:
        await prepared.runner.cleanup()
        raise
    publish_http_server(prepared)
    return prepared.runner


def create_http_app(
    deps: HttpDeps,
    *,
    status_deps: StatusDeps | None = None,
    runtime: ControlPlaneRuntime,
    webhook_routes: tuple[WebhookRoute, ...] = (),
    readiness: ControlPlaneReadiness | None = None,
) -> AiohttpApplication:
    """Build the aiohttp app with all HTTP routes registered."""
    webhook_ingress = build_webhook_ingress(cast("WebhookIngressDeps", deps), webhook_routes)
    resolved_readiness = readiness or ControlPlaneReadiness(accepting_requests=True)
    app = web.Application(
        middlewares=[
            readiness_middleware,
            build_control_plane_middleware(
                runtime,
                provider_authenticated_paths=webhook_ingress.public_paths,
            ),
        ]
    )
    app[deps_key] = deps
    app[readiness_key] = resolved_readiness
    if status_deps is not None:
        app[status_deps_key] = status_deps
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/status", _handle_status)
    app.router.add_get("/work-items", _handle_work_items)
    app.router.add_get("/actions", _handle_actions)
    app.router.add_get("/webhook-effects", _handle_webhook_effects)
    app.router.add_post(
        "/webhook-effects/{effect_id}/reconcile-absent",
        _handle_webhook_effect_absent,
    )
    app.router.add_get("/capabilities", _handle_capabilities)
    app.router.add_get("/canaries/report", _handle_canary_report)
    app.router.add_get("/canaries/runs", _handle_canary_runs)
    app.router.add_post("/deploy", _handle_deploy)
    if os.environ.get(_RUNTIME_HARNESS_ENV) == "1":
        if not isinstance(deps, RuntimeHarnessIngress):
            raise TypeError("Runtime harness HTTP dependencies do not provide message ingress")
        app.router.add_post(_RUNTIME_HARNESS_MESSAGE_PATH, _handle_runtime_harness_message)
    install_webhook_ingress(app, webhook_ingress)
    return app
