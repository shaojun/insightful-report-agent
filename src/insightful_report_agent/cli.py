"""Local and self-hosted entry points for Insightful Report Agent."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import click

from insightful_report_agent.config import Settings, project_root
from insightful_report_agent.models.factory import ModelConfigurationError
from insightful_report_agent.runtime.sandbox import SandboxSelectionError


def configured(*, override: Path | None = None) -> tuple[Path, Settings]:
    root = project_root()
    env_files = (root / ".env", override) if override is not None else (root / ".env",)
    return root, Settings(_env_file=env_files)


@click.group()
def main() -> None:
    """Analyze source collections and deliver cited reports."""


@main.command()
@click.argument("request", required=False, default="")
@click.option("--thread", "thread_id", help="Continue a CLI thread or set its id.")
@click.option(
    "--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--directory",
    "directories",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def ask(
    request: str, thread_id: str | None, files: tuple[Path, ...], directories: tuple[Path, ...]
) -> None:
    """Collect files or write a report for a request in a local thread."""
    thread = thread_id or f"cli-{uuid4().hex[:12]}"
    if not re.fullmatch(r"cli-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,60}", thread):
        raise click.UsageError("CLI thread ids must start with cli-")
    sources = list(
        dict.fromkeys(
            [
                *files,
                *(
                    path
                    for folder in directories
                    for path in sorted(folder.rglob("*"))
                    if path.is_file()
                    and not path.is_symlink()
                    and not any(part.startswith(".") for part in path.relative_to(folder).parts)
                ),
            ]
        )
    )
    if not request.strip() and not sources:
        raise click.UsageError("Supply a request, --file or --directory")
    try:
        asyncio.run(run_request(*configured(), thread, request, sources))
    except (SandboxSelectionError, ModelConfigurationError) as error:
        raise click.ClickException(str(error)) from None


async def run_request(
    root: Path, settings: Settings, thread: str, request: str, sources: list[Path]
) -> None:
    from insightful_report_agent.runtime.local import build_local_runtime, final_text
    from insightful_report_agent.runtime.persistence import open_persistence, workspace_of
    from insightful_report_agent.tools.mcp import load_mcp_tools, servers_for_runtime

    servers = servers_for_runtime(root, settings, "local")
    loaded = await load_mcp_tools(servers)
    async with open_persistence(settings, workspace_of(settings, root)) as (checkpointer, store):
        runtime = build_local_runtime(
            settings,
            root,
            checkpointer=checkpointer,
            store=store,
            mcp_servers=[server for server in servers if server.name not in loaded.failed],
            mcp_tools=loaded.tools,
        )
        try:
            for path in sources:
                if path.stat().st_size > 200 * 1024**2:
                    raise click.ClickException(f"File exceeds 200 MiB: {path.name}")
            for path in sources:
                await runtime.attach(thread, [(path.name, path.read_bytes())])
            click.echo(f"Thread {thread}")
            if request.strip():
                result = await runtime.ask(request, thread_id=thread)
                click.echo(final_text(result))
            else:
                await runtime.source_intake.wait_ready(thread)
                click.echo(f"Collected {len(sources)} files and finished source preparation.")
            if (output := runtime.out_dir / thread).is_dir():
                for file in sorted(output.glob("insightful-report-*.html")):
                    click.echo(file.resolve())
        finally:
            await runtime.aclose()
            if runtime.binding.runner is not None:
                runtime.binding.runner.stop_all()


@main.command()
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read a service-specific settings file after the project .env.",
)
def serve(env_file: Path | None) -> None:
    """Serve the HTTP and A2A transports."""
    import uvicorn

    from insightful_report_agent.surfaces.api.app import create_app

    root, settings = configured(override=env_file)
    uvicorn.run(create_app(root, settings), host=settings.api_host, port=settings.api_port)


@main.command()
def feishu() -> None:
    """Run the Feishu long-connection bot."""
    from insightful_report_agent.surfaces.feishu.app import serve

    serve(*configured())


@main.command()
def slack() -> None:
    """Run the Slack adapter."""
    from insightful_report_agent.surfaces.slack.bolt_app import serve

    asyncio.run(serve(*configured()))


@main.group()
def sandbox() -> None:
    """Manage the isolated source-processing image."""


@sandbox.command("build")
def sandbox_build() -> None:
    """Build the Docker image with document, OCR and video parsers."""
    root, settings = configured()
    result = subprocess.run(
        ["docker", "build", "-t", settings.sandbox_image, str(root / "sandbox")], check=False
    )
    if result.returncode:
        raise click.ClickException("Sandbox image build failed")


@main.command()
def doctor() -> None:
    """Check local models, source intake settings and Docker without calling providers."""
    from insightful_report_agent.models.factory import build_chat_model
    from insightful_report_agent.runtime.sandbox import select_binding
    from insightful_report_agent.tools.abstract_prompts import load_abstract_prompts

    root, settings = configured()
    try:
        if settings.source_input_mode == "legacy":
            load_abstract_prompts(root)
    except ValueError as error:
        raise click.ClickException(str(error)) from None
    try:
        binding = select_binding(settings, root)
        click.echo(f"Sandbox {binding.name}")
        build_chat_model(settings, project_root=root)
    except (SandboxSelectionError, ModelConfigurationError) as error:
        raise click.ClickException(str(error)) from None
    click.echo(f"Lead model {settings.model}")
    click.echo(f"Source intake {settings.source_input_mode}")
    if settings.source_input_mode == "legacy":
        click.echo(f"Abstract model {settings.abstract_model or settings.model}")
