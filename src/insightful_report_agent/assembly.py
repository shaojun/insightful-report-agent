"""One shared agent assembly. Runtime adapters compose these components; they never fork them.

`build_agent_components` performs no network calls and holds no process-global mutable state.
Per-run objects (sandbox handles, task tools, runtime handles) are resolved per call through the
runtime profile, never cached on a middleware instance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deepagents import SubAgent
from langchain.agents.middleware import AgentMiddleware, InterruptOnConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from insightful_report_agent.config import Settings
from insightful_report_agent.middleware.guards import disclosure_for, model_guards
from insightful_report_agent.middleware.look import LookMiddleware
from insightful_report_agent.middleware.progress import ThreadProgressMiddleware
from insightful_report_agent.middleware.skill_list import load_summaries
from insightful_report_agent.middleware.today import TodayMiddleware, ZoneFor, fixed_zone
from insightful_report_agent.middleware.workspace import WorkspaceStagerMiddleware
from insightful_report_agent.models.factory import build_chat_model
from insightful_report_agent.models.registry import ToolDisclosure
from insightful_report_agent.runtime.profiles import RuntimeProfile
from insightful_report_agent.surfaces.slack.phrasebook import services
from insightful_report_agent.tools.mcp import McpServer
from insightful_report_agent.tools.sources import SourceLibrary
from insightful_report_agent.workspace.manifest import Manifest, build_manifest

SKILLS_ROOT = "/skills/"


@dataclass(frozen=True)
class AgentComponents:
    model: BaseChatModel
    tools: tuple[BaseTool, ...]
    middleware: tuple[AgentMiddleware[Any, Any, Any], ...]
    subagents: tuple[SubAgent, ...]
    interrupt_on: Mapping[str, bool | InterruptOnConfig]
    system_prompt: str
    skills: tuple[str, ...]
    manifest: Manifest
    source_library: SourceLibrary
    mcp_servers: tuple[McpServer, ...] = ()


def build_agent_components(
    *,
    settings: Settings,
    runtime: RuntimeProfile,
    model: BaseChatModel | None = None,
    role_models: Mapping[str, BaseChatModel] | None = None,
    mcp_servers: Sequence[McpServer] = (),
    mcp_tools: Sequence[BaseTool] = (),
    zone_for: ZoneFor | None = None,
) -> AgentComponents:
    """Compile one report agent for the CLI and all self-hosted surfaces."""
    root = runtime.project_root
    manifest = build_manifest(root)
    lead = model if model is not None else build_chat_model(settings, "lead", project_root=root)
    library = SourceLibrary(settings, runtime, lead, dict(role_models or {}))
    middleware = [
        WorkspaceStagerMiddleware(manifest, runtime.staging_for),
        TodayMiddleware(zone_for=zone_for or fixed_zone(settings.timezone)),
        *runtime.surface_middleware,
    ]
    if library.vision is not None:
        middleware.append(
            LookMiddleware(
                model=library.vision, model_name=settings.model, backend_for=runtime.backend_for
            )
        )
    if runtime.slack is not None and settings.enable_progress:
        middleware.append(
            ThreadProgressMiddleware(
                runtime.slack, servers=services([s.name for s in mcp_servers]), steering=False
            )
        )
    middleware.extend(
        model_guards(
            role="lead",
            lead=True,
            summaries=load_summaries(root),
            disclosure=disclosure_for(
                ToolDisclosure.ALL,
                mcp_servers,
                lambda: lead,
                settings.mcp_max_tools,
            ),
            servers=mcp_servers,
        )
    )
    prompt = (root / "instructions.md").read_text(encoding="utf-8")
    if settings.preprocessor_docs_url:
        prompt += f"\nConfigured preprocessor documentation: {settings.preprocessor_docs_url}\n"
    if library.legacy:
        prompt += "\nThis thread uses legacy raw source intake. Follow the legacy tool descriptions.\n"
    return AgentComponents(
        model=lead,
        tools=tuple([*library.tools(), *mcp_tools]),
        middleware=tuple(middleware),
        subagents=(),
        interrupt_on={},
        system_prompt=prompt,
        skills=(SKILLS_ROOT,),
        manifest=manifest,
        source_library=library,
        mcp_servers=tuple(mcp_servers),
    )
