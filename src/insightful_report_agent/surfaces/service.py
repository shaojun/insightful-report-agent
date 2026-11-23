"""Compose the built-in API with the configured chat transports."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from insightful_report_agent.config import Settings
from insightful_report_agent.surfaces.api.app import create_app

_LOGGER = logging.getLogger(__name__)


async def _run_feishu(project_root: Path, env_file: Path | None) -> None:
    # The Feishu SDK owns its event loop and blocks the calling thread in Client.start().
    args = [sys.executable, "-m", "insightful_report_agent", "feishu"]
    if env_file is not None:
        args.extend(("--env-file", str(env_file)))
    # Let only the supervisor signal the worker on Ctrl-C. It waits for the worker's cleanup.
    if os.name == "nt":
        from subprocess import CREATE_NEW_PROCESS_GROUP

        process = await asyncio.create_subprocess_exec(
            *args, cwd=project_root, creationflags=CREATE_NEW_PROCESS_GROUP
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *args, cwd=project_root, start_new_session=True
        )
    try:
        status = await process.wait()
        _LOGGER.warning("Feishu stopped with exit code %s", status)
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=30)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


def create_service_app(
    project_root: Path, settings: Settings, *, env_file: Path | None = None, api_only: bool = False
) -> FastAPI:
    if api_only:
        return create_app(project_root, settings)

    feishu_fields = (settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_tenant_key)
    feishu_configured = all(feishu_fields)
    slack_credential = (
        settings.slack_app_token
        if settings.slack_transport == "socket_mode"
        else settings.slack_signing_secret
    )
    slack_configured = bool(settings.slack_bot_token and slack_credential)
    if any(feishu_fields) and not feishu_configured:
        _LOGGER.warning(
            "Feishu skipped. Set FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_TENANT_KEY."
        )
    if (settings.slack_bot_token or slack_credential) and not slack_configured:
        _LOGGER.warning(
            "Slack skipped. Set SLACK_BOT_TOKEN and the credential for SLACK_TRANSPORT."
        )

    @asynccontextmanager
    async def channels(web: FastAPI) -> AsyncIterator[None]:
        async def feishu() -> None:
            try:
                await _run_feishu(project_root, env_file)
            except Exception as error:  # noqa: BLE001 - optional channel failures stay isolated
                _LOGGER.warning("Feishu failed (%s)", type(error).__name__)

        async def slack() -> None:
            from insightful_report_agent.surfaces.slack.bolt_app import serve

            try:
                await serve(project_root, settings, web=web)
            except ImportError:
                _LOGGER.warning("Slack support requires uv sync --extra slack")
            except (Exception, SystemExit) as error:  # noqa: BLE001 - may include credentials
                _LOGGER.warning("Slack failed (%s)", type(error).__name__)

        tasks = []
        if feishu_configured:
            tasks.append(asyncio.create_task(feishu(), name="feishu surface"))
        if slack_configured:
            tasks.append(asyncio.create_task(slack(), name="slack surface"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    web = create_app(project_root, settings, channels=channels)
    if slack_configured and settings.slack_transport == "http":
        from insightful_report_agent.surfaces.slack.bolt_app import install_webhook

        install_webhook(web)
    return web
