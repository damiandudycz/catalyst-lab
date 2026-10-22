"""Flow definitions for Catalyst Lab's local AI assistant.

A flow owns its model instructions, task-specific documentation, and the tools
available to that conversation. New specialized flows can be registered here
without coupling their prompts to the chat UI.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_PROMPTS_DIRECTORY = Path(__file__).with_name("prompts")


def _read_prompt_file(filename: str) -> str:
    return (_PROMPTS_DIRECTORY / filename).read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class AIDocumentation:
    id: str
    title: str
    description: str
    content_file: str

    @property
    def content(self) -> str:
        """Read a detailed guide only when the model requests it."""
        return _read_prompt_file(self.content_file)


@dataclass(frozen=True)
class AITool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]

    def ollama_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }}


class AIFlow:
    id = "base"
    title = "Assistant"
    system_prompt = "You are a helpful assistant."
    documentation: tuple[AIDocumentation, ...] = ()

    def __init__(self, tool_bridge):
        self.tool_bridge = tool_bridge

    def tools(self) -> list[AITool]:
        return []

    def startup_tools(self) -> list[AITool]:
        """Tool schemas that need to be available in the first request."""
        return self.tools()

    def tool_schemas(self) -> list[dict]:
        return [tool.ollama_schema() for tool in self.tools()]

    def system_message(self) -> str:
        resource_map = "\n".join(
            f"- {item.id}: {item.description}" for item in self.documentation
        )
        return (self.system_prompt + "\n\nResource map: each ID below names a guide and describes when to load it. "
                "Use read_documentation with that ID only when its topic applies; use list_documentation to browse "
                "the catalog.\n" + resource_map)


class MCPAssistantFlow(AIFlow):
    id = "mcp"
    title = "Catalyst Lab MCP Assistant"
    system_prompt = _read_prompt_file("mcp_core.md")
    documentation = (
        AIDocumentation("mcp-quick-guide", "MCP quick guide",
                         "Start here for a map of Library data and common app workflows.",
                         "mcp_quick_guide.md"),
        AIDocumentation("mcp-actions", "Action workflow",
                         "Load before using app tools to discover contracts, gather choices, and report results.",
                         "mcp_actions.md"),
        AIDocumentation("mcp-project-setup", "Project creation",
                         "Load when creating projects, selecting a source/template, or collecting template variables.",
                         "mcp_project_setup.md"),
        AIDocumentation("mcp-project-stages", "Project stages",
                         "Load when adding stages or changing stage settings, kernels, or scripts.",
                         "mcp_project_stages.md"),
        AIDocumentation("mcp-project-updates", "Project source updates",
                         "Load when checking or applying updates to Git- or template-based projects.",
                         "mcp_project_updates.md"),
        AIDocumentation("mcp-git-directories", "Releng and overlay workflows",
                         "Load when creating, updating, inspecting, or deleting Releng directories or overlays.",
                         "mcp_git_directories.md"),
        AIDocumentation("mcp-bugs", "Bugs workflows",
                         "Load when searching, reviewing, annotating, resolving, or removing failure entries.",
                         "mcp_bugs.md"),
        AIDocumentation("root-access", "Root Access actions",
                         "Load before enabling/disabling Root Access, canceling privileged actions, or opening their output.",
                         "root_access.md"),
        AIDocumentation("mcp-ui-map", "UI section to tool map",
                         "Load when a request names an app section or screen and you need to find its chat tool families.",
                         "mcp_ui_map.md"),
        AIDocumentation("build-queue", "Build queue",
                         "Load for queued builds, queue state, or resuming queued work.",
                         "build_queue.md"),
        AIDocumentation("mcp-builds", "Build planning and execution",
                         "Load when selecting stages, previewing dependencies/reuse, starting a project build, inspecting visual build-file previews, configuring VM launch options, or browsing saved build files and failure logs.",
                         "mcp_builds.md"),
        AIDocumentation("mcp-snapshots", "Snapshot workflows",
                         "Load when generating, importing, inspecting, deleting, or automatically cleaning Portage snapshots.",
                         "mcp_snapshots.md"),
        AIDocumentation("build-snapshot-default", "Build snapshot preference",
                         "Load when a build uses a snapshot different from the project's default.",
                         "build_snapshot_default.md"),
        AIDocumentation("app-settings", "App settings",
                         "Load when reading or changing user preferences, Git identity, or Ollama settings.",
                         "app_settings.md"),
        AIDocumentation("stage-relationships", "Stage relationships",
                         "Load when changing stage parents or interpreting inherited stage configuration.",
                         "stage_relationships.md"),
        AIDocumentation("stage-scripts", "Stage filesystem scripts",
                         "Load when creating or editing a stage4_fsscript or livecd_fsscript in a project's stage folder, especially when replacing an existing script.",
                         "stage_scripts.md"),
        AIDocumentation("mcp-deployment", "Deployment workflow",
                         "Load when creating an image or deploying a build to a disk or remote system.",
                         "mcp_deployment.md"),
    )

    def tools(self) -> list[AITool]:
        return self.tool_bridge.general_tools()

    def startup_tools(self) -> list[AITool]:
        names = {"list_tools", "read_tool_schema", "list_documentation", "read_documentation"}
        return [tool for tool in self.tools() if tool.name in names]


class AIFlowRegistry:
    """Registry of available conversation flows and their documentation."""
    def __init__(self):
        self._flows: dict[str, type[AIFlow]] = {}

    def register(self, flow_type: type[AIFlow]):
        self._flows[flow_type.id] = flow_type

    def create(self, flow_id: str, bridge) -> AIFlow:
        # Accept the pre-MCP flow ID so stale callers and restored sessions keep working.
        if flow_id == "general":
            flow_id = "mcp"
        try:
            flow_type = self._flows[flow_id]
        except KeyError as error:
            raise ValueError(f"Unknown AI conversation flow: {flow_id}") from error
        return flow_type(bridge)

    def documentation_catalog(self) -> list[dict]:
        return [{"id": document.id, "title": document.title, "description": document.description,
                 "flow": flow_type.title}
                for flow_type in self._flows.values() for document in flow_type.documentation]

    def documentation(self, document_id: str) -> AIDocumentation | None:
        return next((document for flow_type in self._flows.values()
                     for document in flow_type.documentation if document.id == document_id), None)


AI_FLOWS = AIFlowRegistry()
AI_FLOWS.register(MCPAssistantFlow)
