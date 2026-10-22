"""In-process, MCP-style tools for local Catalyst Lab conversations."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from .ai_flows import AITool, AI_FLOWS

MAX_READ_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000


def _unique_name(base: str, is_available) -> str:
    """Return a UI-style unique name using the existing '- N' suffix convention."""
    base = str(base).strip()
    if not base or is_available(base):
        return base
    number = 2
    candidate = f"{base} - {number}"
    while not is_available(candidate):
        number += 1
        candidate = f"{base} - {number}"
    return candidate


class AIToolBridge:
    """Expose narrowly scoped app data and navigation to AI flows.

    This provides the local tools/list and tools/call boundary used by the chat;
    it does not start a network or stdio MCP server.
    """
    def __init__(self, content_navigation_view=None, user_input_callback=None,
                 tool_progress_callback=None):
        self.content_navigation_view = content_navigation_view
        self.user_input_callback = user_input_callback
        self.tool_progress_callback = tool_progress_callback
        self.cancel_event = None
        self._active_tool_client = None
        # Provenance for model-created select fields. The model may present
        # only values returned by the most recent list_options call.
        self._last_listed_options = {}
        self._last_listed_option_rows = {}
        self._last_template_variable_options = {}
        self._last_template_variables = {}
        self._last_project_form_context = {}

    def cancel_active_tool(self):
        """Interrupt a cancellable Ollama-backed tool currently running in this chat."""
        client = self._active_tool_client
        if client is not None:
            client.cancel()

    def begin_user_turn(self):
        """Forget option provenance from earlier chats before a new request."""
        self._last_listed_options.clear()
        self._last_listed_option_rows.clear()
        self._last_template_variable_options.clear()
        self._last_template_variables.clear()
        self._last_project_form_context.clear()

    def documentation_catalog(self):
        return AI_FLOWS.documentation_catalog()

    def general_tools(self) -> list[AITool]:
        string = {"type": "string"}
        raspberry_pi_settings = {"type": "object", "properties": {
            "serial_console": {"type": "boolean", "default": True},
            "extra_arguments": {"type": "string", "maxLength": 4096, "default": ""},
            "config_lines": {"type": "string", "maxLength": 16384,
                             "default": "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\ndisable_overscan=1"},
            "older_models_firmware": {"type": "boolean", "default": True},
        }, "additionalProperties": False,
            "description": "Optional Raspberry Pi boot customization. Use only with raspberry_pi=true; omitted fields use the live-ISO wizard defaults."}
        return [
            AITool("list_tools", "Find app tools by task words. Results are ranked by relevance and paged; use next_offset to inspect more matches. Read a tool schema before using it.",
                   {"type": "object", "properties": {"query": string,
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                    "required": []}, self._list_tools),
            AITool("read_tool_schema", "Read the exact name, description, and JSON parameter schema for one app tool before calling it.",
                   {"type": "object", "properties": {"name": string}, "required": ["name"]}, self._read_tool_schema),
            AITool("list_documentation", "List documentation available across all assistant flows.",
                   {"type": "object", "properties": {}, "required": []}, self._list_documentation),
            AITool("read_documentation", "Read a documentation item by its id.",
                   {"type": "object", "properties": {"id": string}, "required": ["id"]}, self._read_documentation),
            AITool("list_ollama_models", "Check connection to the configured Ollama server and list installed models, their sizes, and the selected chat model. Returns the server error if unreachable.",
                   {"type": "object", "properties": {}, "required": []}, self._list_ollama_models),
            AITool("open_ollama_model_catalog", "Open the same fixed Ollama model catalog linked from AI Assistance. Use only when the user explicitly asks to browse or open the Ollama model catalog; this opens https://ollama.com/library and accepts no arbitrary URL.",
                   {"type": "object", "properties": {}, "required": []}, self._open_ollama_model_catalog),
            AITool("pull_ollama_model", "Download a model to the configured Ollama server. Use only when the user explicitly asks to download/install a model. Live progress is shown in chat.",
                   {"type": "object", "properties": {"model": {"type": "string", "minLength": 1, "maxLength": 200}}, "required": ["model"]}, self._pull_ollama_model),
            AITool("list_objects", "List objects in the current Catalyst Lab Library. Package-cache rows include architecture, CPU flags, linked projects, and each project's compatibility issues; runner results include the built-in Host endpoint plus VM runners. Optional type: project, toolset, snapshot, releng, overlay, packages, runner.",
                   {"type": "object", "properties": {"type": string}, "required": []}, self._list_objects),
            AITool("list_options", "Get selectable app values before asking the user to choose. Categories: project_sources, project_templates, template_variables, git_directory_sources, toolset_stages, toolset_applications, stage_image_options, live_iso_options, stage_kernel_packages, architectures, releng_directories, snapshots, toolsets, library_toolsets, project_toolsets, runners, build_snapshots, build_toolsets, build_runners, packages_directories, projects, project_stages, project_stage_toolsets, stage_releng_templates, sections, stage_targets, stage_settings, stage_setting_options. For the app-wide default toolset in Settings, use library_toolsets; it includes all installed toolsets. For a project's preferred Catalyst toolset, use project_toolsets with context.project; update_project(field=toolset) accepts one of those exact IDs or default to restore the app default. For an existing build request, use projects to select an existing project, then project_stages, build_toolsets, build_runners, and build_snapshots with context.project; do not use project_sources/project_templates or generic snapshots/toolsets for build choices. To add a project stage, load project_stage_toolsets with context.project, stage_targets with context.toolset, project_stages with context.project for seed choices, and stage_releng_templates with context.project and context.target. git_directory_sources needs context.type (releng or overlay): Releng is Git-only; overlays offer Git, local-directory copy, and empty. toolset_stages needs context.architecture or uses the host architecture. Build option categories need context.project. stage_image_options needs context.project and context.build_timestamp; optionally pass context.firmware to filter boot choices. live_iso_options needs context.project and context.build_timestamp for a completed live ISO and returns locally available SSH keys. stage_kernel_packages needs context.project and context.stage, and optionally context.kernel for its selected package default. Template variables need context.template and optionally context.project_name plus selected.<variable_id> values. Stage settings need context.project and context.stage; stage_setting_options additionally needs context.setting. Multiple categories can be requested together.",
                   {"type": "object", "properties": {
                       "categories": {"type": "array", "items": {"type": "string", "enum": ["project_sources", "project_templates", "template_variables", "git_directory_sources", "toolset_stages", "toolset_applications", "stage_image_options", "live_iso_options", "stage_kernel_packages", "architectures", "releng_directories", "snapshots", "toolsets", "library_toolsets", "project_toolsets", "runners", "build_snapshots", "build_toolsets", "build_runners", "packages_directories", "projects", "project_stages", "project_stage_toolsets", "stage_releng_templates", "sections", "stage_targets", "stage_settings", "stage_setting_options"]}, "minItems": 1},
                       "context": {"type": "object", "additionalProperties": {}}}, "required": ["categories"]}, self._list_options),
            AITool("get_stage_settings", "Read a stage's saved and effective Catalyst settings. Returns whether each value is explicit, inherited/calculated, or unset, and lists saved settings unsupported by the selected target (or unverified without a toolset); optionally limit to one setting. Use before interpreting or changing stage configuration.",
                   {"type": "object", "properties": {"project": string, "stage": string, "setting": string},
                    "required": ["project", "stage"]}, self._get_stage_settings),
            AITool("list_stage_kernels", "List a stage's effective boot kernels, showing which come from its Releng template and each kernel setting's saved/effective value. Use before changing kernel configuration.",
                   {"type": "object", "properties": {"project": string, "stage": string},
                    "required": ["project", "stage"]}, self._list_stage_kernels),
            AITool("update_stage_kernel", "Add or rename a stage-specific kernel, change/reset a kernel setting, or toggle whether the stage includes Releng template kernels. Actions: add, rename, set_setting, reset_setting, set_releng_inheritance. Kernel settings are sources, distkernel, config, dracut_args, extraversion, packages, use, gk_kernargs, gk_action, aliases, console, initramfs_overlay, softlevel. Set list settings with string arrays and distkernel with a boolean. This edits stage configuration only; it does not start a build.",
                   {"type": "object", "properties": {"project": string, "stage": string,
                    "action": {"type": "string", "enum": ["add", "rename", "set_setting", "reset_setting", "set_releng_inheritance"]},
                    "name": string, "new_name": string, "setting": string,
                    "value": {"type": ["string", "boolean", "array"], "items": {"type": "string"}},
                    "include_releng": {"type": "boolean"}},
                    "required": ["project", "stage", "action"]}, self._update_stage_kernel),
            AITool("delete_stage_kernel", "Permanently remove a stage-specific kernel and its stage overrides; requires in-chat confirmation. A kernel supplied by the Releng template cannot be removed while inheritance is enabled; disable inheritance to replace the template's kernel list.",
                   {"type": "object", "properties": {"project": string, "stage": string, "name": string},
                    "required": ["project", "stage", "name"]}, self._delete_stage_kernel),
            AITool("ask_user", "Show an in-chat form and return the user's submitted answers. Always provide title, message, and fields; fields must contain 1–50 complete field objects. Never ask for action details in prose. Ask only for details needed for the next action. Project creation must start with one short form containing only source type and project name; after the source is chosen, load choices that apply and ask a second form for those values. For Git, include its repository URL; for templates, ask for the template and then its dependent variables. The app enforces this first step even if you include extra project fields. The user can add an optional note or correction; honor it, refreshing list_options if choices are missing. Copy select options and values exactly from list_options; never invent generic choices such as 'Specific snapshot' when actual entries are available. When an option is marked default, set its default flag; use a field default for other known defaults so the form preselects/fills it. For a required select with exactly one available option, choose it automatically instead of asking. Do not ask the user to choose a known default. Ask one step at a time when later options depend on earlier answers.",
                   {"type": "object", "properties": {
                       "title": string, "message": string,
                       "fields": {"type": "array", "minItems": 1, "maxItems": 50, "items": {
                           "type": "object", "properties": {
                               "id": string, "label": string,
                               "type": {"type": "string", "enum": ["text", "select", "multiselect", "boolean"]},
                               "required": {"type": "boolean"}, "placeholder": string,
                               "default": {},
                               "options": {"type": "array", "items": {"type": "object", "properties": {
                                   "value": string, "label": string, "default": {"type": "boolean"}}, "required": ["value", "label"]}},
                           }, "required": ["id", "label", "type"]}}},
                    "required": ["title", "message", "fields"]}, self._ask_user),
            AITool("list_sections", "List application sections, only when the user explicitly asks to navigate or open one.",
                   {"type": "object", "properties": {}, "required": []}, self._list_sections),
            AITool("open_section", "Navigate to a main application section only when the user explicitly asks to open or show that section. Never use this to perform an action.",
                   {"type": "object", "properties": {"section": string}, "required": ["section"]}, self._open_section),
            AITool("open_about", "Show the same About Catalyst Lab window as the app menu. Use only when the user explicitly asks to open or show About; for questions about app data or settings, use their read tools instead.",
                   {"type": "object", "properties": {}, "required": []}, self._open_about),
            AITool("open_keyboard_shortcuts", "Show the app's Keyboard Shortcuts overlay, matching the app menu item. Use only when the user explicitly asks to see keyboard shortcuts.",
                   {"type": "object", "properties": {}, "required": []}, self._open_keyboard_shortcuts),
            AITool("open_project", "Open a project view only when the user explicitly asks to see it. Never use this to perform a project action.",
                   {"type": "object", "properties": {"project": string}, "required": ["project"]}, self._open_project),
            AITool("open_object", "Open an object's details page only when the user explicitly asks to see that page. Types: project, stage, toolset, snapshot, releng, overlay, packages, runner, builds. For stage, pass project and stage name/ID; for builds, pass project.",
                   {"type": "object", "properties": {
                       "type": {"type": "string", "enum": ["project", "stage", "toolset", "snapshot", "releng", "overlay", "packages", "runner", "builds"]},
                       "name": string, "project": string, "stage": string}, "required": ["type"]}, self._open_object),
            AITool("open_local_folder", "Open an app-managed folder in the system file browser only when the user explicitly asks. Targets: library, packages, build, stage_overlay. For packages pass packages_directory name/ID; for build pass project, stage, and exact timestamp from list_builds; for stage_overlay pass project, stage, and overlay set to portage_confdir, stage4_root_overlay, livecd_root_overlay, or livecd_overlay. Arbitrary paths are not accepted.",
                   {"type": "object", "properties": {
                       "target": {"type": "string", "enum": ["library", "packages", "build", "stage_overlay"]},
                       "packages_directory": string, "project": string,
                       "stage": string, "timestamp": string,
                       "overlay": {"type": "string", "enum": ["portage_confdir", "stage4_root_overlay", "livecd_root_overlay", "livecd_overlay"]}},
                    "required": ["target"]}, self._open_local_folder),
            AITool("get_git_status", "Read the cached Git state for a project, Releng directory, or overlay, including local changes and whether a remote update was detected.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["project", "releng", "overlay"]}, "name": string},
                    "required": ["type", "name"]}, self._get_git_status),
            AITool("list_git_changes", "Inspect current local Git changes without fetching, modifying files, or returning file contents. Lists changed, staged, renamed, and untracked paths; use before reviewing, committing, or discarding changes.",
                   {"type": "object", "properties": {
                       "type": {"type": "string", "enum": ["project", "releng", "overlay"]},
                       "name": string,
                       "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}},
                    "required": ["type", "name"]}, self._list_git_changes),
            AITool("update_git_directory", "Fetch and rebase a project's, Releng directory's, or overlay's Git repository using its existing app update process. Use only when explicitly requested; refuse while another Git operation is running.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["project", "releng", "overlay"]}, "name": string},
                    "required": ["type", "name"]}, self._update_git_directory),
            AITool("commit_git_changes", "Commit local changes in a project, Releng directory, or overlay. Ask the user for a commit message before calling. This stages and commits all changes in that Git directory.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["project", "releng", "overlay"]}, "name": string, "message": string},
                    "required": ["type", "name", "message"]}, self._commit_git_changes),
            AITool("discard_git_changes", "Permanently discard local changes and remove untracked and ignored files from a project, Releng directory, or overlay. Use only when explicitly requested. After checking the target, call this tool directly; it presents the required in-chat confirmation.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["project", "releng", "overlay"]}, "name": string},
                    "required": ["type", "name"]}, self._discard_git_changes),
            AITool("rename_git_directory", "Rename a Releng directory or overlay. Project renaming uses rename_project. The name must be unique within its type.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["releng", "overlay"]}, "name": string, "new_name": string},
                    "required": ["type", "name", "new_name"]}, self._rename_git_directory),
            AITool("delete_git_directory", "Permanently delete a Releng directory or overlay and all its files. Use only when explicitly requested. Call directly after identifying the target; it presents the required in-chat confirmation and reports project references.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["releng", "overlay"]}, "name": string},
                    "required": ["type", "name"]}, self._delete_git_directory),
            AITool("create_git_directory", "Create a Releng directory or overlay directly from chat. First list git_directory_sources for its type. Releng supports Git only; overlays also support copying a local directory (pass its absolute path as local_directory) or creating an empty overlay. Collect the name and any required URL/path before calling. Call directly when ready; it starts without another permission prompt. Never open its wizard.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["releng", "overlay"]},
                    "name": string, "source": {"type": "string", "enum": ["git", "local", "empty"]},
                    "repository_url": string, "local_directory": string},
                    "required": ["type", "name", "source"]}, self._create_git_directory),
            AITool("create_vm_runner", "Create a VM runner directly from chat. If omitted, use the UI defaults: a unique 'Alpine VM' name, app resource limits, and set_as_default=true. Pass set_as_default=false only when the user wants a different default runner. Call directly when ready; creation starts without another permission prompt. Never open the runner wizard.",
                   {"type": "object", "properties": {"name": string,
                    "cpus": {"type": "integer", "minimum": 1, "maximum": 64,
                             "description": "Cannot exceed the host's available CPU count."},
                    "memory_gib": {"type": "integer", "minimum": 2, "maximum": 256},
                    "workspace_gib": {"type": "integer", "minimum": 20, "maximum": 2048},
                    "swap_gib": {"type": "integer", "minimum": 0, "maximum": 512},
                    "set_as_default": {"type": "boolean", "default": True}},
                    "required": []}, self._create_vm_runner),
            AITool("create_toolset", "Create and install a Gentoo toolset directly from chat. Use the host architecture and supported default runner when omitted. Call list_options(toolset_stages) for the exact recommended stage3 URL, toolset_applications for package/version IDs and defaults, and runners for available runners. If omitted, use the recommended stage, name it from that stage, select applications marked default_selected, use app defaults for optional settings, and set_as_default=true like the UI. Pass set_as_default=false only when the user wants a different default toolset. Call directly when ready; installation starts without another permission prompt. Root authorization may still be required. Never open the toolset wizard.",
                   {"type": "object", "properties": {"name": string, "architecture": string, "stage_url": string,
                    "applications": {"type": "array", "items": string},
                    "application_versions": {"type": "object", "additionalProperties": {"type": "string"}},
                    "patches": {"type": "object", "additionalProperties": {"type": "array", "items": string}},
                    "runner": string, "allow_binpkgs": {"type": "boolean", "default": True},
                    "set_as_default": {"type": "boolean", "default": True}},
                    "required": []}, self._create_toolset),
            AITool("open_wizard", "Open an existing app wizard only when the user explicitly asks to open or use that wizard. Never open it merely to complete a task or collect choices; use direct tools and in-chat forms instead. Workflows: initial_setup, project_create, project_build (project), stage_create (project), snapshot_create, overlay_create, releng_create, toolset_create, vm_runner_create, project_update (project plus source template/repository), project_deploy (project), vm_launch (project plus exact completed-build timestamp from list_deployable_builds). Use vm_launch only when the user asks to configure VM launch options; use start_vm for direct launch.",
                   {"type": "object", "properties": {
                       "workflow": {"type": "string", "enum": ["initial_setup", "project_create", "project_build", "stage_create", "snapshot_create", "overlay_create", "releng_create", "toolset_create", "vm_runner_create", "project_update", "project_deploy", "vm_launch"]},
                       "project": string, "timestamp": string,
                       "source": {"type": "string", "enum": ["template", "repository"]}}, "required": ["workflow"]}, self._open_wizard),
            AITool("list_operations", "List active background operations, running VM sessions, queued builds, and completed/failed operation results still waiting to be viewed in the sidebar. IDs can be passed to open_operation; active and queued IDs can also be passed to stop_operation as applicable. Active Catalyst stages include live Portage task progress, source/binary completion counts, and current package when available.",
                   {"type": "object", "properties": {}, "required": []}, self._list_operations),
            AITool("open_operation", "Open the same live view as clicking an active operation, running VM, or queued build in the sidebar. Use only when the user explicitly asks to open/show it, and pass the exact op:, vm:, or queue: ID from list_operations. This only opens its existing view; it does not start, stop, or alter the operation.",
                   {"type": "object", "properties": {"operation_id": string}, "required": ["operation_id"]}, self._open_operation),
            AITool("read_operation_log", "Read recent output from the currently running step of an active background operation. Pass an operation ID from list_operations; output is capped to the requested tail and known deployment secrets are redacted.",
                   {"type": "object", "properties": {"operation_id": string, "limit": {"type": "integer", "minimum": 1, "maximum": 400}}, "required": ["operation_id"]}, self._read_operation_log),
            AITool("read_vm_output", "Read the live QEMU log or guest serial console shown in the VM view. Pass a vm:<id> from list_operations. Read-only; output is bounded to the requested tail.",
                   {"type": "object", "properties": {"operation_id": string,
                    "source": {"type": "string", "enum": ["log", "console"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400}},
                    "required": ["operation_id"]}, self._read_vm_output),
            AITool("send_vm_console_input", "Send exact user-requested text or a supported key to a running VM's serial console, matching typing in the app's Console view. Only use when the user explicitly asks; never invent or rewrite commands. Pass a vm:<id> from list_operations. Text is limited to 1000 printable characters; set press_enter=true only when the user asks to submit/execute it. For a keypress, pass key instead of text: enter, backspace, tab, escape, up, down, left, right, or ctrl_a through ctrl_z. This sends input to the guest and does not verify receipt or action.",
                   {"type": "object", "properties": {"operation_id": string,
                    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "key": {"type": "string", "enum": ["enter", "backspace", "tab", "escape", "up", "down", "left", "right", *(f"ctrl_{letter}" for letter in "abcdefghijklmnopqrstuvwxyz")]},
                    "press_enter": {"type": "boolean", "default": False}},
                    "required": ["operation_id"]}, self._send_vm_console_input),
            AITool("stop_operation", "Cancel an active background operation or remove a queued build. Use only when explicitly requested.",
                   {"type": "object", "properties": {"operation_id": string}, "required": ["operation_id"]}, self._stop_operation),
            AITool("control_vm", "Control a running VM guest by its vm:<id> from list_operations. Actions: power_off (request guest shutdown), reset, or send_ctrl_alt_delete. These affect the guest; call only when the user explicitly asks. Reset and Ctrl+Alt+Delete are supported by QEMU only; Apple Virtualization supports power_off. For an immediate forced stop, use stop_operation.",
                   {"type": "object", "properties": {
                       "operation_id": string,
                       "action": {"type": "string", "enum": ["power_off", "reset", "send_ctrl_alt_delete"]}},
                    "required": ["operation_id", "action"]}, self._control_vm),
            AITool("resume_build_queue", "Resume processing builds already in the queue, matching the app's Start queue action. This can automatically start the next queued build, so use only when the user explicitly asks to resume or start the queue.",
                   {"type": "object", "properties": {}, "required": []}, self._resume_build_queue),
            AITool("update_settings", "Change one supported app preference. Use only an exact setting from the enum and the matching JSON value type. `qemu_cpus` is an integer from 1–64; `qemu_memory_gib` is an integer from 1–256. These are defaults for future VM launches. `keep_root_unlocked` controls whether Root Access stays unlocked after an authorized action; change it only when the user explicitly asks. For `default_toolset_id`, use an exact ID from `list_options(library_toolsets)`; `default` restores automatic selection. For `default_runner_id`, use an exact ID from `list_options(runners)`; `default` restores app selection, and `host` selects Host. `ollama_model` must match `list_ollama_models` and affects future chats only. Enabling `auto_clean_snapshots` can immediately delete unused snapshots and requires confirmation.",
                   {"type": "object", "properties": {
                       "setting": {"type": "string", "enum": [
                           "navigation_style", "pause_queue_on_failure", "keep_root_unlocked", "qemu_cpus", "qemu_memory_gib",
                           "auto_clean_snapshots", "default_toolset_id", "default_runner_id",
                           "project_stages_view_mode", "git_identity_mode", "git_user_name", "git_user_email",
                           "ollama_url", "ollama_model"]},
                       "value": {"type": ["string", "boolean", "integer"],
                                 "description": "Use the type required by setting: booleans for toggles, integers for QEMU limits, text for names/URLs/identity, and exact option IDs for defaults."}},
                    "required": ["setting", "value"]}, self._update_settings),
            AITool("get_settings", "Read current app preferences, including the selected/system Git identities and a sanitized Ollama server URL. URL credentials, query strings, and fragments are redacted.",
                   {"type": "object", "properties": {}, "required": []}, self._get_settings),
            AITool("get_root_access_status", "Read whether the Linux root helper is active and list current privileged action call_ids, names, cancellability, and termination state. Does not expose command output or change root access.",
                   {"type": "object", "properties": {}, "required": []}, self._get_root_access_status),
            AITool("set_root_access", "Enable or disable the app's Linux root helper only when the user explicitly asks. Enabling uses the normal operating-system pkexec authorization prompt and never bypasses OS authorization; it returns authorization_requested while that prompt is being handled, so verify with get_root_access_status. Refuses enabling while privileged actions are running and refuses disabling while any privileged action is active.",
                   {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"]}, self._set_root_access),
            AITool("cancel_root_action", "Request cancellation of one active, cancellable privileged action. Use only when the user explicitly asks to cancel it. Get the exact call_id from get_root_access_status; action names are not unique. Cancellation may take time and the operation may already have made partial changes.",
                   {"type": "object", "properties": {"call_id": string}, "required": ["call_id"]}, self._cancel_root_action),
            AITool("open_root_action_output", "Open the app's live output view for one active privileged action only when the user explicitly asks to see it. Use the exact call_id from get_root_access_status; this opens output in the UI and does not send the output contents to the model.",
                   {"type": "object", "properties": {"call_id": string}, "required": ["call_id"]}, self._open_root_action_output),
            AITool("change_library_location", "Change the Library location directly from chat. Pass the destination folder path supplied by the user. If the destination is an existing Library, this switches to it; otherwise ask the user to choose create empty, copy, or move. Always confirm the destination and operation in chat immediately before changing it. This restarts Catalyst Lab after the assistant reports the result; refuses while any operation or mounted/running environment uses the Library.",
                   {"type": "object", "properties": {"path": {"type": "string", "minLength": 1},
                    "mode": {"type": "string", "enum": ["switch", "new", "copy", "move"]}},
                    "required": ["path"]}, self._change_library_location),
            AITool("list_bugs", "List failed package builds shown in the Bugs section. Optionally filter by exact project, case-insensitive text across package/repository/phase/reason/project/stage, and resolution status. Each failure returns `failure_id` for other Bugs tools (with `id` retained as an alias); repeated matching failures also include a shared issue key. Results are ordered like the Bugs section and can be paged with offset and limit. Set group_by to issue, package, project, stage, or date to match the Bugs screen grouping; grouped results retain exact failure IDs and issue keys. Omit group_by to keep the flat-list response.",
                   {"type": "object", "properties": {"project": string, "query": string,
                    "status": {"type": "string", "enum": ["open", "solved", "potentially_solved", "potentially_resurfaced"]},
                    "group_by": {"type": "string", "enum": ["issue", "package", "project", "stage", "date"]},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": []}, self._list_bugs),
            AITool("open_bugzilla", "Open the Gentoo Bugzilla search for a listed Gentoo package failure in the system browser. Only use when the user explicitly asks to open or visit its Bugzilla page. Get failure_id from list_bugs; failures from non-Gentoo repositories do not have this app link.",
                   {"type": "object", "properties": {"failure_id": string}, "required": ["failure_id"]}, self._open_bugzilla),
            AITool("set_bug_status", "Set one listed failure to open, solved, potentially_solved, or potentially_resurfaced by failure_id, or update every occurrence of one grouped issue by its exact issue_key from list_bugs. Potential statuses normally come from later matching build results; set them manually only when the user asks.",
                   {"type": "object", "properties": {"failure_id": string,
                    "issue_key": {"type": "array", "items": {"type": ["string", "null"]},
                                  "description": "Optional exact issue_key from list_bugs to update every grouped occurrence."},
                    "status": {"type": "string", "enum": ["open", "solved", "potentially_solved", "potentially_resurfaced"]}},
                    "required": ["status"],
                    "oneOf": [{"required": ["failure_id"], "not": {"required": ["issue_key"]}},
                              {"required": ["issue_key"], "not": {"required": ["failure_id"]}}]}, self._set_bug_status),
            AITool("remove_bug", "Remove one listed failure by failure_id, or every occurrence in a grouped issue by its exact issue_key from list_bugs. Pass exactly one selector. Builds and logs remain; this asks for confirmation.",
                   {"type": "object", "properties": {"failure_id": string,
                    "issue_key": {"type": "array", "items": {"type": ["string", "null"]},
                                  "description": "Optional exact issue_key from list_bugs to remove every grouped occurrence."}},
                    "oneOf": [{"required": ["failure_id"], "not": {"required": ["issue_key"]}},
                              {"required": ["issue_key"], "not": {"required": ["failure_id"]}}]}, self._remove_bug),
            AITool("list_bug_notes", "Read issue notes for one exact failure_id from list_bugs, or show the Bugs page's package notes overview with exact project and package values from list_bugs. Pass exactly one mode: failure_id alone, or both project and package. The overview groups notes by distinct issue and returns failure_id values that can be used with add_bug_note, edit_bug_note, or delete_bug_note. Page it with offset and limit.",
                   {"type": "object", "properties": {"failure_id": string, "project": string, "package": string,
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                    "oneOf": [
                        {"required": ["failure_id"], "not": {"anyOf": [
                            {"required": ["project"]}, {"required": ["package"]}]}},
                        {"required": ["project", "package"], "not": {"required": ["failure_id"]}},
                    ]}, self._list_bug_notes),
            AITool("add_bug_note", "Add a note to the matching issue for a failure ID from list_bugs.",
                   {"type": "object", "properties": {"failure_id": string, "text": string}, "required": ["failure_id", "text"]}, self._add_bug_note),
            AITool("edit_bug_note", "Edit an issue note by its note ID. Get note IDs with list_bug_notes.",
                   {"type": "object", "properties": {"failure_id": string, "note_id": string, "text": string}, "required": ["failure_id", "note_id", "text"]}, self._edit_bug_note),
            AITool("delete_bug_note", "Permanently delete an issue note; this asks for confirmation. Get note IDs with list_bug_notes.",
                   {"type": "object", "properties": {"failure_id": string, "note_id": string}, "required": ["failure_id", "note_id"]}, self._delete_bug_note),
            AITool("delete_project", "Permanently delete a project. Use only when explicitly requested. Call directly after identifying the project; the tool presents an in-chat confirmation with optional cleanup of build history and binary packages. Never select cleanup options without the user's choice.",
                   {"type": "object", "properties": {"project": string}, "required": ["project"]}, self._delete_project),
            AITool("delete_build_record", "Permanently delete one saved stage-build record, including its artifact, logs, and failure details. First use list_builds for exact stage ID and timestamp. Refuses while the project build or a VM is using it; asks for in-chat confirmation. Other build records and issue notes are preserved.",
                   {"type": "object", "properties": {"project": string, "stage": string, "timestamp": string},
                    "required": ["project", "stage", "timestamp"]}, self._delete_build_record),
            AITool("delete_build_run", "Permanently delete all stage-build artifacts and the saved step record for one build run. Use the exact timestamp from list_builds. Refuses active builds or VM use and requires one in-chat confirmation; bug issue notes are preserved. Use only when explicitly requested.",
                   {"type": "object", "properties": {"project": string, "timestamp": string},
                    "required": ["project", "timestamp"]}, self._delete_build_run),
            AITool("delete_snapshot", "Permanently delete an unused snapshot and its extracted cache. Only snapshots not referenced by projects, active operations, or the latest-snapshot rule can be deleted. Asks for confirmation.",
                   {"type": "object", "properties": {"snapshot": string}, "required": ["snapshot"]}, self._delete_snapshot),
            AITool("get_snapshot_usage", "Read which projects and saved stage builds reference a Portage snapshot, matching the Snapshot details Used by view. Pass the exact snapshot filename or name from list_objects(type='snapshot'); this does not modify anything.",
                   {"type": "object", "properties": {"snapshot": string}, "required": ["snapshot"]}, self._get_snapshot_usage),
            AITool("delete_packages_cache", "Permanently delete a shared binary-package folder. Asks for confirmation and clears project references; refuses while one of its projects is building.",
                   {"type": "object", "properties": {"packages_directory": string}, "required": ["packages_directory"]}, self._delete_packages_cache),
            AITool("create_packages_directory", "Create a shared binary-packages folder directly in chat. For a standalone folder, require a unique name and an exact architecture value from list_options(architectures); optional cpu_flags default to architecture defaults. To create and assign a compatible folder for a project, pass its exact project ID; the app derives that project's architecture and effective CPU flags and can generate a unique name from the project. Do not invent architecture values or CPU flags. Proceeds without an extra permission prompt once required choices are known; it never opens a wizard.",
                   {"type": "object", "properties": {"name": string, "architecture": string,
                    "cpu_flags": string, "project": string},
                    "oneOf": [{"required": ["project"]},
                              {"required": ["name", "architecture"], "not": {"required": ["project"]}}]},
                   self._create_packages_directory),
            AITool("delete_runner", "Delete a stopped VM runner and its VM disk. This removes runner-local extracted toolset/package caches and workspace data; shared Library files and folders are kept on the host. Refuses while running or used by an operation and asks for confirmation.",
                   {"type": "object", "properties": {"runner": string}, "required": ["runner"]}, self._delete_runner),
            AITool("get_toolset_status", "Read a toolset's Library details: source and timestamps, file size/path, runner, installed applications and patches, mount bindings, use/reservation state, and binary-package preference.",
                   {"type": "object", "properties": {"toolset": string}, "required": ["toolset"]}, self._get_toolset_status),
            AITool("set_toolset_mount", "Mount a toolset read-only or writable, or unmount it saving or discarding changes. Discarding requires in-chat confirmation. The operation may require the app's root authorization flow.",
                   {"type": "object", "properties": {"toolset": string, "action": {"type": "string", "enum": ["mount_read_only", "mount_writable", "unmount_save", "unmount_discard"]}},
                    "required": ["toolset", "action"]}, self._set_toolset_mount),
            AITool("update_toolset", "Run the normal toolset update process, including Portage and binary packages. Use only when explicitly requested; this may take a long time and may require root authorization.",
                   {"type": "object", "properties": {"toolset": string}, "required": ["toolset"]}, self._update_toolset),
            AITool("configure_toolset", "Change a toolset's installed applications, application versions, local patch files, or binary-package preference. First use list_options(toolset_applications) and get_toolset_status. Pass selected application package IDs, version IDs from that result, and exact local patch paths supplied by the user. Omit a version or patches entry to keep the installed value. Application additions or updates start the normal asynchronous toolset update without another permission prompt. Removing installed applications requires confirmation. Changing only allow_binpkgs saves the preference without running an update.",
                   {"type": "object", "properties": {
                       "toolset": string,
                       "applications": {"type": "array", "items": string},
                       "application_versions": {"type": "object", "additionalProperties": {"type": "string"}},
                       "patches": {"type": "object", "additionalProperties": {"type": "array", "items": string}},
                       "allow_binpkgs": {"type": "boolean"}},
                    "required": ["toolset"]}, self._configure_toolset),
            AITool("rename_toolset", "Rename an idle toolset. Existing project references continue to use its ID.",
                   {"type": "object", "properties": {"toolset": string, "name": string}, "required": ["toolset", "name"]}, self._rename_toolset),
            AITool("delete_toolset", "Permanently delete an idle toolset and its files. Projects referencing it will lose that preference and fall back to the app default. Requires in-chat confirmation.",
                   {"type": "object", "properties": {"toolset": string}, "required": ["toolset"]}, self._delete_toolset),
            AITool("get_runner_status", "Read a Host or VM runner's availability, use state, and users. VM runners also report instance/location, whether the temporary workspace is mounted, resource limits, and optionally live CPU/memory/disk usage. Discover IDs with list_objects(type='runner'). Host is always available on supported Linux systems and cannot be started/stopped or resized.",
                   {"type": "object", "properties": {"runner": string, "include_usage": {"type": "boolean"}},
                    "required": ["runner"]}, self._get_runner_status),
            AITool("set_runner_state", "Start or stop a VM runner. Use list_objects(type='runner') to discover VM runners. Host is always active and cannot be started/stopped. Stop refuses while a VM runner is in use; lifecycle changes may take a while.",
                   {"type": "object", "properties": {"runner": string, "state": {"type": "string", "enum": ["start", "stop"]}},
                    "required": ["runner", "state"]}, self._set_runner_state),
            AITool("update_runner", "Rename a VM runner or change its CPU, memory, working-space, and swap limits. Use list_objects(type='runner') to discover VM runners. Host cannot be renamed or resized. VM resource limits can only be changed while stopped; specify at least one field.",
                   {"type": "object", "properties": {"runner": string, "name": string,
                    "cpus": {"type": "integer", "minimum": 1, "maximum": 64,
                             "description": "Cannot exceed the host's available CPU count."},
                    "memory_gib": {"type": "integer", "minimum": 2, "maximum": 256},
                    "workspace_gib": {"type": "integer", "minimum": 20, "maximum": 2048},
                    "swap_gib": {"type": "integer", "minimum": 0, "maximum": 512}},
                    "required": ["runner"]}, self._update_runner),
            AITool("create_project", "Create a project directly from chat without opening the wizard. Ask first for only source type and project name, then load options and ask for values relevant to the selected source. Sources: empty, git, template. Every call needs an exact architecture, Releng directory, snapshot (use 'latest' for the current snapshot), and Catalyst toolset (use 'default' for app selection). Git also needs a repository URL. For template source, pass template as its exact returned ID and template_options as a JSON object of chosen variable IDs and values after loading template_variables for the selected template.",
                   {"type": "object", "properties": {
                       "name": string,
                       "source": {"type": "string", "enum": ["empty", "git", "template"]},
                       "repository_url": string,
                       "template": string,
                       "template_options": {"type": "object", "additionalProperties": {}},
                       "architecture": string,
                       "releng_directory": string,
                       "snapshot": string,
                       "toolset": string,
                   }, "required": ["name", "source", "architecture", "releng_directory", "snapshot", "toolset"],
                    "oneOf": [
                        {"properties": {"source": {"enum": ["empty"]}}},
                        {"properties": {"source": {"enum": ["git"]}}, "required": ["repository_url"]},
                        {"properties": {"source": {"enum": ["template"]}}, "required": ["template"]},
                    ]}, self._create_project),
            AITool("rename_project", "Rename a project in the Library.",
                   {"type": "object", "properties": {"project": string, "name": string}, "required": ["project", "name"]}, self._rename_project),
            AITool("update_project", "Update a project setting. Fields: architecture, releng_directory, snapshot, latest_snapshot, packages_directory, toolset. For a project Catalyst toolset, first call list_options(project_toolsets) with context.project set to this exact project ID, then pass one of its exact IDs or 'default' to restore the app default. For snapshots, 'latest' selects the latest snapshot. Changing architecture while the project has an incompatible binary-packages folder asks in chat whether to keep it, select a compatible folder, or create and assign a new project-compatible folder.",
                   {"type": "object", "properties": {"project": string,
                    "field": {"type": "string", "enum": ["architecture", "releng_directory", "snapshot", "latest_snapshot", "packages_directory", "toolset"]},
                    "value": {"type": ["string", "boolean"]}}, "required": ["project", "field", "value"]}, self._update_project),
            AITool("check_project_source_updates", "Check whether a project created from a template has upstream template changes, or report the app's cached remote-update state for a Git project. Also reports local edits and whether an update can be started. Read-only; external template checks may clone into a temporary directory but never modify the project.",
                   {"type": "object", "properties": {"project": string}, "required": ["project"]},
                   self._check_project_source_updates),
            AITool("update_project_template", "Update a project created from a template through chat. Loads the latest template options and asks in-chat for changed settings and conflict resolutions. Applies the update once those choices are resolved; a mandatory final confirmation is retained only when drop_project_changes=true would discard saved commits. Preserves saved project commits by default. Refuses unsaved edits, merge histories, or unresolved preparation errors. Optional update_overlays updates the overlays used by this project's stages first, matching the project details workflow. If omitted and overlays are linked, the tool asks whether to update them (preselected yes); overlay errors are reported while project update continues.",
                   {"type": "object", "properties": {"project": string, "update_overlays": {"type": "boolean"}, "drop_project_changes": {"type": "boolean"}}, "required": ["project"]},
                   self._update_project_template),
            AITool("update_project_repository", "Review and update a project cloned from Git in chat. Fetches and compares the upstream branch and asks how to resolve each project/upstream conflict. Applies the update once those choices are resolved; a mandatory final confirmation is retained only when drop_project_changes=true would discard saved commits. Preserves committed project changes by replaying them. Refuses unsaved edits or merge histories. Optional update_overlays updates the overlays used by this project's stages first, matching the project details workflow. If omitted and overlays are linked, the tool asks whether to update them (preselected yes); overlay errors are reported while project update continues.",
                   {"type": "object", "properties": {"project": string, "update_overlays": {"type": "boolean"},
                    "drop_project_changes": {"type": "boolean", "description": "Discard saved project commits during update; requires explicit final confirmation."}},
                    "required": ["project"]}, self._update_project_repository),
            AITool("create_stage", "Start creating a project stage in chat following the stage-add flow. Use exact choices from list_options: project_stage_toolsets for toolset, stage_targets for target, project_stages for the seed parent, and stage_releng_templates for an optional Releng spec. Select a Catalyst-capable toolset; it becomes the project's preferred toolset. Every target except stage1 requires a parent stage. Returns in_progress; use list_operations to confirm completion. This creates stage configuration and its initial baseline; it does not start a build.",
                   {"type": "object", "properties": {"project": string, "name": string, "target": string,
                    "parent": {"type": "string", "description": "Seed parent stage name or ID; required except for stage1."},
                    "toolset": {"type": "string", "description": "Catalyst-capable toolset ID from project_stage_toolsets; omitted uses the project's selected/default toolset."},
                    "releng_template": {"type": "string", "description": "Optional exact Releng spec path from stage_releng_templates."}},
                    "required": ["project", "name", "target"]}, self._create_stage),
            AITool("set_stage_parent", "Change or clear a stage's parent relationship. The parent supplies inherited stage settings. Use list_objects(type='project') to see stage IDs and current parents; choose a parent by exact name or ID. Clearing the parent can change effective settings. This only edits configuration and does not start a build.",
                   {"type": "object", "properties": {"project": string, "stage": string,
                    "action": {"type": "string", "enum": ["set", "clear"]}, "parent": string},
                    "required": ["project", "stage", "action"]}, self._set_stage_parent),
            AITool("update_stage", "Change a stage name or supported text setting: target, version_stamp, subarch, rel_type, interpreter, chost, cbuild, cflags, cxxflags, fcflags, fflags, asflags, common_flags, binrepo_path, portage_prefix, snapshot_treeish, source_subpath, update_seed_command, rename_regexp.",
                   {"type": "object", "properties": {"project": string, "stage": string,
                    "field": string, "value": string}, "required": ["project", "stage", "field", "value"]}, self._update_stage),
            AITool("set_stage_setting", "Set a Catalyst stage setting using its actual type. First call list_options(stage_settings) and, for selectable values, stage_setting_options with context.project, context.stage, and context.setting. Pass the exact option value returned. Use a string for text, a string array for lists/multiselect, a boolean for booleans, and an array of option values for multiselect. Automatic values use @inherit_parent, @inherit_releng_template, or @automatic when listed. For settings that combine inherited and custom values, pass both in one array.",
                   {"type": "object", "properties": {"project": string, "stage": string, "setting": string,
                    "value": {"type": ["string", "boolean", "array"], "items": {"type": "string"}}},
                    "required": ["project", "stage", "setting", "value"]}, self._set_stage_setting),
            AITool("reset_stage_setting", "Remove a stage's explicit override so its normal default or inheritance applies again. This is different from setting an explicit empty value.",
                   {"type": "object", "properties": {"project": string, "stage": string, "setting": string},
                    "required": ["project", "stage", "setting"]}, self._reset_stage_setting),
            AITool("remove_unsupported_stage_setting", "Permanently remove one saved stage value that is not supported by its selected Catalyst target; requires in-chat confirmation. First inspect it with get_stage_settings and pass its exact name from unsupported_settings. Never use this for a supported setting; use reset_stage_setting instead.",
                   {"type": "object", "properties": {"project": string, "stage": string, "setting": string},
                    "required": ["project", "stage", "setting"]}, self._remove_unsupported_stage_setting),
            AITool("read_stage_script", "Read a stage filesystem script stored inside the project's stage directory. The exact setting values are `stage4/fsscript` or `livecd/fsscript` (not the internal names `stage4_fsscript`/`livecd_fsscript`). Returns the configured path and script text; does not expose scripts stored outside the project.",
                   {"type": "object", "properties": {"project": string, "stage": string,
                    "setting": {"type": "string", "enum": ["stage4/fsscript", "livecd/fsscript"]}},
                    "required": ["project", "stage", "setting"]}, self._read_stage_script),
            AITool("write_stage_script", "Create or replace a project-owned Catalyst stage filesystem script. The exact setting values are `stage4/fsscript` or `livecd/fsscript` (not the internal names `stage4_fsscript`/`livecd_fsscript`). The script is saved under that project's stage directory and configured on the stage; it never runs the script or starts a build. Replacing an existing script always requires a separate in-chat confirmation; a tool argument cannot substitute for that confirmation. Include the complete intended script content.",
                   {"type": "object", "properties": {"project": string, "stage": string,
                    "setting": {"type": "string", "enum": ["stage4/fsscript", "livecd/fsscript"]},
                    "content": {"type": "string", "maxLength": 100000}},
                    "required": ["project", "stage", "setting", "content"]}, self._write_stage_script),
            AITool("delete_stage", "Permanently remove a stage configuration from a project. Always use only when explicitly requested; the tool asks for confirmation. Child stages are preserved and detached from this parent.",
                   {"type": "object", "properties": {"project": string, "stage": string}, "required": ["project", "stage"]}, self._delete_stage),
            AITool("read_object_file", "Read a text file inside a registered project, overlay, or Releng object. Paths must be relative to that object.",
                   {"type": "object", "properties": {"type": string, "name": string, "path": string}, "required": ["type", "name", "path"]}, self._read_object_file),
            AITool("list_object_files", "List paths inside a registered project, overlay, or Releng directory so you can find files before reading them with read_object_file. Read-only; does not follow symlinks or enter .git. Results are bounded.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["project", "overlay", "releng"]},
                    "name": string, "path": {"type": "string", "description": "Optional directory path relative to the object root."},
                    "recursive": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}},
                    "required": ["type", "name"]}, self._list_object_files),
            AITool("write_object_file", "Create or replace a text file inside a registered project, overlay, or Releng directory. Use list_object_files to find the path. The parent directory must already exist; Git internals and symlinks are blocked. Creates a new file directly when explicitly requested; replacing an existing file requires confirmation.",
                   {"type": "object", "properties": {"type": {"type": "string", "enum": ["project", "overlay", "releng"]},
                    "name": string, "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string", "maxLength": 100000}},
                    "required": ["type", "name", "path", "content"]}, self._write_object_file),
            AITool("list_builds", "List a project's saved stage build records, matching Builds history. Use status_filter='all', 'completed', or 'failed_canceled' (the latter also includes stopped, skipped, interrupted, and not-started stages). Page the filtered results with offset and limit; use exact timestamps and stage IDs from these rows for build details or deletion.",
                   {"type": "object", "properties": {
                       "project": string,
                       "status_filter": {"type": "string", "enum": ["all", "completed", "failed_canceled"], "default": "all"},
                       "offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
                    "required": ["project"]}, self._list_builds),
            AITool("list_deployable_builds", "List completed stage builds and live ISO builds that can be deployed from the Deploy section. Can inspect one project or all projects. Includes artifact path and Raspberry Pi ISO eligibility; this only reads metadata and never starts deployment. Results are paged; pass the returned next_offset to reach older builds.",
                   {"type": "object", "properties": {"project": string,
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
                    "required": []}, self._list_deployable_builds),
            AITool("create_live_iso", "Save a completed live ISO build to a new .iso file directly from chat. Use list_deployable_builds to get exact project and build timestamp. Optionally prepare it for Raspberry Pi when the build supports it; raspberry_pi_settings mirrors the wizard's serial_console, extra_arguments, config_lines, and older_models_firmware controls (omitted fields use UI defaults). Customize the live system with root password, SSH, local SSH keys, Wi-Fi, and localization. Load live_iso_options for exact SSH key IDs. Never pass passwords in arguments; use the secure in-chat form. Wi-Fi credentials are baked into the generated ISO and can be read by anyone possessing it; mention this when requesting Wi-Fi credentials. This never writes to a disk device. Call directly when the path and choices are ready; it starts without another permission prompt and refuses to overwrite existing files.",
                   {"type": "object", "properties": {"project": string, "build_timestamp": string,
                    "output_path": string, "raspberry_pi": {"type": "boolean"},
                    "raspberry_pi_settings": raspberry_pi_settings,
                    "set_root_password": {"type": "boolean"}, "enable_ssh": {"type": "boolean"},
                    "ssh_key_ids": {"type": "array", "items": string},
                    "wifi": {"type": "object", "properties": {"ssid": string, "hidden": {"type": "boolean"},
                        "country": string, "password_required": {"type": "boolean"}}},
                    "timezone": string, "locales": {"type": "array", "items": string}, "keymap": string},
                    "required": ["project", "build_timestamp", "output_path"]}, self._create_live_iso),
            AITool("deploy_live_iso_to_disk", "Write a completed live ISO build to a USB drive or SD card connected to this computer (Linux only). Use list_deployable_builds for the exact project and build timestamp. The tool discovers writable disks, asks the user to select one if disk is omitted, and presents the required in-chat confirmation naming the exact disk immediately before erasing and writing it. Optionally prepare it for Raspberry Pi firmware; raspberry_pi_settings mirrors the wizard's serial_console, extra_arguments, config_lines, and older_models_firmware controls (omitted fields use UI defaults), and their summary appears in the erase confirmation. Customize the live system with root password, SSH, local SSH keys, Wi-Fi, and localization. Load live_iso_options for exact SSH key IDs. Never pass passwords in arguments; use the secure in-chat form. Wi-Fi credentials are baked into the ISO and can be read by anyone possessing it; mention this when requesting Wi-Fi credentials. Requires OS root authorization. Never open the deployment wizard.",
                   {"type": "object", "properties": {"project": string, "build_timestamp": string,
                    "disk": string, "raspberry_pi": {"type": "boolean"},
                    "raspberry_pi_settings": raspberry_pi_settings,
                    "set_root_password": {"type": "boolean"}, "enable_ssh": {"type": "boolean"},
                    "ssh_key_ids": {"type": "array", "items": string},
                    "wifi": {"type": "object", "properties": {"ssid": string, "hidden": {"type": "boolean"},
                        "country": string, "password_required": {"type": "boolean"}}},
                    "timezone": string, "locales": {"type": "array", "items": string}, "keymap": string},
                    "required": ["project", "build_timestamp"]}, self._deploy_live_iso_to_disk),
            AITool("create_stage_image", "Install a completed stage archive into a new disk image file directly from chat. Use list_deployable_builds and list_options(stage_image_options) first, then collect the output path, firmware, bootloader, and kernel. Optional partition_layout is an ordered array of up to 16 partitions; each item has type (efi, bios-boot, linux, swap, firmware), size_mib (positive integer, or null only for the final partition to use remaining space), optional filesystem (ext4, xfs, btrfs, vfat), and optional mount_point. The layout must contain exactly one final ext4 Linux / partition using remaining image space, plus firmware-required partitions. Omit it to use the app default. Optional settings configure hostname, network service or static network, Wi-Fi, users/groups, SSH keys, locale, Gentoo repository, partition table, image compression, and firmware package. If repository is omitted, the app selects the latest snapshot when packages must be installed and no repository otherwise. Never include passwords in tool arguments: request them through the secure in-chat form shown by the tool. This creates a new file and never writes a disk device. Call directly when ready; it starts without another permission prompt and refuses to overwrite existing files.",
                   {"type": "object", "properties": {"project": string, "build_timestamp": string,
                    "output_path": string, "firmware": string, "bootloader": string, "kernel": string,
                    "partition_table": {"type": "string", "enum": ["gpt", "dos"]},
                    "partition_layout": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
                        "type": {"type": "string", "enum": ["efi", "bios-boot", "linux", "swap", "firmware"]},
                        "size_mib": {"type": ["integer", "null"], "minimum": 1},
                        "filesystem": {"type": ["string", "null"], "enum": ["ext4", "xfs", "btrfs", "vfat", None]},
                        "mount_point": {"type": ["string", "null"]}}, "required": ["type", "size_mib"]}},
                    "compress": {"type": "boolean"}, "hostname": string,
                    "network_service": string, "static_network": {"type": "object", "properties": {
                        "interface": string, "address": string, "gateway": string, "dns": string}},
                    "wifi": {"type": "object", "properties": {"ssid": string, "hidden": {"type": "boolean"},
                        "country": string, "password_required": {"type": "boolean"}}},
                    "users": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
                        "name": string, "full_name": string, "groups": {"type": "array", "items": string},
                        "password_required": {"type": "boolean"}}, "required": ["name"]}},
                    "set_root_password": {"type": "boolean"}, "enable_ssh": {"type": "boolean"},
                    "ssh_key_ids": {"type": "array", "items": string}, "repository": string,
                    "install_firmware": {"type": "boolean"}, "timezone": string,
                    "locales": {"type": "array", "items": string}, "keymap": string},
                    "required": ["project", "build_timestamp", "output_path", "firmware", "bootloader", "kernel"]}, self._create_stage_image),
            AITool("deploy_stage_to_target", "Install a completed stage archive directly to a physical disk of this computer (Linux only) or to a machine connected through SSH. Read list_options(stage_image_options) for supported boot, kernel, network service, groups, repository, and SSH-key choices. Set firmware only for a local disk target; firmware is detected for SSH targets and must not be passed. Optional bootloader, kernel, network_service, hostname, static_network (interface/address/gateway/dns), timezone, locales, keymap, users (up to 8 with exact available groups), set_root_password, enable_ssh, ssh_key_ids, wifi, repository, install_firmware, and reboot configure the installed system; omitted values use app defaults. If packages must be installed and repository is omitted, the app selects the latest Gentoo snapshot; explicitly choosing no repository in that case is rejected. Passwords for root/users/Wi-Fi are requested through secure in-chat forms and never passed as tool arguments. Optional partition_table is gpt or dos; partition_layout is an ordered array of up to 16 partitions using type (efi, bios-boot, linux, swap, firmware), positive size_mib or null only for the final partition to use remaining space, optional filesystem (ext4, xfs, btrfs, vfat), and optional mount_point. The layout must contain exactly one Linux / partition and satisfy firmware and filesystem constraints; omit it for the app default. It WILL erase the selected disk: an in-chat confirmation naming the exact target, disk, layout, and system choices is required immediately before starting. Local targets require OS root authorization; SSH passwords are collected only through a secure in-chat field. Never pass passwords as tool arguments. Do not open the deployment wizard.",
                   {"type": "object", "properties": {
                       "project": string, "build_timestamp": string,
                       "target_method": {"type": "string", "enum": ["local_disk", "ssh"]},
                       "firmware": {"type": "string", "enum": ["uefi", "bios", "ps3", "petitboot", "raspberry-pi", "other"]},
                       "host": string, "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                       "user": string, "disk": string,
                       "bootloader": string, "kernel": string, "hostname": string,
                       "network_service": string, "static_network": {"type": "object", "properties": {
                           "interface": string, "address": string, "gateway": string, "dns": string}},
                       "timezone": string, "locales": {"type": "array", "items": string}, "keymap": string,
                       "users": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
                           "name": string, "full_name": string, "groups": {"type": "array", "items": string},
                           "password_required": {"type": "boolean"}}, "required": ["name"]}},
                       "set_root_password": {"type": "boolean"}, "enable_ssh": {"type": "boolean"},
                       "ssh_key_ids": {"type": "array", "items": string},
                       "wifi": {"type": "object", "properties": {"ssid": string,
                           "hidden": {"type": "boolean"}, "country": string,
                           "password_required": {"type": "boolean"}}},
                       "repository": {"type": "string", "enum": ["none", "build-snapshot", "latest"]},
                       "install_firmware": {"type": "boolean"}, "reboot": {"type": "boolean"},
                       "partition_table": {"type": "string", "enum": ["gpt", "dos"]},
                       "partition_layout": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
                           "type": {"type": "string", "enum": ["efi", "bios-boot", "linux", "swap", "firmware"]},
                           "size_mib": {"type": ["integer", "null"], "minimum": 1},
                           "filesystem": {"type": ["string", "null"], "enum": ["ext4", "xfs", "btrfs", "vfat", None]},
                           "mount_point": {"type": ["string", "null"]}}, "required": ["type", "size_mib"]}}},
                    "required": ["project", "build_timestamp"]}, self._deploy_stage_to_target),
            AITool("list_repository_packages", "Browse ebuild packages and available versions in a Portage snapshot or overlay, matching the app's Packages tab. Search category/package names and page through bounded results with query, offset, and limit.",
                   {"type": "object", "properties": {
                       "repository_type": {"type": "string", "enum": ["snapshot", "overlay"]},
                       "repository": string, "query": string,
                       "offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                       "include_versions": {"type": "boolean"}},
                    "required": ["repository_type", "repository"]}, self._list_repository_packages),
            AITool("package_cache_stats", "Inspect a registered binary Packages cache like its details page: total on-disk size, project links, and size plus artifact counts for each release-type folder. Optionally include package CPVs capped by limit. Counts CPV records in Portage Packages indexes, not distinct compile attempts.",
                   {"type": "object", "properties": {
                       "packages_directory": string, "release_type": string,
                       "include_packages": {"type": "boolean"}, "limit": {"type": "integer"}},
                    "required": ["packages_directory"]}, self._package_cache_stats),
            AITool("list_library_storage", "Inspect Library storage usage and known items. Sections: overview, cache, temporary, packages, snapshots, projects, builds. Results are capped by limit; use offset to page through every item.",
                   {"type": "object", "properties": {
                       "section": {"type": "string", "enum": ["overview", "cache", "temporary", "packages", "snapshots", "projects", "builds"]},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                       "offset": {"type": "integer", "minimum": 0}}, "required": ["section"]}, self._list_library_storage),
            AITool("delete_library_storage_item", "Delete one exact recognized Cache or Temporary item returned by list_library_storage. Use only when explicitly asked and confirm in chat. Cache deletion uses the same item-specific active-use checks as the Library UI; Temporary cleanup is blocked while app operations, VMs, or runner use are active.",
                   {"type": "object", "properties": {"section": {"type": "string", "enum": ["cache", "temporary"]},
                    "category": string, "relative_path": string}, "required": ["section", "category", "relative_path"]}, self._delete_library_storage_item),
            AITool("list_build_files", "List paths inside one saved build folder, including collected failure files shown by the Bugs screen. Pass the exact project and timestamp from list_builds; results are read-only, paged, and never follow symlinks. Scans at most 10,000 entries and reports scan_limit_reached. Use read_build_file for selected text files.",
                   {"type": "object", "properties": {"project": string, "timestamp": string,
                    "path": {"type": "string", "description": "Optional directory relative to the build folder; omit for its root."},
                    "recursive": {"type": "boolean", "default": False},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 9500},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}},
                    "required": ["project", "timestamp"]}, self._list_build_files),
            AITool("read_build_file", "Read a text file inside a project's stored build record, for example failure/package/build.log. Use list_build_files to discover paths. Large files return their end.",
                   {"type": "object", "properties": {"project": string, "timestamp": string, "path": string}, "required": ["project", "timestamp", "path"]}, self._read_build_file),
            AITool("get_build_run", "Read the saved step-by-step summary and bounded output from one completed build run, like the Builds view. Get the exact timestamp from list_builds. For a running build use list_operations and read_operation_log.",
                   {"type": "object", "properties": {"project": string, "timestamp": string,
                    "lines_per_step": {"type": "integer", "minimum": 1, "maximum": 100, "default": 40}},
                    "required": ["project", "timestamp"]}, self._get_build_run),
            AITool("preview_build_plan", "Preview the Build wizard's dependency plan for selected project stages without starting anything. Pass stage IDs from list_options(project_stages). Reports parent stages that must be rebuilt and usable prior parent artifacts that will be reused, in build order. Optionally pass an exact toolset ID from list_options(build_toolsets) to check required Catalyst settings for stages in the plan, matching the wizard's warnings. This is a planning/readiness preview only; it does not guarantee the build will succeed.",
                   {"type": "object", "properties": {"project": string,
                    "stages": {"type": "array", "items": string, "minItems": 1}, "toolset": string},
                    "required": ["project", "stages"]}, self._preview_build_plan),
            AITool("start_build", "Start building project stages directly from chat without an additional permission prompt. If another project build is active, this queues the requested build and returns status=queued; if this same project is already building, it fails. First select an existing project from list_options(projects); never invent a project name or ID. Then load project_stages, build_toolsets, build_runners, and build_snapshots with context.project set to that exact project ID. Use only the exact values returned. Use preview_build_plan when it helps explain parent-stage rebuilds or reuse before starting. If the user delegates the build or asks to start it without further questions, select all project stages unless they narrowed the scope, and use a sole available option or the returned default for other required choices; ask only when a required choice remains genuinely ambiguous and has no default. Never guess IDs or values such as gcc, local, or latest. Never set cleanup_previous_builds true unless the user asked to clean up prior build records. Set update_project_snapshot true only when the user asks to change the project default; otherwise leave it unchanged. Never open a wizard.",
                   {"type": "object", "properties": {
                    "project": {**string, "description": "Exact project ID returned by list_options(projects)."},
                    "stages": {"type": "array", "items": {**string, "description": "Exact stage ID returned by list_options(project_stages) with context.project set to project."}, "minItems": 1,
                               "description": "One or more exact stage IDs from list_options(project_stages) for this project."},
                    "toolset": {**string, "description": "Exact toolset ID or default returned by list_options(build_toolsets) for this project."},
                    "runner": {**string, "description": "Exact runner ID returned by list_options(build_runners) for this project."},
                    "snapshot": {**string, "description": "Exact snapshot value returned by list_options(build_snapshots) for this project."},
                    "cleanup_previous_builds": {"type": "boolean"},
                    "update_project_snapshot": {"type": "boolean"}},
                    "required": ["project", "stages", "toolset", "runner", "snapshot"]}, self._start_build),
            AITool("stop_build", "Cancel a currently running build for a project. Use only when the user explicitly asks to stop or cancel it.",
                   {"type": "object", "properties": {"project": string}, "required": ["project"]}, self._stop_build),
            AITool("start_vm", "Run a completed stage or live ISO build in the app's VM runtime without opening the setup wizard. Use list_deployable_builds for the exact project and build timestamp. Backend defaults to the best available app backend; CPU, memory and networking use app defaults unless specified. Stages without a boot kernel cannot be launched this way and need a kernel first. This opens the live VM view so the user can interact with it; list_operations/stop_operation manage the session.",
                   {"type": "object", "properties": {
                       "project": string, "timestamp": string,
                       "backend": {"type": "string", "enum": ["auto", "qemu", "apple"]},
                       "cpus": {"type": "integer", "minimum": 1},
                       "memory_gib": {"type": "integer", "minimum": 1},
                       "network_enabled": {"type": "boolean"}},
                    "required": ["project", "timestamp"]}, self._start_vm),
            AITool("create_snapshot", "Generate a Portage snapshot directly from chat using a Catalyst-capable toolset and supported runner. First call list_options for toolsets and runners, then use an exact returned value; 'default' selects the app's default toolset/runner. Ask only if a required choice has no usable default and multiple options remain. Once ready, it starts without another in-chat permission prompt. Never open a wizard.",
                   {"type": "object", "properties": {"toolset": string, "runner": string},
                    "required": ["toolset", "runner"]}, self._create_snapshot),
            AITool("import_snapshot", "Import an existing local .sqfs Portage snapshot into the Library. Use an exact absolute file path supplied by the user. It starts without another permission prompt and refuses to overwrite an existing snapshot.",
                   {"type": "object", "properties": {"source_path": string},
                    "required": ["source_path"]}, self._import_snapshot),
        ]

    @staticmethod
    def _json(value) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, indent=2)

    @staticmethod
    def _partition_layout_from_arguments(raw):
        """Convert the chat tool's MiB partition description into validated app objects."""
        if raw is None:
            return None
        from .deploy_target import MIB, PartitionSpec, PartitionType, TargetFilesystem

        if not isinstance(raw, list) or len(raw) > 16:
            raise ValueError("partition_layout must be an array of at most 16 partitions")
        result = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Partition {index} must be an object")
            try:
                partition_type = PartitionType(item.get("type"))
            except (TypeError, ValueError) as error:
                raise ValueError(f"Partition {index} has an unsupported type") from error
            size_mib = item.get("size_mib")
            if size_mib is not None and (type(size_mib) is not int or size_mib < 1):
                raise ValueError(f"Partition {index} size_mib must be a positive integer or null")
            filesystem_value = item.get("filesystem")
            try:
                filesystem = TargetFilesystem(filesystem_value) if filesystem_value is not None else None
            except (TypeError, ValueError) as error:
                raise ValueError(f"Partition {index} has an unsupported filesystem") from error
            mount_point = item.get("mount_point")
            if mount_point is not None and not isinstance(mount_point, str):
                raise ValueError(f"Partition {index} mount_point must be a string or null")
            result.append(PartitionSpec(partition_type, size_mib * MIB if size_mib is not None else None,
                                        filesystem, mount_point))
        return result

    def _list_documentation(self, _arguments):
        return self._json(self.documentation_catalog())

    def _list_tools(self, arguments):
        query = str(arguments.get("query", "")).strip().lower()
        try:
            offset = int(arguments.get("offset", 0))
            limit = int(arguments.get("limit", 12))
        except (TypeError, ValueError) as error:
            raise ValueError("offset and limit must be whole numbers") from error
        if not 0 <= offset <= 10_000:
            raise ValueError("offset must be between 0 and 10000")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        tools = [tool for tool in self.general_tools()
                 if tool.name not in {"list_tools", "read_tool_schema"}]
        if query:
            terms = [term for term in re.findall(r"[a-z0-9_]+", query) if len(term) > 1]
            ranked = []
            for tool in tools:
                name = tool.name.lower()
                description = tool.description.lower()
                score = sum((3 if term in name else 1 if term in description else 0)
                            for term in terms)
                if score:
                    ranked.append((score, tool))
            ranked.sort(key=lambda item: (-item[0], item[1].name))
            tools = [tool for _, tool in ranked]
        total = len(tools)
        selected = tools[offset:offset + limit]
        next_offset = offset + len(selected) if offset + len(selected) < total else None
        return self._json({
            "query": query or None,
            "total_matches": total,
            "offset": offset,
            "returned_count": len(selected),
            "next_offset": next_offset,
            "truncated": next_offset is not None,
            "tools": [{"name": tool.name,
                       "description": (tool.description[:180].rsplit(" ", 1)[0] + "…"
                                       if len(tool.description) > 180 else tool.description)}
                      for tool in selected],
            "next": "Use next_offset with the same query to continue; call read_tool_schema with a returned name before using it.",
        })

    def _read_tool_schema(self, arguments):
        name = str(arguments.get("name", "")).strip()
        tool = next((item for item in self.general_tools() if item.name == name), None)
        if tool is None or name in {"list_tools", "read_tool_schema"}:
            raise ValueError(f"Unknown app tool: {name or '(empty)'}")
        return self._json(tool.ollama_schema()["function"])

    def _read_documentation(self, arguments):
        item = AI_FLOWS.documentation(str(arguments.get("id", "")))
        if item is None:
            raise ValueError("Unknown documentation id")
        return f"# {item.title}\n\n{item.content}"

    def _list_ollama_models(self, _arguments):
        from .repositories import Repository
        from .ollama_client import OllamaClient, redact_server_url
        settings = Repository.Settings.value
        client = OllamaClient(settings.ollama_url, timeout=8)
        models = client.list_models()
        server, redacted = redact_server_url(settings.ollama_url)
        return self._json({
            "server": server,
            "server_url_redacted": redacted,
            "selected_model": settings.ollama_model,
            "models": [{"name": item.get("name"), "size_bytes": item.get("size"),
                        "modified_at": item.get("modified_at"),
                        "family": (item.get("details") or {}).get("family"),
                        "parameter_size": (item.get("details") or {}).get("parameter_size"),
                        "quantization": (item.get("details") or {}).get("quantization_level")}
                       for item in models if isinstance(item, dict) and item.get("name")],
        })

    def _open_ollama_model_catalog(self, _arguments):
        def action():
            from gi.repository import Gtk
            parent = self.content_navigation_view.get_root() if self.content_navigation_view else None
            if parent is None:
                application = Gtk.Application.get_default()
                parent = application.get_active_window() if application else None
            if parent is None:
                raise RuntimeError("The app window is unavailable for opening the Ollama model catalog")
            url = "https://ollama.com/library"
            Gtk.UriLauncher.new(url).launch(parent, None, None)
            return self._json({"status": "opened", "url": url})
        return self._on_main_thread(action)

    def _pull_ollama_model(self, arguments):
        from .repositories import Repository
        from .ollama_client import OllamaClient
        model = str(arguments.get("model", "")).strip()
        if not model or len(model) > 200 or any(character in model for character in "\r\n\0"):
            raise ValueError("Provide a valid Ollama model name (1 to 200 characters)")
        settings = Repository.Settings.value
        event_count = 0
        latest = {"status": "Starting download"}

        def report(event):
            nonlocal event_count
            event_count += 1
            latest.update(event)
            if self.tool_progress_callback is not None:
                self.tool_progress_callback(model, event)

        client = OllamaClient(settings.ollama_url, timeout=60,
                              cancel_event=self.cancel_event)
        self._active_tool_client = client
        try:
            client.pull_model(model, progress=report)
        finally:
            if self._active_tool_client is client:
                self._active_tool_client = None
        return self._json({"status": "completed", "model": model,
                           "events_received": event_count,
                           "last_status": latest.get("status", "success")})

    def _list_objects(self, arguments):
        from .repositories import Repository
        groups = {
            "project": Repository.ProjectDirectory.value,
            "toolset": Repository.Toolset.value,
            "snapshot": Repository.Snapshot.value,
            "releng": Repository.RelengDirectory.value,
            "overlay": Repository.OverlayDirectory.value,
            "packages": Repository.PackagesDirectory.value,
            "runner": Repository.Runner.value,
        }
        from .runner import HOST_RUNNER, runner_is_supported
        runner_items = [HOST_RUNNER, *Repository.Runner.value]
        requested = str(arguments.get("type", "")).strip().lower()
        if requested and requested not in groups:
            raise ValueError("Unknown object type")
        selected = {requested: (runner_items if requested == "runner" else groups[requested])} if requested else {
            **groups, "runner": runner_items,
        }
        result = []
        for kind, items in selected.items():
            for item in items:
                row = {"type": kind, "name": getattr(item, "name", str(item)),
                       "id": str(getattr(item, "id", getattr(item, "uuid", "")))}
                if hasattr(item, "short_details"):
                    row["details"] = item.short_details
                if kind == "runner":
                    row["runner_type"] = getattr(getattr(item, "runner_type", None), "value", None)
                    row["supported"] = runner_is_supported(item)
                    row["is_used"] = bool(getattr(item, "is_used", False))
                    row["users"] = list(getattr(item, "users", []))
                    status = getattr(item, "status", None)
                    row["state"] = ("active" if item is HOST_RUNNER else
                                    str(status).lower() if status is not None else
                                    "unknown")
                if kind == "packages":
                    from .status import StatusIndicatorState
                    row["architecture"] = item.architecture.value if item.architecture else None
                    row["cpu_flags"] = item.cpu_flags
                    project_rows = []
                    for project in item.projects:
                        details = item.compatibility(project)
                        project_rows.append({
                            "name": project.name,
                            "compatibility": [{"severity": detail.state.name.lower(),
                                               "message": detail.text} for detail in details],
                            "usable": not any(detail.state == StatusIndicatorState.ERROR
                                              for detail in details),
                        })
                    row["projects"] = project_rows
                if hasattr(item, "stages"):
                    row["stages"] = [{"id": str(stage.id), "name": stage.name,
                                      "target": getattr(stage, "target", None),
                                      "parent": str(stage.parent) if getattr(stage, "parent", None) else None}
                                     for stage in item.stages]
                    configuration = {
                        "architecture": item.get_architecture().value if item.get_architecture() else None,
                        "releng_directory": item.get_releng_directory().name if item.get_releng_directory() else None,
                        "snapshot": ("latest" if item.uses_latest_snapshot else
                                     item.get_snapshot().name if item.get_snapshot() else None),
                        "packages_directory": (item.get_packages_directory().name
                                               if item.get_packages_directory() else None),
                    }
                    from . import project_toolsets
                    from .toolset_application import ToolsetApplication
                    from .toolset_selection import default_toolset, sorted_toolsets, toolset_has_applications
                    available_toolsets = [toolset for toolset in sorted_toolsets()
                                          if toolset_has_applications(
                                              toolset, [ToolsetApplication.CATALYST])
                                          and os.path.isfile(toolset.file_path())]
                    preferred_id = project_toolsets.get(item.directory_path())
                    selected_toolset = next(
                        (toolset for toolset in available_toolsets
                         if str(toolset.uuid) == str(preferred_id)), None)
                    selection_source = "project"
                    if selected_toolset is None:
                        selected_toolset = (default_toolset(
                            [ToolsetApplication.CATALYST], available_toolsets) or
                            (available_toolsets[0] if available_toolsets else None))
                        selection_source = "app_default" if selected_toolset else "unavailable"
                    configuration["catalyst_toolset"] = ({
                        "id": str(selected_toolset.uuid),
                        "name": selected_toolset.name,
                        "selection_source": selection_source,
                    } if selected_toolset else None)
                    row["configuration"] = configuration
                result.append(row)
        return self._json(result)

    @classmethod
    def _collect_option_pairs(cls, value):
        pairs = set()
        if isinstance(value, dict):
            if "value" in value and "label" in value:
                pairs.add((str(value["value"]), str(value["label"])))
            for nested in value.values():
                pairs.update(cls._collect_option_pairs(nested))
        elif isinstance(value, list):
            for nested in value:
                pairs.update(cls._collect_option_pairs(nested))
        return pairs

    def _list_options(self, arguments):
        from .architecture import Architecture
        from .repositories import Repository
        from .toolset import ToolsetEnv
        from .toolset_application import ToolsetApplication
        from .toolset_selection import default_toolset, sorted_toolsets, toolset_has_applications
        from .app_section import AppSection

        categories = arguments.get("categories", [])
        if not isinstance(categories, list) or not categories:
            raise ValueError(
                "list_options requires a non-empty JSON array in `categories` (not `query`). "
                "Read its schema with read_tool_schema, then call for example: "
                "list_options({\"categories\":[\"project_sources\",\"architectures\"]}).")
        context = arguments.get("context") or {}
        known = {
            "project_sources", "architectures", "releng_directories", "snapshots",
            "toolsets", "runners", "build_toolsets", "build_runners", "build_snapshots",
            "project_toolsets", "library_toolsets",
            "packages_directories", "projects", "project_stages", "sections",
            "stage_targets", "stage_settings", "stage_setting_options", "stage_kernel_packages",
            "project_stage_toolsets", "stage_releng_templates",
            "project_templates", "template_variables", "git_directory_sources",
            "toolset_stages", "toolset_applications", "stage_image_options", "live_iso_options",
        }
        result = {}
        category_aliases = {
            "project_runners": "build_runners",
            "project_build_toolsets": "build_toolsets",
            "project_build_snapshots": "build_snapshots",
        }
        for category in categories:
            requested_category = str(category)
            # Recover a few predictable near-misses emitted by local models.
            # Keep the canonical key in the result and mirror it under the
            # requested key so callers using the alias can consume the values.
            category = category_aliases.get(requested_category, requested_category)
            category_default_value = None
            if category not in known:
                raise ValueError(
                    f"Unknown option category: {category}. Valid categories: "
                    + ", ".join(sorted(known))
                )
            if category == "project_sources":
                values = [("empty", "Empty project"), ("git", "Clone Git repository"), ("template", "Start from a template")]
            elif category == "project_templates":
                from .project_template import ProjectTemplate, load_app_templates
                templates = load_app_templates()
                values = [(item.repository_path, f"{item.name} — {item.description}" if item.description else item.name)
                          for item in templates if isinstance(item, ProjectTemplate)]
            elif category == "template_variables":
                from .project_template import ProjectTemplate, load_app_templates
                template_key = str(context.get("template") or
                                   self._last_project_form_context.get("template", "")).strip()
                project_name = str(context.get("project_name") or
                                   self._last_project_form_context.get("project_name", ""))
                if not template_key:
                    raise ValueError(
                        "template_variables requires context.template. First call list_options with "
                        "categories=['project_templates'], ask the user to select a returned value, then call "
                        "list_options with categories=['template_variables'] and "
                        "context={'template': '<exact returned value>', 'project_name': '<project name>'}.")
                template = next((item for item in load_app_templates()
                                 if isinstance(item, ProjectTemplate) and
                                 (item.repository_path == template_key or item.name == template_key)), None)
                if template is None:
                    raise ValueError("That project template is no longer available")
                selected = {key.removeprefix("selected."): value for key, value in context.items()
                            if key.startswith("selected.")}
                names = template.resolve(selected, project_name=project_name)
                values = []
                for variable in template.visible_variables(names):
                    options = []
                    if variable.type in ("choice", "architecture", "multiple"):
                        options = [{"value": option.value, "label": option.title,
                                    "default": option.value == variable.default}
                                   for option in variable.available_options(names)]
                    elif variable.type == "boolean":
                        options = [{"value": "true", "label": "Yes",
                                    "default": str(variable.default).lower() == "true"},
                                   {"value": "false", "label": "No",
                                    "default": str(variable.default).lower() == "false"}]
                    values.append({"id": variable.id, "label": variable.title,
                                   "description": variable.description, "type": variable.type,
                                   "options": options, "default": variable.default,
                                   "minimum_selected": variable.minimum_selected})
                result[category] = values
                continue
            elif category == "git_directory_sources":
                kind = str(context.get("type", "")).strip().lower()
                if kind == "releng":
                    values = [("git", "Clone a Git repository")]
                elif kind == "overlay":
                    values = [("git", "Clone a Git repository"),
                              ("local", "Copy a local directory"),
                              ("empty", "Create an empty overlay")]
                else:
                    raise ValueError("Pass context.type as releng or overlay")
            elif category == "toolset_stages":
                from .architecture import Architecture
                from .toolset_env_builder import get_stage3_urls
                architecture_key = str(context.get("architecture", "")).strip()
                architecture = next((item for item in Architecture
                                     if item.value == architecture_key or item.name == architecture_key),
                                    Architecture.HOST if not architecture_key else None)
                if architecture is None:
                    raise ValueError("Choose a supported architecture from list_options(architectures)")
                urls = get_stage3_urls(architecture)
                if isinstance(urls, Exception):
                    raise RuntimeError(f"Couldn't load stage3 archives: {urls}")
                from .toolset_create_view import ToolsetCreateView
                ordered_urls = sorted(urls, key=lambda url: not ToolsetCreateView._is_recommended_stage(
                    os.path.basename(url.path), architecture))
                values = [(item.geturl(), os.path.basename(item.path) +
                           (" — recommended" if ToolsetCreateView._is_recommended_stage(
                               os.path.basename(item.path), architecture) else ""))
                          for item in ordered_urls]
            elif category == "toolset_applications":
                from .toolset import ToolsetApplication
                result[category] = [{
                    "value": app.package,
                    "label": app.name,
                    "description": app.description,
                    "default_selected": not app.auto_select,
                    "default": not app.auto_select,
                    "versions": [{"value": str(version.id), "label": version.name}
                                 for version in app.versions],
                } for app in ToolsetApplication.ALL if not app.auto_select]
                continue
            elif category == "stage_kernel_packages":
                project_key = str(context.get("project", "")).strip()
                stage_key = str(context.get("stage", "")).strip()
                if not project_key or not stage_key:
                    raise ValueError("Pass context.project and context.stage to list stage kernel packages")
                project, stage = self._stage_kernel_context(
                    {"project": project_key, "stage": stage_key})
                from .project_kernel_packages import load_kernel_packages
                from ..project.project_stage_kernels import kernel_setting, stage_kernel_names
                selected_kernel = str(context.get("kernel", "")).strip()
                selected_package = None
                if selected_kernel:
                    if selected_kernel not in stage_kernel_names(project, stage):
                        raise ValueError("context.kernel must be a name from list_stage_kernels")
                    selected_package, _ = kernel_setting(project, stage, selected_kernel, "sources")
                packages = [item for item in load_kernel_packages(project, stage) if item.supported]
                result[category] = [{"value": item.atom,
                                     "label": item.atom,
                                     "description": item.details,
                                     "default": item.atom == selected_package}
                                    for item in packages]
                continue
            elif category == "live_iso_options":
                project_key = str(context.get("project", "")).strip()
                timestamp = str(context.get("build_timestamp", "")).strip()
                if not project_key or not timestamp:
                    raise ValueError("Pass context.project and context.build_timestamp to list live ISO options")
                from .project_build import load_project_builds
                from .deploy_live_iso import is_live_iso_build
                project = self._project(project_key)
                build = next((item for item in load_project_builds(project)
                              if item.timestamp == timestamp), None)
                if build is None or not is_live_iso_build(build):
                    raise ValueError("Choose a completed live ISO from list_deployable_builds")
                from .deploy_system import local_public_keys
                import hashlib
                result[category] = {
                    "ssh_keys": [{
                        "value": hashlib.sha256((key.path + "\0" + key.key).encode()).hexdigest()[:20],
                        "label": f"{key.title} ({key.subtitle})",
                    } for key in local_public_keys()],
                }
                continue
            elif category == "stage_image_options":
                from .project_build import load_project_builds
                from .deploy_installation import is_deployable
                from .deploy_installation import DeployRepository, DEFAULT_USER_GROUPS, USER_GROUPS
                from .deploy_target import TargetFirmware, PartitionTable, default_image_layout, machine_architecture
                from .deploy_boot import Bootloader, Kernel, StageContents, default_bootloader
                from .deploy_system import NetworkService, default_network_service, local_public_keys
                import hashlib
                project_key = str(context.get("project", "")).strip()
                timestamp = str(context.get("build_timestamp", "")).strip()
                if not project_key or not timestamp:
                    raise ValueError("Pass context.project and context.build_timestamp")
                project = self._project(project_key)
                build = next((item for item in load_project_builds(project) if item.timestamp == timestamp), None)
                if build is None or not is_deployable(project, build):
                    raise ValueError("Choose a completed stage archive from list_deployable_builds")
                architecture = project.get_architecture()
                machine_arch = machine_architecture(architecture.value if architecture else None)
                if machine_arch is None:
                    raise ValueError("The project's architecture cannot be used to create a disk image")
                contents = StageContents.scan(build.artifact_path)
                firmware_options = TargetFirmware.options(machine_arch)
                requested_firmware = str(context.get("firmware", "")).strip()
                firmware = next((item for item in firmware_options if item.value == requested_firmware),
                                firmware_options[0] if not requested_firmware and firmware_options else None)
                if firmware is None:
                    raise ValueError("Choose firmware from this project's stage_image_options")
                uefi = firmware == TargetFirmware.UEFI
                raspberry_pi = firmware == TargetFirmware.RASPBERRY_PI
                bootloaders = [item for item in Bootloader if item.supported(machine_arch, uefi, False)]
                kernels = [Kernel.DISTRIBUTION_BINARY, Kernel.DISTRIBUTION]
                if Kernel.RASPBERRY_PI.supported(machine_arch):
                    kernels.insert(0 if raspberry_pi else 2, Kernel.RASPBERRY_PI)
                kernels = ([Kernel.STAGE] if contents.kernels else []) + kernels + [Kernel.NONE]
                network_services = [item for item in NetworkService if item.supported(contents.init)
                                    and (item in contents.network_services or item in (NetworkService.DHCPCD, NetworkService.NONE))]
                group_descriptions = dict(USER_GROUPS)
                user_groups = [{"value": group,
                                "label": f"{group} — {group_descriptions.get(group, 'Additional stage group')}"
                                + (" (recommended)" if group in DEFAULT_USER_GROUPS else "")}
                               for group in sorted(contents.groups)]
                ssh_keys = [{"value": hashlib.sha256((key.path + "\0" + key.key).encode()).hexdigest()[:20],
                             "label": f"{key.title} ({key.subtitle})"}
                            for key in local_public_keys()]
                repository_options = [{"value": DeployRepository.NONE.value, "label": "Not installed"},
                                      {"value": DeployRepository.LATEST.value,
                                       "label": "Latest snapshot, downloaded during deployment"}]
                snapshot_file = (((getattr(build, "details", None) or {}).get("inputs") or {})
                                 .get("snapshot") or {}).get("file")
                snapshot = next((item for item in Repository.Snapshot.value
                                 if item.filename == snapshot_file), None) if snapshot_file else None
                if snapshot is not None:
                    repository_options.insert(1, {"value": DeployRepository.BUILD_SNAPSHOT.value,
                                                  "label": f"Snapshot used for this build ({snapshot.name})"})
                recommended_bootloader = default_bootloader(contents, machine_arch, uefi, False, raspberry_pi)
                recommended_kernel = kernels[0]
                recommended_network = default_network_service(contents.network_services, contents.init)
                recommended_firmware_install = (not contents.kernels and not contents.firmware
                                                and not raspberry_pi and not contents.firmware_conflict)
                recommended_needs_packages = (
                    recommended_bootloader.needs_package and recommended_bootloader not in contents.bootloaders
                    or bool(recommended_kernel.package)
                    or bool(recommended_network.package and recommended_network not in contents.network_services)
                    or recommended_firmware_install
                )
                recommended = {
                    "firmware": firmware.value,
                    "bootloader": recommended_bootloader.value,
                    "kernel": recommended_kernel.value,
                    "network_service": recommended_network.value,
                    "partition_table": PartitionTable.GPT.value,
                    "repository": (DeployRepository.LATEST if recommended_needs_packages
                                   else DeployRepository.NONE).value,
                    "compress": True,
                    "install_firmware": recommended_firmware_install,
                }
                result[category] = {
                    "firmware": [{"value": item.value, "label": item.display_name()} for item in firmware_options],
                    "bootloaders": [{"value": item.value,
                                     "label": item.display_name + (" (in stage)" if item in contents.bootloaders else
                                                                    "" if not item.needs_package else " (will be installed)")}
                                    for item in bootloaders],
                    "kernels": [{"value": item.value,
                                 "label": f"{item.display_name} ({', '.join(contents.kernels)})" if item == Kernel.STAGE
                                 else item.display_name} for item in kernels],
                    "network_services": [{"value": item.value, "label": item.display_name +
                                          (" (in stage)" if item in contents.network_services else
                                           " (will be installed)" if item.package else "")}
                                         for item in network_services],
                    "user_groups": user_groups,
                    "ssh_keys": ssh_keys,
                    "repositories": repository_options,
                    "ssh_available_in_stage": contents.ssh_installed,
                    "partition_tables": [{"value": item.value, "label": item.display_name} for item in PartitionTable],
                    "recommended": recommended,
                    "stage_contents": contents.summary,
                }
                for option_group, recommended_key in (
                        ("firmware", "firmware"), ("bootloaders", "bootloader"),
                        ("kernels", "kernel"), ("network_services", "network_service"),
                        ("partition_tables", "partition_table"), ("repositories", "repository")):
                    default = recommended.get(recommended_key)
                    if default is not None:
                        for option in result[category][option_group]:
                            option["default"] = option["value"] == default
                for option in result[category]["user_groups"]:
                    option["default"] = option["value"] in DEFAULT_USER_GROUPS
                continue
            elif category == "architectures":
                values = [(item.value, item.value) for item in sorted(Architecture, key=lambda item: item.value)]
            elif category == "releng_directories":
                values = [(str(item.id), item.name) for item in Repository.RelengDirectory.value]
            elif category == "snapshots":
                values = [("latest", "Latest snapshot")] + [(item.filename, item.name) for item in Repository.Snapshot.value]
            elif category == "build_snapshots":
                project_key = str(context.get("project", "")).strip()
                if not project_key:
                    raise ValueError("Pass context.project to list build snapshots")
                project = self._project(project_key)
                current = project.get_snapshot()
                current_label = "Latest snapshot" if project.uses_latest_snapshot else (
                    current.name if current else "No project snapshot configured")
                values = [("project", f"Project setting — {current_label}"),
                          ("latest", "Fetch latest snapshot before build")]
                values.extend((item.filename, item.name) for item in Repository.Snapshot.value)
            elif category == "build_toolsets":
                project_key = str(context.get("project", "")).strip()
                if not project_key:
                    raise ValueError("Pass context.project to list build toolsets")
                project = self._project(project_key)
                from .toolset_selection import build_toolset_requirements
                requirements = build_toolset_requirements(project)
                values = [(str(item.uuid), item.name) for item in sorted_toolsets(
                    Repository.Toolset.value, requirements)
                    if toolset_has_applications(item, requirements)
                    and getattr(item, "busy_reason", None) is None]
                from .toolset_selection import default_toolset
                default = default_toolset(requirements)
                if default:
                    values.insert(0, ("default", f"App default — {default.name}"))
            elif category == "build_runners":
                from .runner import HOST_RUNNER, runner_is_supported
                values = [("host", f"Host — {HOST_RUNNER.short_details}")] if runner_is_supported(HOST_RUNNER) else []
                values.extend((str(item.id), item.name) for item in Repository.Runner.value
                              if runner_is_supported(item))
            elif category == "toolsets":
                values = [(str(item.uuid), item.name) for item in sorted_toolsets(
                    Repository.Toolset.value, [ToolsetApplication.CATALYST])
                    if toolset_has_applications(item, [ToolsetApplication.CATALYST])
                    and getattr(item, "busy_reason", None) is None]
                default = next((item for item in values
                                if item[0] == str(getattr(Repository.Settings.value, "default_toolset_id", ""))), None)
                values.insert(0, ("default", f"App default ({default[1]})" if default else "App default"))
            elif category == "library_toolsets":
                from .toolset_selection import default_toolset
                toolsets = sorted(Repository.Toolset.value, key=lambda item: item.name.lower())
                values = [(str(item.uuid), item.name) for item in toolsets]
                configured_id = getattr(Repository.Settings.value, "default_toolset_id", None)
                if configured_id and any(value == str(configured_id) for value, _label in values):
                    category_default_value = str(configured_id)
                else:
                    automatic = default_toolset(applications=(), toolsets=toolsets)
                    values.insert(0, ("default", f"App default — {automatic.name}" if automatic else "App default"))
                    category_default_value = "default"
            elif category == "project_toolsets":
                project_key = str(context.get("project", "")).strip()
                if not project_key:
                    raise ValueError("Pass context.project to list project Catalyst toolsets")
                project = self._project(project_key)
                candidates = [item for item in sorted_toolsets()
                              if toolset_has_applications(item, [ToolsetApplication.CATALYST])
                              and os.path.isfile(item.file_path())]
                values = [(str(item.uuid), item.name) for item in candidates]
                from . import project_toolsets
                from .toolset_selection import default_toolset
                preferred_id = project_toolsets.get(project.directory_path())
                selected = next((item for item in candidates
                                 if str(item.uuid) == str(preferred_id)), None)
                if selected is not None:
                    category_default_value = str(selected.uuid)
                else:
                    default = default_toolset([ToolsetApplication.CATALYST], candidates)
                    values.insert(0, ("default", f"App default — {default.name}" if default else "App default"))
                    category_default_value = "default"
            elif category == "runners":
                from .runner import HOST_RUNNER, runner_is_supported, default_runner
                values = [("host", f"Host — {HOST_RUNNER.short_details}")] if runner_is_supported(HOST_RUNNER) else []
                values.extend((str(item.id), item.name) for item in Repository.Runner.value
                              if runner_is_supported(item))
                preferred = default_runner()
                if preferred is not None:
                    values.insert(0, ("default", f"App default — {preferred.name}"))
            elif category == "packages_directories":
                values = [(str(item.id), item.name) for item in Repository.PackagesDirectory.value]
            elif category == "projects":
                values = [(str(item.id), item.name) for item in Repository.ProjectDirectory.value]
            elif category == "project_stages":
                project_key = str(context.get("project", "")).strip()
                if not project_key:
                    raise ValueError("Pass context.project to list project stages")
                project = self._project(project_key)
                values = [(str(item.id), f"{item.name} ({item.target})") for item in project.stages
                          if not getattr(item, "is_broken", False)]
            elif category == "project_stage_toolsets":
                project_key = str(context.get("project", "")).strip()
                if not project_key:
                    raise ValueError("Pass context.project to list project stage toolsets")
                project = self._project(project_key)
                candidates = [item for item in sorted_toolsets(
                    Repository.Toolset.value, [ToolsetApplication.CATALYST])
                    if item.env == ToolsetEnv.EXTERNAL
                    and toolset_has_applications(item, [ToolsetApplication.CATALYST])
                    and getattr(item, "busy_reason", None) is None]
                values = [(str(item.uuid), item.name) for item in candidates]
                from . import project_toolsets
                preferred_id = project_toolsets.get(project.directory_path())
                selected = next((item for item in candidates if str(item.uuid) == preferred_id), None)
                selected = selected or default_toolset([ToolsetApplication.CATALYST], candidates)
                category_default_value = str(selected.uuid) if selected else None
            elif category == "stage_releng_templates":
                project_key = str(context.get("project", "")).strip()
                target = str(context.get("target", "")).strip()
                if not project_key or not target:
                    raise ValueError("Pass context.project and context.target to list stage Releng specs")
                project = self._project(project_key)
                releng_directory = project.get_releng_directory()
                architecture = project.get_architecture()
                if releng_directory is None or architecture is None:
                    values = []
                else:
                    from .project_stage import load_releng_templates
                    values = [(path, path) for path in load_releng_templates(
                        releng_directory, target, architecture)]
            elif category == "sections":
                values = [(section.__name__, section.section_details.title) for section in AppSection.all_sections]
            elif category == "stage_targets":
                selected_toolset = str(context.get("toolset", "")).strip()
                toolset = next((item for item in Repository.Toolset.value
                                if str(item.uuid) == selected_toolset or item.name == selected_toolset), None)
                if selected_toolset and toolset is None:
                    raise ValueError("Selected toolset is unavailable; refresh project_stage_toolsets")
                if toolset is None:
                    from .toolset_selection import default_toolset
                    toolset = default_toolset([ToolsetApplication.CATALYST])
                if (toolset is None or toolset.env != ToolsetEnv.EXTERNAL
                        or not toolset_has_applications(toolset, [ToolsetApplication.CATALYST])):
                    values = []
                else:
                    from .project_stage import load_catalyst_targets
                    values = [(item, item) for item in load_catalyst_targets(toolset)]
            elif category in ("stage_settings", "stage_setting_options"):
                # Settings and their options are derived from the selected Catalyst target,
                # using the project's actual toolset just like the stage editor.
                project_key = str(context.get("project", "")).strip()
                stage_key = str(context.get("stage", "")).strip()
                if not project_key or not stage_key:
                    raise ValueError("Pass context.project and context.stage to list stage settings")
                project = self._project(project_key)
                stage = next((item for item in project.stages
                              if item.name == stage_key or str(item.id) == stage_key), None)
                if stage is None:
                    raise ValueError(f"No stage named or identified {stage_key!r}")
                from .project_stage import load_catalyst_stage_arguments_details
                from .toolset_selection import build_toolset_requirements, default_toolset
                from . import project_toolsets
                selected_id = project_toolsets.get(project.directory_path())
                toolset = next((item for item in Repository.Toolset.value if str(item.uuid) == selected_id), None)
                toolset = toolset or default_toolset(build_toolset_requirements(project))
                if toolset is None:
                    details = {}
                else:
                    details = load_catalyst_stage_arguments_details(toolset, stage.target)
                if category == "stage_settings":
                    result[category] = [{"value": item.name, "label": item.display_name,
                                         "required": item.required,
                                         "type": item.type.name,
                                         "allows_additions": bool(item.details and item.details.allows_additions),
                                         "automatic_options": [self._automatic_option_token(option)
                                                               for option in (item.details.automatic_options if item.details else [])]}
                                        for item in details.values()]
                    continue
                setting_name = str(context.get("setting", "")).strip()
                argument = details.get(setting_name)
                if argument is None:
                    raise ValueError("Pass context.setting from stage_settings for an available setting")
                options = self._stage_setting_options(project, stage, argument, toolset)
                result[category] = [{"value": self._stage_option_token(option.value),
                                     "label": option.display,
                                     "description": option.subtitle,
                                     "unsupported": bool(option.unsupported)} for option in options]
                continue
            default_value = {
                "architectures": Architecture.HOST.value if Architecture.HOST else None,
                "snapshots": "latest",
                "build_snapshots": "project",
                "toolsets": "default",
                "project_toolsets": category_default_value,
                "build_toolsets": "default",
            }.get(category, category_default_value)
            if category == "toolset_stages":
                recommended_value = next((value for value, label in values
                                          if " — recommended" in label), None)
                default_value = recommended_value
            elif category in ("runners", "build_runners"):
                from .runner import default_runner, HOST_RUNNER
                runner = default_runner()
                if runner is HOST_RUNNER:
                    default_value = "host"
                elif runner is not None:
                    default_value = str(runner.id)
                if category == "runners" and any(value == "default" for value, _ in values):
                    default_value = "default"
            if default_value is None and len(values) == 1:
                default_value = values[0][0]
            result[category] = [{"value": value, "label": label,
                                 "default": value == default_value}
                                for value, label in values]
            if requested_category != category:
                result[requested_category] = result[category]
        self._last_listed_options.update({
            category: self._collect_option_pairs(options)
            for category, options in result.items()
        })
        self._last_listed_option_rows.update({
            category: [dict(option) for option in options if isinstance(option, dict)]
            for category, options in result.items() if isinstance(options, list)
        })
        if "template_variables" in result:
            self._last_template_variable_options.clear()
            self._last_template_variables.clear()
        for variable in result.get("template_variables", []):
            if isinstance(variable, dict) and variable.get("id"):
                variable_id = str(variable["id"])
                self._last_template_variable_options[variable_id] = self._collect_option_pairs(
                    variable.get("options", []))
                self._last_template_variables[variable_id] = dict(variable)
        return self._json(result)

    @staticmethod
    def _automatic_option_token(option):
        from .project_stage_automatic_option import StageAutomaticOption
        return {
            StageAutomaticOption.INHERIT_FROM_PARENT: "@inherit_parent",
            StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE: "@inherit_releng_template",
            StageAutomaticOption.GENERATE_AUTOMATICALLY: "@automatic",
        }[option]

    @classmethod
    def _stage_option_token(cls, value):
        from enum import Enum
        from .project_stage_automatic_option import StageAutomaticOption
        if isinstance(value, StageAutomaticOption):
            return cls._automatic_option_token(value)
        if isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, Enum):
            return value.name
        for attribute in ("path", "filename", "name"):
            candidate = getattr(value, attribute, None)
            if isinstance(candidate, str) and candidate:
                return candidate
        return str(value)

    @staticmethod
    def _stage_setting_options(project, stage, argument, toolset):
        from .project_stage import (
            load_catalyst_stage_arguments_options,
            load_catalyst_stage_arguments_options_for_boolean,
            load_catalyst_stage_automatic_arguments_options,
        )
        from .project_stage_arguments import StageArgumentType
        automatic = load_catalyst_stage_automatic_arguments_options(stage, argument, project) or []
        if argument.type == StageArgumentType.boolean:
            available = load_catalyst_stage_arguments_options_for_boolean(argument) or []
        else:
            available = load_catalyst_stage_arguments_options(project, stage, argument, toolset) or []
        # Keep the current unsupported selection visible so the model can diagnose it
        # and replace it, matching the recovery behavior of the stage settings UI.
        current = getattr(stage, argument.attribute_name, None)
        current_values = current if isinstance(current, list) else [current]
        present = {option.value for option in automatic + available}
        from .project_stage import create_unsupported_options
        missing = [value for value in current_values if value is not None and value not in present]
        return create_unsupported_options(missing, argument.details) + automatic + available

    def _ask_user(self, arguments):
        if self.user_input_callback is None:
            raise RuntimeError("Chat input forms are unavailable")
        title = arguments.get("title")
        message = arguments.get("message")
        fields = arguments.get("fields")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("A form needs a non-empty title")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("A form needs a non-empty message")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 50:
            raise ValueError("A form must contain between 1 and 50 fields")
        title = title.strip()[:120]
        # Models sometimes copy the template API's variable types directly
        # (`choice`, `multiple`, `architecture`) or omit field IDs. Convert
        # those into the smaller, stable set supported by the chat form.
        used_ids = set()
        for index, field in enumerate(fields, start=1):
            if not isinstance(field, dict):
                continue
            # Recover missing labels from common aliases or the stable field
            # ID so an incomplete model-generated form remains renderable.
            label = str(field.get("label") or field.get("title") or field.get("name") or "").strip()
            identifier = str(field.get("id", "")).strip()
            if not label:
                label = re.sub(r"[_-]+", " ", identifier).strip().capitalize()
            if not label:
                label = f"Field {index}"
            field["label"] = label[:120]
            field_type = str(field.get("type", "")).strip().lower()
            field["type"] = {
                "string": "text", "choice": "select", "architecture": "select",
                "multiple": "multiselect",
            }.get(field_type, field_type)
            identifier = str(field.get("id", "")).strip()
            if not identifier:
                label = str(field.get("label", "")).strip()
                identifier = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
                if not identifier:
                    identifier = f"field_{index}"
                if identifier in used_ids:
                    identifier = f"{identifier}_{index}"
                field["id"] = identifier
            used_ids.add(identifier)
        # Template fields come from application metadata. Models sometimes
        # add Markdown punctuation to IDs/options (e.g. ``serial_console**``)
        # or reuse template API types. Rebuild matching fields from the exact
        # latest list_options result so the form remains valid and selectable.
        template_variables = getattr(self, "_last_template_variables", {})
        if template_variables:
            def normalized_key(value):
                return re.sub(r"[^a-z0-9]+", "", str(value).lower())

            canonical = {normalized_key(key): value
                         for key, value in template_variables.items()}
            for field in fields:
                if not isinstance(field, dict):
                    continue
                variable = canonical.get(normalized_key(field.get("id", "")))
                if variable is None:
                    variable = next((item for item in template_variables.values()
                                     if normalized_key(item.get("label", ""))
                                     == normalized_key(field.get("label", ""))), None)
                if variable is None:
                    continue
                field["id"] = str(variable["id"])
                field["label"] = str(variable.get("label") or variable["id"])
                field["type"] = {
                    "choice": "select", "architecture": "select",
                    "multiple": "multiselect", "boolean": "boolean",
                }.get(str(variable.get("type", "text")).lower(), "text")
                field["required"] = (
                    int(variable.get("minimum_selected", 0)) > 0
                    if variable.get("type") == "multiple"
                    else bool(field.get("required", True)))
                if field["type"] in ("select", "multiselect", "boolean"):
                    field["options"] = list(variable.get("options") or [])
                else:
                    field.pop("options", None)
                if variable.get("default") is not None:
                    field["default"] = variable["default"]
        # Keep project creation conversational and let the selected source drive
        # the next form. Models often put every possible source's fields into a
        # single ask_user call; show only the stable first-step choices.
        name_field = next((field for field in fields if isinstance(field, dict)
                           and str(field.get("id", "")).lower() in {"name", "project_name"}), None)
        source_field = next((field for field in fields if isinstance(field, dict)
                             and str(field.get("id", "")).lower() in
                             {"source", "source_type", "project_source"}), None)
        source_values = {str(item.get("value", "")) for item in
                         (source_field.get("options", []) if source_field else [])
                         if isinstance(item, dict)}
        project_creation_form = bool(name_field and source_field
                                     and source_values.intersection({"empty", "git", "template"}))
        if project_creation_form:
            source_field["id"] = "source"
            source_field["type"] = "select"
            source_field["options"] = [
                {"value": value, "label": label}
                for value, label in (("empty", "Empty project"),
                                     ("git", "Clone Git repository"),
                                     ("template", "Start from a template"))
                if value in source_values
            ]
            fields = [source_field, name_field]
        identifiers = set()
        for field in fields:
            if not isinstance(field, dict):
                raise ValueError("Each form field must be an object")
            identifier = str(field.get("id", ""))
            if not identifier or identifier in identifiers:
                raise ValueError("Form field IDs must be non-empty and unique")
            identifiers.add(identifier)
            field_type = field.get("type")
            if field_type not in ("text", "select", "multiselect", "boolean"):
                raise ValueError(f"Unsupported form field type: {field_type}")
            if field_type in ("select", "multiselect"):
                options = field.get("options")
                if not isinstance(options, list) or not options or len(options) > 100:
                    raise ValueError(f"Field {identifier!r} needs 1 to 100 options from list_options")
                if any(not isinstance(option, dict) or "value" not in option or "label" not in option for option in options):
                    raise ValueError(f"Field {identifier!r} has an invalid option")
                # Project creation should offer the actual snapshots directly,
                # rather than an invented "Specific snapshot" choice that
                # requires another round trip to see the real entries.
                if (identifier in {"snapshot", "snapshot_id", "snapshot_choice"}
                        and "project" in title.lower()):
                    from .repositories import Repository
                    field["options"] = ([{"value": "latest", "label": "Latest snapshot",
                                          "default": True}] +
                                         [{"value": item.filename, "label": item.name}
                                          for item in Repository.Snapshot.value])
                    field["default"] = "latest"
                expected_category = {
                    "project": "projects",
                    "project_id": "projects",
                    "project_name": "projects",
                    "target_project": "projects",
                    "stage": "project_stages",
                    "stage_id": "project_stages",
                    "stages": "project_stages",
                    "toolset_id": "toolsets",
                    "runner_id": "runners",
                    "packages_directory": "packages_directories",
                    "packages_directory_id": "packages_directories",
                    "architecture": "architectures",
                    "architectures": "architectures",
                    "releng": "releng_directories",
                    "releng_directory": "releng_directories",
                    "snapshot": "snapshots",
                    "snapshot_id": "snapshots",
                    "snapshot_choice": "snapshots",
                    "toolset": "toolsets",
                    "stage_toolset": "project_stage_toolsets",
                    "stage_target": "stage_targets",
                    "seed_stage": "project_stages",
                    "releng_template": "stage_releng_templates",
                    "runner": "runners",
                    "source": "project_sources",
                    "source_type": "project_sources",
                    "template": "project_templates",
                }.get(identifier)
                if not expected_category:
                    normalized_identifier = re.sub(r"[^a-z0-9]+", "_", identifier.lower()).strip("_")
                    expected_category = {
                        "project_id": "projects", "project_name": "projects",
                        "target_project": "projects", "stage_id": "project_stages",
                        "toolset_id": "toolsets", "runner_id": "runners",
                        "packages_directory_id": "packages_directories",
                    }.get(normalized_identifier)
                # Use the actual current app option list to repair model-added
                # labels/values before validation. This preserves strict
                # provenance while allowing retries to recover from malformed
                # form output instead of failing the whole ask_user call.
                listed_rows = (self._last_listed_option_rows.get(expected_category, [])
                               if expected_category else [])
                if listed_rows:
                    field["options"] = [dict(option) for option in listed_rows]
                    defaults = [option.get("value") for option in listed_rows
                                if option.get("default")]
                    if defaults:
                        field["default"] = defaults[0]
                    elif field.get("required") and len(listed_rows) == 1:
                        field["default"] = listed_rows[0].get("value")
                if identifier in self._last_template_variable_options:
                    available = self._last_template_variable_options[identifier]
                elif expected_category:
                    available = self._last_listed_options.get(expected_category, set())
                else:
                    available = set().union(*self._last_listed_options.values()) \
                        if self._last_listed_options else set()
                if not available:
                    category_text = f"list_options({expected_category})" if expected_category else "list_options"
                    raise ValueError(
                        f"Can't show invented choices for {identifier!r}. Call {category_text} first, "
                        "then use its exact value and label in ask_user."
                    )
                invalid = [option for option in field["options"]
                           if (str(option["value"]), str(option["label"])) not in available]
                if invalid:
                    raise ValueError(
                        f"Choices for {identifier!r} don't match available app options. "
                        "Call list_options for the correct category/context and retry with exact values and labels."
                    )
        message = message.strip()[:500]
        answers = self.user_input_callback(title, message, fields)
        if answers is None:
            return self._json({"status": "cancelled"})
        answers = dict(answers)
        feedback = str(answers.pop("__user_feedback__", "")).strip()
        # Keep the most recent project form's selected template/name as context
        # for a follow-up list_options call when a local model omits context.
        for identifier, context_key in (("template", "template"), ("project_template", "template"),
                                        ("project_name", "project_name"), ("name", "project_name")):
            if identifier in answers and answers[identifier] not in (None, ""):
                self._last_project_form_context[context_key] = answers[identifier]
        result = {"status": "submitted", "answers": answers}
        if project_creation_form and any(key in answers for key in ("source", "source_type", "project_source")):
            source = next((str(answers[key]) for key in ("source", "source_type", "project_source")
                           if key in answers), "")
            result["next_step"] = (
                f"The user selected project source {source!r}. Ask a new form for the remaining shared project "
                "settings (architecture, Releng directory, snapshot, and toolset), using fresh list_options results. "
                "For source 'git', include a required repository URL. For source 'template', include a template "
                "choice, then load template_variables for that exact template and ask its variables before creating. "
                "For source 'empty', do not ask Git or template questions. Do not ask for the name or source again.")
        if feedback:
            result["user_feedback"] = feedback[:2000]
        return self._json(result)

    def _list_sections(self, _arguments):
        from .app_section import AppSection
        return self._json([{"label": section.section_details.label,
                            "title": section.section_details.title,
                            "class": section.__name__}
                           for section in AppSection.all_sections])

    def _open_section(self, arguments):
        from .app_section import AppSection
        from .app_events import AppEvents, app_event_bus
        target = str(arguments.get("section", "")).strip().lower()
        section = next((item for item in AppSection.all_sections
                        if target in (item.section_details.label.lower(), item.section_details.title.lower(), item.__name__.lower())), None)
        if section is None:
            raise ValueError("No application section matches that name")
        completed = threading.Event()
        outcome = {}
        def open_on_main_thread():
            try:
                app_event_bus.emit(AppEvents.OPEN_APP_SECTION, section)
            except Exception as error:
                outcome["error"] = str(error)
            finally:
                completed.set()
            return False
        from gi.repository import GLib
        GLib.idle_add(open_on_main_thread)
        if not completed.wait(10):
            raise RuntimeError("Timed out opening application section")
        if outcome.get("error"):
            raise RuntimeError(outcome["error"])
        return f"Opened {section.section_details.title}."

    def _open_about(self, _arguments):
        def action():
            from gi.repository import Gio
            application = Gio.Application.get_default()
            if application is None or application.lookup_action("about") is None:
                raise RuntimeError("The About window is unavailable")
            application.activate_action("about", None)
            return self._json({"status": "opened", "window": "About Catalyst Lab"})
        return self._on_main_thread(action)

    def _open_keyboard_shortcuts(self, _arguments):
        def action():
            window = self.content_navigation_view.get_root() if self.content_navigation_view else None
            if window is None:
                from gi.repository import Gtk
                application = Gtk.Application.get_default()
                window = application.get_active_window() if application else None
            if window is None or not window.activate_action("win.show-help-overlay", None):
                raise RuntimeError("The Keyboard Shortcuts overlay is unavailable")
            return self._json({"status": "opened", "window": "Keyboard Shortcuts"})
        return self._on_main_thread(action)

    def _open_project(self, arguments):
        project = self._project(arguments.get("project", ""))
        def action():
            from .app_events import AppEvents, app_event_bus
            from .app_section import AppSection
            from .project_details_view import ProjectDetailsView
            app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.ProjectsSection)
            app_event_bus.emit(AppEvents.PUSH_VIEW, ProjectDetailsView(project), title=project.name)
            return f"Opened project {project.name}."
        return self._on_main_thread(action)

    def _git_directory(self, kind, name):
        kind = str(kind).strip().lower()
        if kind not in ("project", "releng", "overlay"):
            raise ValueError("Git actions support project, releng, or overlay objects")
        return self._find_any_object(kind, str(name).strip())

    def _get_git_status(self, arguments):
        directory = self._git_directory(arguments.get("type", ""), arguments.get("name", ""))
        status = getattr(directory.status, "name", str(directory.status)).lower()
        return self._json({
            "type": str(arguments.get("type", "")).lower(),
            "name": directory.name,
            "status": status,
            "has_remote_changes": bool(directory.has_remote_changes),
            "is_busy": bool(directory.is_busy),
            "remote_url": directory.remote_url,
            "branch": directory.branch_name,
            "last_commit": directory.last_commit_date.isoformat() if directory.last_commit_date else None,
            "recent_commits": [{"message": item.get("message"), "author": item.get("author"),
                                "date": item.get("date").isoformat() if item.get("date") else None,
                                "hash": item.get("hash")}
                               for item in (directory.logs or [])[:10]],
            "note": "Git state is the app's most recently refreshed status; call update_git_directory to fetch and rebase remote changes.",
        })

    def _list_git_changes(self, arguments):
        directory = self._git_directory(arguments.get("type", ""), arguments.get("name", ""))
        if directory.is_busy:
            raise ValueError(f"{directory.name} already has a Git operation running")
        limit = arguments.get("limit", 200)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500")
        result = directory._run_git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"Could not inspect Git changes for {directory.name}")
        raw_entries = (result.stdout or b"").decode("utf-8", errors="replace").split("\0")
        entries = []
        index = 0
        while index < len(raw_entries) and raw_entries[index]:
            record = raw_entries[index]
            index += 1
            if len(record) < 4 or record[2] != " ":
                continue
            code, path = record[:2], record[3:]
            previous_path = None
            if "R" in code or "C" in code:
                if index < len(raw_entries) and raw_entries[index]:
                    previous_path = raw_entries[index]
                    index += 1
            entries.append({"status": code, "staged": code[0] not in (" ", "?"),
                            "worktree": code[1] not in (" ", "?"), "untracked": code == "??",
                            "path": path,
                            **({"previous_path": previous_path} if previous_path else {})})
        return self._json({
            "type": str(arguments.get("type", "")).lower(),
            "name": directory.name,
            "entry_count": len(entries),
            "returned_count": min(len(entries), limit),
            "truncated": len(entries) > limit,
            "changes": entries[:limit],
            "contents_included": False,
            "note": "Status codes use Git porcelain XY notation (index, worktree). No network fetch or file-content read was performed.",
        })

    def _update_git_directory(self, arguments):
        kind = str(arguments.get("type", "")).strip().lower()
        directory = self._git_directory(kind, arguments.get("name", ""))
        if directory.is_busy:
            raise ValueError(f"{directory.name} already has a Git operation running")
        if not directory.remote_url:
            raise ValueError(f"{directory.name} has no Git remote to update")
        def action():
            from .project_update import ProjectUpdate
            from .overlay_update import OverlayUpdate
            from .releng_update import RelengUpdate
            update_type = {"project": ProjectUpdate, "overlay": OverlayUpdate, "releng": RelengUpdate}[kind]
            process = update_type(directory=directory)
            process.start()
            return self._json({"status": "started", "type": kind, "name": directory.name,
                               "operation": process.title,
                               "message": "The fetch and rebase operation started; follow it in Operations."})
        return self._on_main_thread(action)

    def _commit_git_changes(self, arguments):
        directory = self._git_directory(arguments.get("type", ""), arguments.get("name", ""))
        message = str(arguments.get("message", "")).strip()
        if not message:
            raise ValueError("A non-empty commit message is required")
        if directory.is_busy:
            raise ValueError(f"{directory.name} already has a Git operation running")
        status = getattr(directory.status, "name", "").lower()
        if status != "changed":
            raise ValueError(f"{directory.name} has no ordinary local changes to commit (current status: {status})")
        def action():
            directory.commit_changes(message=message)
            return self._json({"status": "started", "name": directory.name, "message": message,
                               "result": "The commit is running in the background."})
        return self._on_main_thread(action)

    def _discard_git_changes(self, arguments):
        directory = self._git_directory(arguments.get("type", ""), arguments.get("name", ""))
        if directory.is_busy:
            raise ValueError(f"{directory.name} already has a Git operation running")
        status = getattr(directory.status, "name", "").lower()
        if status not in ("changed", "conflicted"):
            raise ValueError(f"{directory.name} has no local changes to discard (current status: {status})")
        if self.user_input_callback is None:
            raise RuntimeError("Discarding Git changes requires an in-chat confirmation")
        answer = self.user_input_callback(
            "Discard local changes?",
            f"This resets {directory.name} and deletes all untracked and ignored files inside it. This cannot be undone.",
            [{"id": "confirm", "label": "Permanently discard all local and untracked files", "type": "boolean",
              "placeholder": "Confirm permanent deletion", "required": True}],
        )
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "name": directory.name})
        def action():
            if directory.is_busy:
                raise RuntimeError("A Git operation started while confirming; nothing was discarded")
            directory.discard_changes()
            return self._json({"status": "started", "name": directory.name,
                               "message": "Discarding changes in the background."})
        return self._on_main_thread(action)

    def _rename_git_directory(self, arguments):
        kind = str(arguments.get("type", "")).strip().lower()
        if kind not in ("releng", "overlay"):
            raise ValueError("Only Releng directories and overlays can be renamed here")
        directory = self._git_directory(kind, arguments.get("name", ""))
        new_name = str(arguments.get("new_name", "")).strip()
        if not new_name:
            raise ValueError("The new name cannot be empty")
        old_name = directory.name
        if new_name == old_name:
            return self._json({"status": "unchanged", "type": kind, "name": old_name})
        if directory.is_busy:
            raise ValueError(f"{directory.name} has an operation running")
        def action():
            from .releng_manager import RelengManager
            from .overlay_manager import OverlayManager
            manager = RelengManager if kind == "releng" else OverlayManager
            if new_name != directory.name and not manager.shared().is_name_available(new_name):
                raise ValueError(f"A {kind} directory named {new_name!r} already exists")
            manager.shared().rename_directory(directory, new_name)
            return self._json({"status": "renamed", "type": kind, "old_name": old_name,
                               "name": new_name})
        return self._on_main_thread(action)

    def _delete_git_directory(self, arguments):
        kind = str(arguments.get("type", "")).strip().lower()
        if kind not in ("releng", "overlay"):
            raise ValueError("Only Releng directories and overlays can be deleted here")
        directory = self._git_directory(kind, arguments.get("name", ""))
        if directory.is_busy:
            raise ValueError(f"{directory.name} has an operation running")
        from .repositories import Repository
        references = []
        for project in Repository.ProjectDirectory.value:
            if kind == "releng" and project.get_releng_directory() is directory:
                references.append(project.name)
            elif kind == "overlay":
                for stage in project.stages:
                    repos = getattr(stage, "repos", None)
                    repo_values = repos if isinstance(repos, list) else [repos]
                    if any(str(value) == str(directory.id) for value in repo_values):
                        references.append(project.name)
                        break
        from .project_build_process import running_project_build
        active_references = [project.name for project in Repository.ProjectDirectory.value
                             if project.name in references and running_project_build(project) is not None]
        if active_references:
            raise ValueError("Cannot delete this directory while these referencing projects are building: " +
                             ", ".join(active_references))
        warning = (" It is directly referenced by these projects: " + ", ".join(references) + ".") if references else ""
        if self.user_input_callback is None:
            raise RuntimeError("Deleting a Git directory requires an in-chat confirmation")
        answer = self.user_input_callback(
            f"Delete {kind} directory?",
            f"Permanently delete {directory.name} and all files inside it.{warning}",
            [{"id": "confirm", "label": f"Permanently delete {directory.name}", "type": "boolean",
              "placeholder": "Confirm permanent deletion", "required": True}],
        )
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "type": kind, "name": directory.name})
        def action():
            if directory.is_busy:
                raise RuntimeError("A Git operation started while confirming; nothing was deleted")
            active_references_now = [project.name for project in Repository.ProjectDirectory.value
                                     if project.name in references and running_project_build(project) is not None]
            if active_references_now:
                raise RuntimeError("A referencing project started building while confirming; nothing was deleted")
            from .releng_manager import RelengManager
            from .overlay_manager import OverlayManager
            from .app_events import AppEvents, app_event_bus
            from .app_section import AppSection
            manager = RelengManager if kind == "releng" else OverlayManager
            manager.shared().remove_directory(directory)
            app_event_bus.emit(AppEvents.OPEN_APP_SECTION,
                               AppSection.RelengSection if kind == "releng" else AppSection.OverlaysSection)
            return self._json({"status": "deleted", "type": kind, "name": directory.name,
                               "referencing_projects": references})
        return self._on_main_thread(action)

    def _create_git_directory(self, arguments):
        kind = str(arguments.get("type", "")).strip().lower()
        if kind not in ("releng", "overlay"):
            raise ValueError("Git directory type must be releng or overlay")
        name = str(arguments.get("name", "")).strip()
        source = str(arguments.get("source", "")).strip().lower()
        repository_url = str(arguments.get("repository_url", "")).strip()
        local_directory = str(arguments.get("local_directory", "")).strip()
        if not name:
            raise ValueError("Directory name cannot be empty")
        if source not in (("git",) if kind == "releng" else ("git", "local", "empty")):
            raise ValueError(f"Source {source!r} is not available for {kind}")
        if source == "git" and not repository_url:
            raise ValueError("A Git repository URL is required")
        if source == "local":
            if not local_directory:
                raise ValueError("An absolute local_directory path is required to copy an overlay")
            if not os.path.isabs(local_directory) or not os.path.isdir(local_directory):
                raise ValueError("local_directory must be an absolute path to an existing directory")
        from .releng_manager import RelengManager
        from .overlay_manager import OverlayManager
        manager = RelengManager if kind == "releng" else OverlayManager
        if not manager.shared().is_name_available(name):
            raise ValueError(f"A {kind} directory named {name!r} already exists")
        def action():
            if not manager.shared().is_name_available(name):
                raise ValueError(f"A {kind} directory named {name!r} already exists")
            from gi.repository import Gio
            from .git_installation import GitDirectorySetupConfiguration, GitDirectorySource
            installation_source = {
                "git": GitDirectorySource.GIT_REPOSITORY,
                "local": GitDirectorySource.LOCAL_DIRECTORY,
                "empty": GitDirectorySource.CREATE_NEW,
            }[source]
            data = (repository_url if source == "git" else
                    Gio.File.new_for_path(local_directory) if source == "local" else None)
            if kind == "releng":
                from .releng_installation import RelengInstallation
                installation = RelengInstallation(GitDirectorySetupConfiguration(
                    source=installation_source, name=name, data=data))
            else:
                from .overlay_installation import OverlayInstallation
                if source == "empty":
                    from .overlay_create_view import DefaultOverlayDirContentBuilder
                    data = DefaultOverlayDirContentBuilder()
                installation = OverlayInstallation(GitDirectorySetupConfiguration(
                    source=installation_source, name=name, data=data))
            installation.start()
            return self._json({"status": "started", "type": kind, "name": name,
                               "source": source})
        return self._on_main_thread(action)

    def _create_vm_runner(self, arguments):
        import os
        from .build_machine_manager import VMRunnerManager
        from .lima import lima_unavailable_reason
        from .build_machine import VMRunner
        if reason := lima_unavailable_reason():
            raise RuntimeError(reason)
        manager = VMRunnerManager.shared()
        name = str(arguments.get("name", "")).strip()
        if not name:
            name = _unique_name("Alpine VM", manager.is_name_available)
        if not manager.is_name_available(name):
            raise ValueError(f"A VM runner named {name!r} already exists")
        cpus = int(arguments.get("cpus", max(1, min(8, (os.cpu_count() or 4) // 2))))
        memory_gib = int(arguments.get("memory_gib", VMRunner.DEFAULT_MEMORY_GIB))
        workspace_gib = int(arguments.get("workspace_gib", 64))
        swap_gib = int(arguments.get("swap_gib", VMRunner.DEFAULT_SWAP_GIB))
        set_as_default = arguments.get("set_as_default", True)
        if type(set_as_default) is not bool:
            raise ValueError("set_as_default must be true or false")
        if not 1 <= cpus <= (os.cpu_count() or 64):
            raise ValueError("CPU count must be between 1 and the host's available CPU count")
        if not 2 <= memory_gib <= 256:
            raise ValueError("Memory must be between 2 and 256 GiB")
        if not 20 <= workspace_gib <= 2048:
            raise ValueError("Working space must be between 20 and 2048 GiB")
        if not 0 <= swap_gib <= 512:
            raise ValueError("Swap must be between 0 and 512 GiB")
        def action():
            if not manager.is_name_available(name):
                raise ValueError(f"A VM runner named {name!r} already exists")
            from .build_machine_installation import VMRunnerInstallation
            installation = VMRunnerInstallation(name=name, cpus=cpus, memory_gib=memory_gib,
                                                workspace_gib=workspace_gib, swap_gib=swap_gib,
                                                set_as_default=set_as_default)
            installation.start()
            return self._json({"status": "started", "runner": name,
                               "resources": {"cpus": cpus, "memory_gib": memory_gib,
                                             "workspace_gib": workspace_gib, "swap_gib": swap_gib}})
        return self._on_main_thread(action)

    def _create_toolset(self, arguments):
        from urllib.parse import urlparse
        from gi.repository import Gio
        from .architecture import Architecture
        from .toolset import ToolsetApplication
        from .toolset_application import ToolsetApplicationSelection
        from .toolset_env_builder import get_stage3_urls
        from .toolset_manager import ToolsetManager
        from .runner import default_runner, runner_by_id, runner_is_supported, RunnerType

        manager = ToolsetManager.shared()
        name = str(arguments.get("name", "")).strip()

        architecture_key = str(arguments.get("architecture", "")).strip()
        architecture = next((item for item in Architecture
                             if item.value == architecture_key or item.name == architecture_key), None)
        if not architecture_key:
            architecture = Architecture.HOST
        if architecture is None:
            raise ValueError("Choose an architecture from list_options(architectures)")
        stage_url = str(arguments.get("stage_url", "")).strip()
        if not stage_url:
            stage_url = next((str(item.get("value", ""))
                              for item in self._last_listed_option_rows.get("toolset_stages", [])
                              if item.get("default")), "")
        if not stage_url:
            raise ValueError("Call list_options(toolset_stages) and use its recommended stage URL")
        parsed_url = urlparse(stage_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "distfiles.gentoo.org":
            raise ValueError("Choose an HTTPS stage3 archive from list_options(toolset_stages)")
        current_stages = get_stage3_urls(architecture)
        if isinstance(current_stages, Exception):
            raise RuntimeError(f"Couldn't verify the selected stage3 archive: {current_stages}")
        selected_stage = next((item for item in current_stages if item.geturl() == stage_url), None)
        if selected_stage is None:
            raise ValueError("That stage3 archive is no longer available for this architecture; refresh list_options")
        if not name:
            filename = Path(selected_stage.path).name
            filename_without_extension = Path(filename).stem
            for suffix in Path(filename).suffixes:
                filename_without_extension = filename_without_extension.rstrip(suffix)
            parts = filename_without_extension.split("-")
            name = " ".join(parts[1:-1]) if len(parts) > 2 else filename_without_extension
            name = _unique_name(name, manager.is_name_available)
        if not name:
            raise ValueError("Toolset name cannot be derived from the selected stage; provide a name")
        if not manager.is_name_available(name):
            raise ValueError(f"A toolset named {name!r} already exists")

        runner_key = str(arguments.get("runner", "default")).strip()
        runner = default_runner() if runner_key == "default" else runner_by_id(runner_key)
        if runner is None or not runner_is_supported(runner):
            raise ValueError("Choose a supported runner from list_options(runners)")

        selected_apps = arguments.get("applications")
        if selected_apps is None:
            selected_apps = [app.package for app in ToolsetApplication.ALL if not app.auto_select]
        if not isinstance(selected_apps, list) or any(not isinstance(item, str) for item in selected_apps):
            raise ValueError("applications must be a list of package IDs from toolset_applications")
        selectable_apps = {app.package: app for app in ToolsetApplication.ALL if not app.auto_select}
        listed_apps = self._last_listed_options.get("toolset_applications", set())
        if not listed_apps:
            raise ValueError("Call list_options(toolset_applications) before selecting toolset applications")
        if set(selected_apps) - {value for value, _label in listed_apps}:
            raise ValueError("applications must use exact package IDs from the latest toolset_applications options")
        unknown_apps = sorted(set(selected_apps) - selectable_apps.keys())
        if unknown_apps:
            raise ValueError(f"Unknown toolset application(s): {', '.join(unknown_apps)}")
        if len(set(selected_apps)) != len(selected_apps):
            raise ValueError("applications cannot contain duplicate package IDs")

        versions = arguments.get("application_versions") or {}
        patches = arguments.get("patches") or {}
        if not isinstance(versions, dict) or not isinstance(patches, dict):
            raise ValueError("application_versions and patches must be objects keyed by application package")
        unknown_version_apps = set(versions) - selectable_apps.keys()
        unknown_patch_apps = set(patches) - selectable_apps.keys()
        if unknown_version_apps or unknown_patch_apps:
            raise ValueError("Versions and patches may only be specified for selectable toolset applications")

        selections = []
        for app in ToolsetApplication.ALL:
            requested_version = str(versions.get(app.package, ""))
            version = next((item for item in app.versions if str(item.id) == requested_version), None)
            if requested_version and version is None:
                raise ValueError(f"Version {requested_version!r} is not available for {app.package}")
            version = version or (app.versions[0] if app.versions else None)
            if version is None:
                raise ValueError(f"No version is available for {app.package}")
            patch_paths = patches.get(app.package, [])
            if not isinstance(patch_paths, list) or any(not isinstance(path, str) for path in patch_paths):
                raise ValueError(f"Patches for {app.package} must be a list of local file paths")
            patch_files = []
            for path in patch_paths:
                patch_path = Path(path).expanduser()
                if not patch_path.is_file():
                    raise ValueError(f"Patch file does not exist: {patch_path}")
                patch_files.append(Gio.File.new_for_path(str(patch_path.resolve())))
            selections.append(ToolsetApplicationSelection(
                app=app, version=version, selected=app.package in selected_apps,
                patches=patch_files))

        allow_binpkgs = arguments.get("allow_binpkgs", True)
        set_as_default = arguments.get("set_as_default", True)
        if type(allow_binpkgs) is not bool or type(set_as_default) is not bool:
            raise ValueError("allow_binpkgs and set_as_default must be booleans")

        stage_filename = os.path.basename(selected_stage.path)
        app_summary = []
        for package in selected_apps:
            app = selectable_apps[package]
            version = next((item for item in app.versions
                            if str(item.id) == str(versions.get(package, ""))), None)
            version = version or (app.versions[0] if app.versions else None)
            suffix = f" ({version.name})" if version else ""
            patch_count = len(patches.get(package, []))
            if patch_count:
                suffix += f", {patch_count} patch{'es' if patch_count != 1 else ''}"
            app_summary.append(f"{app.name}{suffix}")
        def begin(authorization_keeper):
            if not authorization_keeper:
                return
            try:
                if not manager.is_name_available(name):
                    raise ValueError(f"A toolset named {name!r} already exists")
                from .toolset_installation import ToolsetInstallation
                installation = ToolsetInstallation(
                    alias=name, stage_url=selected_stage, allow_binpkgs=allow_binpkgs,
                    apps_selection=selections, runner=runner, set_as_default=set_as_default)
                installation.start(authorization_keeper=authorization_keeper)
            except Exception as error:
                print(f"Failed to start toolset installation: {error}")
                release = getattr(authorization_keeper, "release", None)
                if release:
                    release()

        from .rootless import authorize_toolset_action
        machine = runner if runner.runner_type == RunnerType.VM else None
        authorize_toolset_action(begin, name=f"Install toolset {name}", machine=machine)
        return self._json({"status": "start_requested", "toolset": name,
                           "architecture": architecture.value, "stage": stage_filename,
                           "runner": runner.name, "applications": app_summary,
                           "allow_binpkgs": allow_binpkgs, "set_as_default": set_as_default})

    def _open_object(self, arguments):
        kind = str(arguments.get("type", "")).lower()
        name = str(arguments.get("name", "")).strip()
        project_key = str(arguments.get("project", "")).strip()
        if kind == "builds":
            if not project_key:
                raise ValueError("Pass project to open its build history")
            item = self._project(project_key)
        elif kind == "stage":
            if not project_key:
                raise ValueError("Pass project to open one of its stages")
            project = self._project(project_key)
            stage_key = str(arguments.get("stage", "") or name).strip()
            item = next((stage for stage in project.stages
                         if stage.name == stage_key or str(stage.id) == stage_key), None)
            if item is None:
                raise ValueError(f"No stage named or identified {stage_key!r} in {project.name}")
        else:
            if not name:
                raise ValueError("Pass the object name or ID")
            item = self._find_any_object(kind, name)

        def action():
            from .app_events import AppEvents, app_event_bus
            from .app_section import AppSection
            from .project_details_view import ProjectDetailsView
            from .toolset_details_view import ToolsetDetailsView
            from .snapshot_details_view import SnapshotDetailsView
            from .releng_details_view import RelengDetailsView
            from .overlay_details_view import OverlayDetailsView
            from .packages_views import PackagesDirectoryDetailsView
            from .build_machine_details_view import VMRunnerDetailsView
            from .project_builds_view import ProjectBuildsView
            from .project_stage_details_view import ProjectStageDetailsView
            views = {
                "project": (AppSection.ProjectsSection, lambda: ProjectDetailsView(item), item.name),
                "stage": (AppSection.ProjectsSection, lambda: ProjectStageDetailsView(project, item), item.name),
                "toolset": (AppSection.EnvironmentsSection, lambda: ToolsetDetailsView(item), item.name),
                "snapshot": (AppSection.SnapshotsSection, lambda: SnapshotDetailsView(item), item.name),
                "releng": (AppSection.RelengSection, lambda: RelengDetailsView(item), item.name),
                "overlay": (AppSection.OverlaysSection, lambda: OverlayDetailsView(item), item.name),
                "packages": (AppSection.PackagesSection, lambda: PackagesDirectoryDetailsView(item), item.name),
                "runner": (AppSection.EnvironmentsSection, lambda: VMRunnerDetailsView(item), item.name),
                "builds": (AppSection.BuildsSection, lambda: ProjectBuildsView(item), f"{item.name} builds"),
            }
            if kind not in views:
                raise ValueError(f"Unsupported details type: {kind}")
            section, make_view, title = views[kind]
            app_event_bus.emit(AppEvents.OPEN_APP_SECTION, section)
            app_event_bus.emit(AppEvents.PUSH_VIEW, make_view(), title=title)
            return f"Opened {kind} details for {item.name}."
        return self._on_main_thread(action)

    def _open_local_folder(self, arguments):
        """Open one app-managed directory; never accept an arbitrary filesystem path."""
        target = str(arguments.get("target", "")).strip().lower()
        if target == "library":
            from ..global_objects.repository import library_directory
            path = library_directory()
            label = "Library folder"
        elif target == "packages":
            key = str(arguments.get("packages_directory", "")).strip()
            if not key:
                raise ValueError("Pass a packages directory name or ID from list_objects(type='packages')")
            directory = self._find_any_object("packages", key)
            path = directory.directory_path()
            os.makedirs(path, exist_ok=True)
            label = f"Packages folder {directory.name}"
        elif target == "build":
            from .project_build import load_project_builds
            project_key = str(arguments.get("project", "")).strip()
            stage_key = str(arguments.get("stage", "")).strip()
            timestamp = str(arguments.get("timestamp", "")).strip()
            if not project_key or not stage_key or not timestamp:
                raise ValueError("Pass project, stage, and exact timestamp from list_builds")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", timestamp):
                raise ValueError("Pass the exact build timestamp returned by list_builds")
            project = self._project(project_key)
            build = next((item for item in load_project_builds(project)
                          if item.timestamp == timestamp
                          and (item.stage_name == stage_key or str(item.stage_id) == stage_key)), None)
            if build is None or not build.path or not os.path.isdir(build.path):
                raise ValueError("No existing build folder matches that project, stage, and timestamp")
            path = build.path
            label = f"Build folder {project.name}/{build.stage_name} ({timestamp})"
        elif target == "stage_overlay":
            from .project_stage_arguments import StageArgumentDetails
            from .project_stage_portage_confdir import stage_overlay_path, stage_root_overlay_path
            project_key = str(arguments.get("project", "")).strip()
            stage_key = str(arguments.get("stage", "")).strip()
            overlay = str(arguments.get("overlay", "")).strip()
            overlay_details = {
                "portage_confdir": StageArgumentDetails.portage_confdir,
                "stage4_root_overlay": StageArgumentDetails.stage4_root_overlay,
                "livecd_root_overlay": StageArgumentDetails.livecd_root_overlay,
                "livecd_overlay": StageArgumentDetails.livecd_overlay,
            }
            if not project_key or not stage_key:
                raise ValueError("Pass project and stage name/ID")
            if overlay not in overlay_details:
                raise ValueError("Pass one supported overlay type: portage_confdir, stage4_root_overlay, livecd_root_overlay, or livecd_overlay")
            project = self._project(project_key)
            stage = next((item for item in project.stages
                          if item.name == stage_key or str(item.id) == stage_key), None)
            if stage is None:
                raise ValueError(f"No stage named or identified {stage_key!r} in {project.name}")
            details = overlay_details[overlay]
            path = (stage_overlay_path(project, stage)
                    if details == StageArgumentDetails.portage_confdir
                    else stage_root_overlay_path(project, stage, details))
            # Match the stage settings UI: these managed folders are retained
            # when their overlay setting is disabled so users can edit them first.
            os.makedirs(path, exist_ok=True)
            label = f"{overlay} folder for {project.name}/{stage.name}"
        else:
            raise ValueError("target must be library, packages, build, or stage_overlay")

        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            raise ValueError(f"The {label.lower()} is not available on disk")

        def action():
            from gi.repository import Gio
            launched = Gio.AppInfo.launch_default_for_uri(
                Gio.File.new_for_path(path).get_uri(), None)
            if not launched:
                raise RuntimeError("The system could not open this folder in a file browser")
            return self._json({"status": "opened", "target": target,
                               "name": label, "path": path})
        return self._on_main_thread(action)

    def _open_wizard(self, arguments):
        workflow = str(arguments.get("workflow", ""))
        project_key = str(arguments.get("project", "")).strip()
        project = self._project(project_key) if project_key else None
        source = str(arguments.get("source", "")).strip().lower()
        if workflow in ("project_build", "stage_create", "project_update", "project_deploy", "vm_launch") and project is None:
            raise ValueError(f"Workflow {workflow} requires project")
        launch_build = None
        if workflow == "vm_launch":
            timestamp = str(arguments.get("timestamp", "")).strip()
            if not timestamp:
                raise ValueError("vm_launch requires the exact completed-build timestamp from list_deployable_builds")
            from .project_build import load_project_builds, StageBuildStatus
            launch_build = next((item for item in load_project_builds(project)
                                 if item.timestamp == timestamp), None)
            if launch_build is None or launch_build.status != StageBuildStatus.COMPLETED:
                raise ValueError("That completed build is no longer available; refresh list_deployable_builds")
        if workflow == "project_update" and source not in ("template", "repository"):
            raise ValueError("Project update needs source='template' or source='repository'")

        def action():
            from .app_events import AppEvents, app_event_bus
            from .app_section import AppSection
            if workflow == "initial_setup":
                if self.content_navigation_view is None:
                    raise RuntimeError("The app navigation view is unavailable")
                from .initial_setup_page import InitialSetupPage
                InitialSetupPage.push(self.content_navigation_view)
                return "Opened the initial setup workflow. Complete its steps in the app."
            elif workflow == "project_create":
                from .project_create_view import ProjectCreateView
                view, title, section = ProjectCreateView(), "New project", AppSection.ProjectsSection
            elif workflow == "project_build":
                from .project_build_view import ProjectBuildView
                view, title, section = ProjectBuildView(project_directory=project), f"Build {project.name}", AppSection.ProjectsSection
            elif workflow == "stage_create":
                from .project_stage_create_view import ProjectStageCreateView
                view, title, section = ProjectStageCreateView(project_directory=project), "New stage", AppSection.ProjectsSection
            elif workflow == "snapshot_create":
                from .snapshot_create_view import SnapshotCreateView
                view, title, section = SnapshotCreateView(), "New snapshot", AppSection.SnapshotsSection
            elif workflow == "overlay_create":
                from .overlay_create_view import OverlayCreateView
                view, title, section = OverlayCreateView(), "New overlay", AppSection.OverlaysSection
            elif workflow == "releng_create":
                from .releng_create_view import RelengCreateView
                view, title, section = RelengCreateView(), "New Releng directory", AppSection.RelengSection
            elif workflow == "toolset_create":
                from .toolset_create_view import ToolsetCreateView
                view, title, section = ToolsetCreateView(), "New toolset", AppSection.EnvironmentsSection
            elif workflow == "vm_runner_create":
                from .build_machine_create_view import VMRunnerCreateView
                view, title, section = VMRunnerCreateView(), "New VM runner", AppSection.EnvironmentsSection
            elif workflow == "project_update":
                from .project_update_view import ProjectUpdateView
                view, title, section = ProjectUpdateView(project, source), f"Update {project.name}", AppSection.ProjectsSection
            elif workflow == "project_deploy":
                from .project_deploy_view import ProjectDeployView
                view = ProjectDeployView(project_directory=project)
                app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.DeploySection)
                app_event_bus.emit(AppEvents.PUSH_VIEW, view, title=f"Deploy {project.name}")
                return f"Opened deployment options for {project.name}."
            elif workflow == "vm_launch":
                from .qemu_launch_wizard import QemuLaunchWizard
                view = QemuLaunchWizard(project, launch_build)
                app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.BuildsSection)
                app_event_bus.emit(AppEvents.PRESENT_VIEW, view,
                                   f"Run {launch_build.stage_name}", 1024, 768)
                return f"Opened VM launch options for {project.name} build {launch_build.timestamp}."
            else:
                raise ValueError(f"Unsupported workflow: {workflow}")
            app_event_bus.emit(AppEvents.OPEN_APP_SECTION, section)
            app_event_bus.emit(AppEvents.PRESENT_VIEW, view, title, 640, 560)
            return f"Opened {title} workflow. Complete and confirm it in the app."
        return self._on_main_thread(action)

    def _list_operations(self, _arguments):
        from .multistage_process import MultiStageProcess, MultiStageProcessState
        from .build_queue import BuildQueue
        operations = []
        results = []
        for process in MultiStageProcess.started_processes:
            if process.status in (MultiStageProcessState.COMPLETED, MultiStageProcessState.FAILED):
                if getattr(process, "result_seen", False):
                    continue
                project = getattr(process, "project_directory", None)
                results.append({"id": f"op:{id(process)}", "title": process.title,
                                "status": "stopped" if getattr(process, "canceled", False)
                                else process.status.name.lower(),
                                "progress": process.progress,
                                "project": getattr(project, "name", None),
                                "timestamp": getattr(process, "timestamp", None)})
                continue
            if process.status != MultiStageProcessState.IN_PROGRESS:
                continue
            project = getattr(process, "project_directory", None)
            active = next((step.name for step in process.stages
                           if getattr(step.state, "name", "") == "IN_PROGRESS"), None)
            operation = {"id": f"op:{id(process)}", "title": process.title,
                         "status": process.status.name.lower(), "progress": process.progress,
                         "current_step": active, "project": getattr(project, "name", None),
                         "timestamp": getattr(process, "timestamp", None)}
            package_progress = self._active_portage_progress(process)
            if package_progress is not None:
                operation["portage"] = package_progress
            operations.append(operation)
        from .qemu_run import QemuRun
        for run in QemuRun.running_runs:
            if not run.is_running:
                continue
            operations.append({
                "id": f"vm:{id(run)}",
                "title": f"VM: {run.name} — {run.backend_name}",
                "status": "running",
                "project": getattr(run.project_directory, "name", None),
                "stage": getattr(run.build, "stage_name", None),
                "backend": run.backend_name,
                "cpus": run.cpus,
                "memory_gib": round(run.memory_mib / 1024, 1),
            })
        queue = BuildQueue.shared()
        queued = [{"id": f"queue:{item.id}", "title": f"Build {item.project_directory.name}" if item.project_directory else "Queued build",
                   "status": "queued", "project": item.project_directory.name if item.project_directory else None,
                   "stages": item.stage_names}
                  for item in queue.entries]
        return self._json({"active": operations, "results": results, "queued_builds": queued,
                           "queue_paused": queue.paused, "queue_pause_reason": queue.pause_reason})

    def _open_operation(self, arguments):
        operation_id = str(arguments.get("operation_id", "")).strip()
        if not operation_id.startswith(("op:", "vm:", "queue:")):
            raise ValueError("Pass an exact op:, vm:, or queue: ID from list_operations")

        def action():
            if operation_id.startswith("op:"):
                from .multistage_process import MultiStageProcess, MultiStageProcessState
                process = next((item for item in MultiStageProcess.started_processes
                                if f"op:{id(item)}" == operation_id
                                and (item.status == MultiStageProcessState.IN_PROGRESS
                                     or (item.status in (MultiStageProcessState.COMPLETED,
                                                         MultiStageProcessState.FAILED)
                                         and not getattr(item, "result_seen", False)))), None)
                if process is None:
                    raise ValueError("That operation is no longer available to open")
                from .main_window_processes_list import present_process
                present_process(process)
                return self._json({"status": "opened", "id": operation_id,
                                   "title": process.title})

            if operation_id.startswith("vm:"):
                from .qemu_run import QemuRun
                run = next((item for item in QemuRun.running_runs
                            if f"vm:{id(item)}" == operation_id and item.is_running), None)
                if run is None:
                    raise ValueError("That VM is no longer running")
                from .qemu_run_view import present_qemu_run
                present_qemu_run(run)
                return self._json({"status": "opened", "id": operation_id,
                                   "title": f"VM: {run.name}"})

            from .build_queue import BuildQueue
            entry = next((item for item in BuildQueue.shared().entries
                          if f"queue:{item.id}" == operation_id), None)
            if entry is None:
                raise ValueError("That queued build is no longer available")
            from .build_queue_view import present_queued_build
            present_queued_build(entry)
            return self._json({"status": "opened", "id": operation_id,
                               "title": f"Build {entry.project_directory.name}" if entry.project_directory
                               else "Queued build"})

        return self._on_main_thread(action)

    def _read_operation_log(self, arguments):
        operation_id = str(arguments.get("operation_id", "")).strip()
        if not operation_id.startswith("op:"):
            raise ValueError("Pass an active background operation ID from list_operations")
        try:
            limit = int(arguments.get("limit", 120))
        except (TypeError, ValueError) as error:
            raise ValueError("limit must be a whole number") from error
        if not 1 <= limit <= 400:
            raise ValueError("limit must be between 1 and 400")

        from .multistage_process import MultiStageProcess, MultiStageProcessState
        process = next((item for item in MultiStageProcess.started_processes
                        if f"op:{id(item)}" == operation_id
                        and item.status == MultiStageProcessState.IN_PROGRESS), None)
        if process is None:
            raise ValueError("That active operation is no longer available")
        active = next((step for step in process.stages
                       if getattr(getattr(step, "state", None), "name", "") == "IN_PROGRESS"), None)
        if active is None:
            raise ValueError("The operation has no running step with output yet")
        lines = list(getattr(active, "output_lines", []))
        original_count = len(lines)

        # Deployment tools can print shell-command output. Remove secret values
        # already held by the local operation before returning any of its logs.
        secrets = []
        for settings in (getattr(process, "settings", None), getattr(process, "system", None)):
            if settings is not None:
                secrets.extend([getattr(settings, "root_password", None),
                                *[getattr(user, "password", None)
                                  for user in getattr(settings, "users", [])],
                                getattr(getattr(settings, "wifi", None), "password", None),
                                *getattr(settings, "ssh_keys", [])])
        secrets = sorted({str(secret) for secret in secrets
                          if isinstance(secret, str) and secret}, key=len, reverse=True)
        safe_lines = []
        for line in lines[-limit:]:
            line = str(line)
            for secret in secrets:
                line = line.replace(secret, "[REDACTED]")
            safe_lines.append(line)
        return self._json({"operation_id": operation_id, "operation": process.title,
                           "step": active.name, "status": active.state.name.lower(),
                           "line_count": original_count, "returned_lines": len(safe_lines),
                           "truncated": original_count > len(safe_lines), "lines": safe_lines})

    def _read_vm_output(self, arguments):
        operation_id = str(arguments.get("operation_id", "")).strip()
        if not operation_id.startswith("vm:"):
            raise ValueError("Pass a vm:<id> operation ID from list_operations")
        source = str(arguments.get("source", "log")).strip().lower()
        if source not in ("log", "console"):
            raise ValueError("source must be 'log' or 'console'")
        try:
            limit = int(arguments.get("limit", 120))
        except (TypeError, ValueError) as error:
            raise ValueError("limit must be a whole number") from error
        if not 1 <= limit <= 400:
            raise ValueError("limit must be between 1 and 400")

        from .qemu_run import QemuRun
        run = next((item for item in QemuRun.running_runs
                    if f"vm:{id(item)}" == operation_id and item.is_running), None)
        if run is None:
            raise ValueError("That virtual machine is no longer running")
        if source == "console":
            output = str(run.serial_output)
            lines = output.splitlines()
            # The console buffer is retained by QEMU as a bounded 1 MiB tail.
            bounded = lines[-limit:]
            return self._json({"operation_id": operation_id, "source": source,
                               "project": getattr(run.project_directory, "name", None),
                               "stage": getattr(run.build, "stage_name", None),
                               "line_count": len(lines), "returned_lines": len(bounded),
                               "truncated": len(lines) > len(bounded), "lines": bounded})

        lines = [str(line) for line in run.output]
        bounded = lines[-limit:]
        return self._json({"operation_id": operation_id, "source": source,
                           "project": getattr(run.project_directory, "name", None),
                           "stage": getattr(run.build, "stage_name", None),
                           "line_count": len(lines), "returned_lines": len(bounded),
                           "truncated": len(lines) > len(bounded), "lines": bounded})

    @staticmethod
    def _active_portage_progress(process):
        """Return package-level progress from the active stage's captured Portage output."""
        from .emerge_progress import EmergeProgress
        active_stage = next((step for step in getattr(process, "stages", [])
                             if getattr(getattr(step, "state", None), "name", "") == "IN_PROGRESS"
                             and hasattr(step, "output_lines")), None)
        if active_stage is None:
            return None
        output_lines = getattr(active_stage, "output_lines", [])
        if not output_lines:
            return None

        parser = EmergeProgress(ignore_seed_update=True)
        current_package = None
        current_is_binary = False
        emerging = re.compile(r">>> Emerging(?P<binary> binary)? \(\d+ of \d+\)\s+(?P<package>\S+)")
        completed = re.compile(r">>> Completed(?P<binary> binary)? \(\d+ of \d+\)\s+(?P<package>\S+)")
        source_completed, binary_completed = set(), set()
        for line in output_lines:
            parser.parse(line)
            # A completed package clears the in-flight package until the next emerge line.
            # Split carriage-return updates because Portage can rewrite a terminal line in place.
            for part in re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(line)).split("\r"):
                match = emerging.search(part)
                if match:
                    current_package = match.group("package")
                    current_is_binary = bool(match.group("binary"))
                elif match := completed.search(part):
                    (binary_completed if match.group("binary") else source_completed).add(match.group("package"))
                    current_package = None
                    current_is_binary = False

        if parser.total <= 0:
            return None
        count = max(0, min(parser.completed, parser.total))
        result = {"tasks_completed": count, "tasks_total": parser.total,
                  "source_packages_completed": len(source_completed),
                  "binary_packages_completed": len(binary_completed),
                  "percent": round(count * 100 / parser.total, 1)}
        stage = getattr(active_stage, "stage", None)
        if stage is not None:
            result["stage"] = getattr(stage, "name", None)
        if current_package:
            result["current_package"] = current_package
            result["current_package_is_binary"] = current_is_binary
        return result

    def _send_vm_console_input(self, arguments):
        operation_id = str(arguments.get("operation_id", "")).strip()
        if not operation_id.startswith("vm:"):
            raise ValueError("Pass a vm:<id> operation ID from list_operations")
        text = arguments.get("text")
        key = str(arguments.get("key", "")).strip().lower()
        if bool(text) == bool(key):
            raise ValueError("Provide exactly one of text or key")
        key_bytes = {
            "enter": b"\r", "backspace": b"\x7f", "tab": b"\t", "escape": b"\x1b",
            "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
        }
        if key:
            if key.startswith("ctrl_") and len(key) == 6 and "a" <= key[-1] <= "z":
                key_bytes[key] = bytes([ord(key[-1]) - ord("a") + 1])
            if key not in key_bytes:
                raise ValueError("Unsupported console key")
            if arguments.get("press_enter", False):
                raise ValueError("Use key='enter' instead of press_enter with a key")
            payload = key_bytes[key]
        else:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("text must contain the exact text to send")
            if len(text) > 1000:
                raise ValueError("text cannot exceed 1000 characters")
            if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in text):
                raise ValueError("text must contain printable characters; use key for special keys")
            payload = text.encode("utf-8")
        press_enter = arguments.get("press_enter", False)
        if not isinstance(press_enter, bool):
            raise ValueError("press_enter must be a boolean")
        if text and press_enter:
            payload += b"\r"

        def action():
            from .qemu_run import QemuRun
            run = next((item for item in QemuRun.running_runs
                        if f"vm:{id(item)}" == operation_id and item.is_running), None)
            if run is None:
                raise ValueError("That virtual machine is no longer running")
            if hasattr(run, "ready") and not run.ready:
                raise ValueError("The virtual machine is not ready for console input")
            if hasattr(run, "_serial") and run._serial is None:
                raise ValueError("The VM serial console is not connected")
            run.send_serial(payload)
            return self._json({"status": "sent", "operation_id": operation_id,
                               "character_count": len(text or ""), "key_sent": key or None,
                               "enter_sent": bool(text and press_enter) or key == "enter",
                               "delivery_confirmed": False})

        return self._on_main_thread(action)

    def _stop_operation(self, arguments):
        operation_id = str(arguments.get("operation_id", "")).strip()
        if not operation_id:
            raise ValueError("Pass an operation ID from list_operations")
        def action():
            from .multistage_process import MultiStageProcess, MultiStageProcessState
            from .build_queue import BuildQueue
            from .qemu_run import QemuRun
            if operation_id.startswith("op:"):
                process = next((item for item in MultiStageProcess.started_processes
                                if f"op:{id(item)}" == operation_id), None)
                if process is None or process.status != MultiStageProcessState.IN_PROGRESS:
                    raise ValueError("That active operation is no longer available")
                process.cancel()
                return f"Cancellation requested for {process.title}."
            if operation_id.startswith("vm:"):
                run = next((item for item in QemuRun.running_runs
                            if f"vm:{id(item)}" == operation_id and item.is_running), None)
                if run is None:
                    raise ValueError("That virtual machine is no longer running")
                run.stop()
                return f"Stop requested for VM {run.name} ({run.backend_name})."
            if operation_id.startswith("queue:"):
                entry_id = operation_id.removeprefix("queue:")
                entry = next((item for item in BuildQueue.shared().entries if item.id == entry_id), None)
                if entry is None:
                    raise ValueError("That queued build is no longer available")
                title = entry.project_directory.name if entry.project_directory else "build"
                BuildQueue.shared().remove(entry)
                return f"Removed queued build for {title}."
            raise ValueError("Unknown operation ID")
        return self._on_main_thread(action)

    def _control_vm(self, arguments):
        operation_id = str(arguments.get("operation_id", "")).strip()
        action_name = str(arguments.get("action", "")).strip().lower()
        if not operation_id.startswith("vm:"):
            raise ValueError("Pass a VM ID from list_operations (vm:<id>)")
        if action_name not in ("power_off", "reset", "send_ctrl_alt_delete"):
            raise ValueError("VM action must be power_off, reset, or send_ctrl_alt_delete")

        def action():
            from .qemu_run import QemuRun
            run = next((item for item in QemuRun.running_runs
                        if f"vm:{id(item)}" == operation_id and item.is_running), None)
            if run is None:
                raise ValueError("That virtual machine is no longer running")
            if action_name == "power_off":
                run.power_off()
            elif action_name == "reset":
                if not hasattr(run, "reset"):
                    raise ValueError(f"Reset is not supported by the {run.backend_name} backend")
                run.reset()
            else:
                if not hasattr(run, "send_ctrl_alt_delete"):
                    raise ValueError(f"Ctrl+Alt+Delete is not supported by the {run.backend_name} backend")
                run.send_ctrl_alt_delete()
            labels = {"power_off": "Requested guest shutdown for",
                      "reset": "Reset",
                      "send_ctrl_alt_delete": "Sent Ctrl+Alt+Delete to"}
            return f"{labels[action_name]} VM {run.name} ({run.backend_name})."
        return self._on_main_thread(action)

    def _resume_build_queue(self, _arguments):
        def action():
            from .build_queue import BuildQueue
            queue = BuildQueue.shared()
            if not queue.entries:
                return self._json({"status": "empty", "message": "There are no queued builds."})
            count = len(queue.entries)
            was_paused = queue.paused
            queue.start()
            return self._json({
                "status": "resumed" if was_paused else "already_running",
                "queued_builds": count,
                "message": ("Queue resumed; its next build will start when available."
                            if was_paused else "Queue is already active; queued builds will start when available."),
            })
        return self._on_main_thread(action)

    def _update_settings(self, arguments):
        setting = str(arguments.get("setting", "")).strip()
        value = arguments.get("value")
        allowed = {"navigation_style", "pause_queue_on_failure", "keep_root_unlocked", "qemu_cpus", "qemu_memory_gib",
                   "auto_clean_snapshots", "default_toolset_id", "default_runner_id",
                   "project_stages_view_mode", "git_identity_mode", "git_user_name", "git_user_email",
                   "ollama_url", "ollama_model"}
        if setting not in allowed:
            raise ValueError(f"Unsupported preference: {setting}")
        if setting in ("pause_queue_on_failure", "keep_root_unlocked", "auto_clean_snapshots") and not isinstance(value, bool):
            raise ValueError(f"{setting} needs a true/false value")
        if setting == "auto_clean_snapshots" and value:
            from .repositories import Repository
            if not Repository.Settings.value.auto_clean_snapshots:
                from .snapshot_manager import SnapshotManager
                unused = SnapshotManager.shared().unused_snapshots()
                if unused:
                    if self.user_input_callback is None:
                        raise RuntimeError("Enabling this preference requires confirmation because it removes unused snapshots")
                    answer = self.user_input_callback(
                        "Remove unused snapshots?",
                        "Enabling automatic cleanup will remove these snapshots now: " +
                        ", ".join(item.name for item in unused),
                        [{"id": "confirm", "label": "Remove them and enable automatic cleanup",
                          "type": "boolean", "placeholder": "Confirm removal", "required": True}],
                    )
                    if not answer or not answer.get("confirm"):
                        return "Canceled; automatic snapshot cleanup remains disabled."
        def action():
            from .repositories import Repository
            from .settings import NavigationStyle
            settings = Repository.Settings.value
            if setting in ("pause_queue_on_failure", "keep_root_unlocked", "auto_clean_snapshots"):
                setattr(settings, setting, value)
            elif setting == "navigation_style":
                try:
                    settings.navigation_style = NavigationStyle(str(value))
                except ValueError as error:
                    raise ValueError("navigation_style must be 'modal' or 'push'") from error
            elif setting in ("qemu_cpus", "qemu_memory_gib"):
                try:
                    number = int(value)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"{setting} must be an integer") from error
                maximum = 64 if setting == "qemu_cpus" else 256
                if not 1 <= number <= maximum:
                    raise ValueError(f"{setting} must be between 1 and {maximum}")
                setattr(settings, setting, number)
            elif setting == "default_toolset_id":
                if str(value).lower() in ("default", "app default", ""):
                    settings.default_toolset_id = None
                else:
                    toolset = next((item for item in Repository.Toolset.value
                                    if str(item.uuid) == str(value) or item.name == str(value)), None)
                    if toolset is None:
                        raise ValueError("No toolset matches that ID or name")
                    settings.default_toolset_id = str(toolset.uuid)
            elif setting == "default_runner_id":
                from .runner import HOST_RUNNER, runner_by_id
                if str(value).lower() in ("default", "app default", ""):
                    settings.default_runner_id = None
                elif str(value).lower() == "host":
                    runner = HOST_RUNNER
                    settings.default_runner_id = str(runner.id)
                else:
                    runner = runner_by_id(str(value))
                    if runner is None:
                        raise ValueError("No runner matches that ID")
                    settings.default_runner_id = str(runner.id)
            elif setting == "project_stages_view_mode":
                if value not in ("compact", "full", "list"):
                    raise ValueError("project_stages_view_mode must be compact, full, or list")
                settings.project_stages_view_mode = value
            elif setting == "git_identity_mode":
                if value not in ("system", "custom"):
                    raise ValueError("git_identity_mode must be system or custom")
                settings.git_identity_mode = value
            else:
                if not isinstance(value, str):
                    raise ValueError(f"{setting} must be text")
                setattr(settings, setting, value)
            return f"Updated setting {setting}."
        return self._on_main_thread(action)

    def _get_settings(self, _arguments):
        def action():
            from .repositories import Repository
            settings = Repository.Settings.value
            names = (
                "navigation_style", "pause_queue_on_failure", "keep_root_unlocked", "qemu_cpus", "qemu_memory_gib",
                "auto_clean_snapshots", "default_toolset_id", "default_runner_id",
                "project_stages_view_mode", "git_identity_mode", "git_user_name", "git_user_email",
                "ollama_model", "library_location",
            )
            values = {name: getattr(settings, name, None) for name in names}
            values["navigation_style"] = settings.navigation_style.value
            from .ollama_client import redact_server_url
            values["ollama_url"], values["ollama_url_redacted"] = redact_server_url(settings.ollama_url)
            from ..global_objects.git_identity import effective_git_identity, system_git_identity
            system_identity = system_git_identity()
            effective_identity = effective_git_identity()
            values["git_identity"] = {
                "mode": settings.git_identity_mode,
                "effective": {"name": effective_identity.name, "email": effective_identity.email,
                              "configured": effective_identity.configured},
                "system": {"name": system_identity.name, "email": system_identity.email,
                           "configured": system_identity.configured},
            }
            from .repository import collapse_home
            values["library_location"] = collapse_home(settings.library_location)
            return self._json(values)
        return self._on_main_thread(action)

    def _get_root_access_status(self, _arguments):
        def action():
            import sys
            from ..root_helper.root_helper_client import RootHelperClient
            from .repositories import Repository
            client = RootHelperClient.shared()
            with client.set_request_status_lock:
                actions = [{"call_id": str(call.call_id),
                            "name": getattr(call.request, "function_name", str(call.request)),
                            "cancellable": call.is_cancellable,
                            "terminating": call.terminated}
                           for call in client.running_actions]
            return self._json({
                "platform_supported": sys.platform.startswith("linux"),
                "enabled": bool(client.is_server_process_running),
                "keep_unlocked": bool(Repository.Settings.value.keep_root_unlocked),
                "running_actions": actions,
            })
        return self._on_main_thread(action)

    def _open_root_action_output(self, arguments):
        call_id = str(arguments.get("call_id", "")).strip()
        if not call_id:
            raise ValueError("Pass the exact call_id returned by get_root_access_status")

        def action():
            from ..root_helper.root_helper_client import RootHelperClient
            client = RootHelperClient.shared()
            with client.set_request_status_lock:
                call = next((item for item in client.running_actions
                             if str(item.call_id) == call_id), None)
            if call is None:
                raise ValueError("No active root action has that call_id; refresh get_root_access_status")
            from ...ui.components.root_command_output.root_command_output_view import RootCommandOutputView
            from ...ui.app_sections.app_events import app_event_bus, AppEvents
            title = getattr(call.request, "function_name", str(call.request))
            app_event_bus.emit(AppEvents.PRESENT_VIEW, RootCommandOutputView(call), title, 640, 480)
            return self._json({"status": "opened", "call_id": call_id, "output_in_model": False})

        return self._on_main_thread(action)

    def _cancel_root_action(self, arguments):
        call_id = str(arguments.get("call_id", "")).strip()
        if not call_id:
            raise ValueError("Pass the exact call_id returned by get_root_access_status")

        def action():
            from ..root_helper.root_helper_client import RootHelperClient
            client = RootHelperClient.shared()
            with client.set_request_status_lock:
                call = next((item for item in client.running_actions
                             if str(item.call_id) == call_id), None)
                if call is None:
                    raise ValueError("No active root action has that call_id; refresh get_root_access_status")
                if not call.is_cancellable:
                    raise ValueError("That root action is not cancellable")
                if call.terminated:
                    return self._json({"status": "already_terminating", "call_id": call_id,
                                       "name": getattr(call.request, "function_name", str(call.request))})
                name = getattr(call.request, "function_name", str(call.request))
            call.cancel()
            return self._json({"status": "cancellation_requested", "call_id": call_id,
                               "name": name,
                               "message": "Cancellation was requested; refresh get_root_access_status to see whether it completed."})

        return self._on_main_thread(action)

    def _set_root_access(self, arguments):
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")

        def action():
            import sys
            from ..root_helper.root_helper_client import RootHelperClient
            client = RootHelperClient.shared()
            with client.set_request_status_lock:
                running_actions = list(client.running_actions)
            if enabled:
                if not sys.platform.startswith("linux"):
                    raise RuntimeError("The root helper is available only on Linux")
                if client.is_server_process_running:
                    return self._json({"status": "already_enabled", "enabled": True})
                if running_actions:
                    raise RuntimeError("Wait for the active privileged actions before enabling root access")

                def keep_enabled(keeper):
                    if keeper is not None:
                        keeper.ignore_released = True

                client.authorize_and_run(name="Enable root access from chat", callback=keep_enabled)
                return self._json({"status": "authorization_requested", "enabled": None,
                                   "message": "Complete or cancel the operating-system authorization prompt, then check get_root_access_status."})

            if running_actions:
                raise RuntimeError("Cannot disable root access while privileged actions are running")
            if not client.is_server_process_running:
                return self._json({"status": "already_disabled", "enabled": False})
            client.stop_root_helper()
            return self._json({"status": "disabled", "enabled": False})

        return self._on_main_thread(action)

    def _change_library_location(self, arguments):
        from ..global_objects.library import LibraryChange, change_library, change_problem, library_target
        from ..global_objects.repository import collapse_home, library_directory
        path = str(arguments.get("path", "")).strip()
        if not path or "\0" in path or "\n" in path or "\r" in path:
            raise ValueError("Pass the destination folder path supplied by the user")
        target = library_target(path)
        modes = ([LibraryChange.SWITCH] if target.existing else [
            LibraryChange.NEW, LibraryChange.COPY, LibraryChange.MOVE])
        mode_value = str(arguments.get("mode", "")).strip()
        if not mode_value:
            if self.user_input_callback is None:
                raise RuntimeError("Choose the Library change mode in chat")
            selected = self.user_input_callback(
                "Choose how to use this Library location",
                f"Destination: {collapse_home(target.path)}",
                [{"id": "mode", "label": "Library action", "type": "select", "required": True,
                  "options": [{"value": mode.value, "label": {
                      LibraryChange.SWITCH: "Switch to this existing Library",
                      LibraryChange.NEW: "Create a new empty Library",
                      LibraryChange.COPY: "Copy the current Library",
                      LibraryChange.MOVE: "Move the current Library",
                  }[mode]} for mode in modes]}],
            )
            mode_value = str((selected or {}).get("mode", "")).strip()
        try:
            mode = LibraryChange(mode_value)
        except ValueError as error:
            raise ValueError("Choose a valid Library action") from error
        if mode not in modes:
            raise ValueError("That action is not valid for the selected destination")
        if problem := change_problem(target, mode):
            raise ValueError(problem)

        def busy_items():
            from ...ui.app_sections.settings.settings_section import items_using_library
            return items_using_library()

        items = self._on_main_thread(busy_items)
        if items:
            raise ValueError("The Library is in use. Stop these first: " + ", ".join(items))
        old_location = collapse_home(library_directory())
        action_summary = {
            LibraryChange.SWITCH: f"switch to the existing Library at {collapse_home(target.path)}; current Library stays at {old_location}",
            LibraryChange.NEW: f"create an empty Library at {collapse_home(target.path)}; current Library stays at {old_location}",
            LibraryChange.COPY: f"copy the current Library from {old_location} to {collapse_home(target.path)}",
            LibraryChange.MOVE: f"move the current Library from {old_location} to {collapse_home(target.path)}",
        }[mode]
        if self.user_input_callback is None:
            raise RuntimeError("This Library change needs an in-chat confirmation")
        confirmation = self.user_input_callback(
            "Confirm Library change",
            f"Catalyst Lab will {action_summary}. The app will restart after this assistant reply.",
            [{"id": "confirm", "label": "Confirm and restart Catalyst Lab", "type": "boolean",
              "default": False, "required": True}],
        )
        if not confirmation or not confirmation.get("confirm"):
            return "Canceled; the Library location was not changed."
        items = self._on_main_thread(busy_items)
        if items:
            raise ValueError("The Library became busy. Stop these first: " + ", ".join(items))
        progress_callback = self.tool_progress_callback

        def report(fraction, status):
            if progress_callback is not None:
                progress_callback("Library", {"status": status, "total": 1000,
                                               "completed": int(max(0, min(1, fraction)) * 1000)})

        warnings = change_library(target, mode, report)
        self.restart_after_turn = True
        return self._json({"status": "changed", "mode": mode.value,
                           "library_location": collapse_home(target.path),
                           "restart_required": True, "warnings": warnings})

    @staticmethod
    def _bug_failure_id(failure):
        return f"{failure.project_directory.id}:{failure.build.timestamp}:{failure.index}"

    def _list_bugs(self, arguments):
        from .build_failures import collect_package_failures
        project_key = str(arguments.get("project", "")).strip()
        query = str(arguments.get("query", "")).strip().casefold()
        status = str(arguments.get("status", "")).strip()
        if status and status not in {"open", "solved", "potentially_solved", "potentially_resurfaced"}:
            raise ValueError("status must be open, solved, potentially_solved, or potentially_resurfaced")
        try:
            limit = max(1, min(int(arguments.get("limit", 100)), 500))
            offset = max(0, min(int(arguments.get("offset", 0)), 1_000_000))
        except (TypeError, ValueError):
            limit = 100
            offset = 0
        group_by = str(arguments.get("group_by", "")).strip().lower()
        if group_by and group_by not in {"issue", "package", "project", "stage", "date"}:
            raise ValueError("group_by must be issue, package, project, stage, or date")
        failures = collect_package_failures()
        if project_key:
            project = self._project(project_key)
            failures = [item for item in failures if item.project_directory is project]
        if query:
            failures = [item for item in failures if query in " ".join(str(value) for value in (
                item.package, item.repository, item.phase or "", item.reason or "",
                item.project_directory.name, item.build.stage_name,
            )).casefold()]
        if status:
            failures = [item for item in failures if item.resolution_status == status]

        def failure_row(item):
            failure_id = self._bug_failure_id(item)
            return {
                "failure_id": failure_id,
                "id": failure_id,
                "issue_key": list(item.issue_key) if item.issue_key else None,
                "project": item.project_directory.name,
                "project_id": str(item.project_directory.id),
                "stage": item.build.stage_name,
                "stage_id": str(item.build.stage_id),
                "build_timestamp": item.build.timestamp,
                "package": item.package,
                "repository": item.repository,
                "phase": item.phase,
                "reason": item.reason,
                "status": item.resolution_status,
                "date": item.date.isoformat(),
                "log_available": bool(item.log_path),
                "log_file": (os.path.relpath(item.log_path, item.build.path)
                             if item.log_path and item.build.path else None),
            }

        page_failures = failures[offset:offset + limit]
        if not group_by:
            return self._json([failure_row(item) for item in page_failures])

        groups = {}
        for item in page_failures:
            if group_by == "issue":
                key = tuple(item.issue_key) if item.issue_key else ("unknown", self._bug_failure_id(item))
                group_key = list(key)
                label = f"{item.package} · {item.project_directory.name} · {item.build.stage_name}"
            elif group_by == "package":
                key = item.atom
                group_key, label = key, item.atom
            elif group_by == "project":
                key = str(item.project_directory.id)
                group_key, label = key, item.project_directory.name
            elif group_by == "stage":
                key = (str(item.project_directory.id), str(item.build.stage_id))
                group_key = list(key)
                label = f"{item.project_directory.name} · {item.build.stage_name}"
            else:
                key = item.date.date().isoformat()
                group_key, label = key, item.date.strftime("%Y-%m-%d")
            group = groups.setdefault(key, {"key": group_key, "label": label, "failures": []})
            group["failures"].append(failure_row(item))
        return self._json({
            "group_by": group_by,
            "total_failures": len(failures),
            "offset": offset,
            "limit": limit,
            "returned_failures": len(page_failures),
            "next_offset": offset + len(page_failures) if offset + len(page_failures) < len(failures) else None,
            "groups": [{"key": group["key"], "label": group["label"],
                        "count": len(group["failures"]), "failures": group["failures"]}
                       for group in groups.values()],
        })

    def _open_bugzilla(self, arguments):
        failure = self._resolve_bug_failure(arguments.get("failure_id", ""))
        if not failure.is_gentoo:
            raise ValueError("The Bugs section only links Gentoo repository failures to Gentoo Bugzilla")
        from .build_failures import bugzilla_url
        url = bugzilla_url(failure.atom)

        def action():
            from gi.repository import Gtk
            parent = self.content_navigation_view.get_root() if self.content_navigation_view else None
            if parent is None:
                application = Gtk.Application.get_default()
                parent = application.get_active_window() if application else None
            if parent is None:
                raise RuntimeError("The app window is unavailable for opening Bugzilla")
            Gtk.UriLauncher.new(url).launch(parent, None, None)
            return self._json({"status": "opened", "url": url,
                               "package": failure.atom, "failure_id": self._bug_failure_id(failure)})
        return self._on_main_thread(action)

    def _set_bug_status(self, arguments):
        failures = self._resolve_bug_failures(arguments)
        status = str(arguments.get("status", "")).strip().lower()
        if status not in ("open", "solved", "potentially_solved", "potentially_resurfaced"):
            raise ValueError("Unknown bug resolution status")
        def action():
            updated, errors = [], []
            for failure in failures:
                try:
                    failure.set_resolution_status(status)
                    updated.append(self._bug_failure_id(failure))
                except Exception as error:
                    errors.append({"failure_id": self._bug_failure_id(failure), "error": str(error)})
            result = {"status": "failed" if not updated else "partial" if errors else "updated",
                      "resolution": status,
                      "updated_count": len(updated), "failure_ids": updated,
                      "project": failures[0].project_directory.name,
                      "package": failures[0].atom}
            if errors:
                result["errors"] = errors
            return self._json(result)
        return self._on_main_thread(action)

    def _remove_bug(self, arguments):
        failures = self._resolve_bug_failures(arguments)
        if self.user_input_callback is None:
            raise RuntimeError("Removing a failure requires an in-chat confirmation")
        answer = self.user_input_callback("Remove failed package failures?",
                                          f"Remove {len(failures)} failure(s) of {failures[0].atom} from the Bugs list? Their builds and logs will remain.", [
            {"id": "confirm", "label": f"Remove {len(failures)} failure(s) from Bugs", "type": "boolean",
             "placeholder": "Confirm removal", "required": True}])
        if answer and answer.get("__user_feedback__"):
            return self._json({"status": "revision_requested",
                               "message": str(answer["__user_feedback__"])[:2000]})
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "failure_ids": [self._bug_failure_id(failure) for failure in failures]})
        def action():
            removed, errors = [], []
            for failure in failures:
                try:
                    failure.remove()
                    removed.append(self._bug_failure_id(failure))
                except Exception as error:
                    errors.append({"failure_id": self._bug_failure_id(failure), "error": str(error)})
            result = {"status": "failed" if not removed else "partial" if errors else "removed",
                      "removed_count": len(removed),
                      "failure_ids": removed, "builds_kept": True}
            if errors:
                result["errors"] = errors
            return self._json(result)
        return self._on_main_thread(action)

    def _resolve_bug_failures(self, arguments):
        failure_id = str(arguments.get("failure_id", "")).strip()
        raw_issue_key = arguments.get("issue_key")
        if bool(failure_id) == (raw_issue_key is not None):
            raise ValueError("Pass exactly one failure_id or issue_key from list_bugs")
        from .build_failures import collect_package_failures
        failures = collect_package_failures()
        if failure_id:
            failure = next((item for item in failures if self._bug_failure_id(item) == failure_id), None)
            if failure is None:
                raise ValueError("That failure is no longer listed; call list_bugs again")
            return [failure]
        if not isinstance(raw_issue_key, list) or not raw_issue_key:
            raise ValueError("issue_key must be the exact array returned by list_bugs")
        expected = tuple(raw_issue_key)
        matches = [item for item in failures if item.issue_key == expected]
        if not matches:
            raise ValueError("That issue group is no longer listed; call list_bugs again")
        return matches

    def _resolve_bug_failure(self, failure_id):
        from .build_failures import collect_package_failures
        failure_id = str(failure_id).strip()
        failure = next((item for item in collect_package_failures()
                        if self._bug_failure_id(item) == failure_id), None)
        if failure is None:
            raise ValueError("That failure is no longer listed; call list_bugs again")
        return failure

    @staticmethod
    def _bug_notes_path(failure):
        import hashlib
        identity = failure.issue_key or (str(failure.project_directory.id),
                                         str(failure.build.id), failure.index)
        encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        issue_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        from .project_build import project_builds_directory
        return os.path.join(project_builds_directory(failure.project_directory), "bugs_notes", issue_id + ".json")

    def _read_bug_notes(self, failure):
        path = self._bug_notes_path(failure)
        try:
            with open(path, encoding="utf-8") as file:
                notes = json.load(file)
        except FileNotFoundError:
            notes = []
        if not isinstance(notes, list):
            raise ValueError("The issue notes file has an invalid format")
        return path, [note for note in notes if isinstance(note, dict)
                      and isinstance(note.get("id"), str)
                      and isinstance(note.get("created_at"), str)
                      and isinstance(note.get("text"), str)]

    @staticmethod
    def _write_bug_notes(path, notes):
        if not notes:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(notes, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)

    def _list_bug_notes(self, arguments):
        failure_id = str(arguments.get("failure_id", "")).strip()
        project_key = str(arguments.get("project", "")).strip()
        package = str(arguments.get("package", "")).strip()
        if failure_id:
            if project_key or package:
                raise ValueError("Pass failure_id alone, or pass both project and package for the package overview")
            failure = self._resolve_bug_failure(failure_id)
            _, notes = self._read_bug_notes(failure)
            return self._json({"failure_id": self._bug_failure_id(failure),
                               "project": failure.project_directory.name,
                               "package": failure.atom,
                               "notes": sorted(notes, key=lambda item: item["created_at"], reverse=True)})
        if not project_key or not package:
            raise ValueError("Pass failure_id, or both exact project and package values from list_bugs")

        project = self._project(project_key)
        from .build_failures import collect_package_failures
        grouped = {}
        for failure in collect_package_failures():
            if failure.project_directory is not project or failure.atom != package:
                continue
            path = self._bug_notes_path(failure)
            entry = grouped.setdefault(path, {"failure": failure, "instances": []})
            entry["instances"].append(failure)

        issues = []
        for path, entry in grouped.items():
            failure = entry["failure"]
            _, notes = self._read_bug_notes(failure)
            if not notes:
                continue
            instances = entry["instances"]
            issues.append({
                "failure_id": self._bug_failure_id(failure),
                "issue_key": list(failure.issue_key) if failure.issue_key else None,
                "stage": failure.build.stage_name,
                "repository": failure.repository or "gentoo",
                "phase": failure.phase,
                "reason": failure.reason,
                "failure_count": len(instances),
                "latest_failure": max(item.date for item in instances).isoformat(),
                "notes": sorted(notes, key=lambda item: item["created_at"], reverse=True),
            })
        issues.sort(key=lambda item: item["latest_failure"], reverse=True)
        try:
            offset = max(0, min(int(arguments.get("offset", 0)), 1_000_000))
            limit = max(1, min(int(arguments.get("limit", 50)), 100))
        except (TypeError, ValueError):
            offset, limit = 0, 50
        total = len(issues)
        page = issues[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < total else None
        return self._json({"project": project.name, "package": package,
                           "total_count": total, "offset": offset, "limit": limit,
                           "returned_count": len(page), "next_offset": next_offset,
                           "issues": page})

    def _add_bug_note(self, arguments):
        failure = self._resolve_bug_failure(arguments.get("failure_id", ""))
        content = str(arguments.get("text", "")).strip()
        if not content or len(content) > 20_000:
            raise ValueError("A note must contain 1–20,000 characters")
        import uuid
        from datetime import datetime
        path, notes = self._read_bug_notes(failure)
        note = {"id": str(uuid.uuid4()), "created_at": datetime.now().astimezone().isoformat(), "text": content}
        notes.insert(0, note)
        self._write_bug_notes(path, notes)
        return self._json({"status": "created", "note": note})

    def _edit_bug_note(self, arguments):
        failure = self._resolve_bug_failure(arguments.get("failure_id", ""))
        note_id = str(arguments.get("note_id", ""))
        content = str(arguments.get("text", "")).strip()
        if not content or len(content) > 20_000:
            raise ValueError("A note must contain 1–20,000 characters")
        path, notes = self._read_bug_notes(failure)
        note = next((item for item in notes if item["id"] == note_id), None)
        if note is None:
            raise ValueError("No note matches that ID")
        from datetime import datetime
        note["text"] = content
        note["updated_at"] = datetime.now().astimezone().isoformat()
        self._write_bug_notes(path, notes)
        return self._json({"status": "updated", "note": note})

    def _delete_bug_note(self, arguments):
        failure = self._resolve_bug_failure(arguments.get("failure_id", ""))
        note_id = str(arguments.get("note_id", ""))
        path, notes = self._read_bug_notes(failure)
        if not any(item["id"] == note_id for item in notes):
            raise ValueError("No note matches that ID")
        if self.user_input_callback is None:
            raise RuntimeError("Deleting a note requires an in-chat confirmation")
        answer = self.user_input_callback("Delete issue note?", "This note will be permanently deleted.", [
            {"id": "confirm", "label": "Permanently delete this note", "type": "boolean",
             "placeholder": "Confirm deletion", "required": True}])
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled"})
        notes = [item for item in notes if item["id"] != note_id]
        self._write_bug_notes(path, notes)
        return self._json({"status": "deleted", "note_id": note_id})

    def _delete_project(self, arguments):
        project = self._project(arguments.get("project", ""))
        if self.user_input_callback is None:
            raise RuntimeError("Project deletion requires an in-chat confirmation form")
        from .project_build_process import running_project_build
        if running_project_build(project) is not None:
            raise RuntimeError("Stop the project's running build before deleting it")
        packages = project.get_packages_directory()
        shared_packages = packages is not None and any(item is not project for item in packages.projects)
        fields = [
            {"id": "confirm", "label": f"Permanently delete project {project.name}", "type": "boolean",
             "placeholder": "I understand this deletes the project files", "required": True},
            {"id": "delete_builds", "label": "Also delete its build history, logs, caches, and downloaded seeds",
             "type": "boolean", "required": True},
        ]
        if packages is not None and not shared_packages:
            fields.append({"id": "delete_packages",
                           "label": "Also delete its binary packages (this permanently removes the package cache)",
                           "type": "boolean", "required": True})
        answers = self.user_input_callback(
            "Confirm project deletion",
            "This is permanent. Choose any optional data to remove. Binary packages shared with another project cannot be deleted.",
            fields,
        )
        if not answers or not answers.get("confirm"):
            return self._json({"status": "cancelled", "project": project.name})
        delete_packages = bool(answers.get("delete_packages"))
        if delete_packages and packages is None:
            raise ValueError("The project's binary package directory is no longer available")

        def action():
            from .project_manager import ProjectManager
            from .repositories import Repository
            if delete_packages:
                from .packages_directory import delete_packages_directory
                delete_packages_directory(packages)
            if answers.get("delete_builds"):
                from .project_build import delete_project_builds
                delete_project_builds(project)
            ProjectManager.shared().remove_directory(project)
            Repository.ProjectDirectory.save()
            from .app_events import AppEvents, app_event_bus
            from .app_section import AppSection
            app_event_bus.emit(AppEvents.OPEN_APP_SECTION, AppSection.ProjectsSection)
            return self._json({"status": "deleted", "project": project.name,
                               "builds_deleted": bool(answers.get("delete_builds")),
                               "binary_packages_deleted": delete_packages})
        return self._on_main_thread(action)

    def _delete_build_record(self, arguments):
        from .project_build import load_project_builds, StageBuildStatus
        project = self._project(arguments.get("project", ""))
        stage_key = str(arguments.get("stage", "")).strip()
        timestamp = str(arguments.get("timestamp", "")).strip()
        if not stage_key or not timestamp:
            raise ValueError("Pass the exact stage ID/name and timestamp from list_builds")
        matches = [build for build in load_project_builds(project)
                   if build.timestamp == timestamp and
                   (build.stage_name == stage_key or str(build.stage_id) == stage_key
                    or build.stage_id.hex == stage_key)]
        if len(matches) != 1:
            raise ValueError("No unique saved build matches that project, stage, and timestamp. Refresh with list_builds.")
        build = matches[0]
        if build.status in {StageBuildStatus.IN_PROGRESS, StageBuildStatus.SCHEDULED}:
            raise RuntimeError("This build record is still active or scheduled and cannot be deleted")

        from .project_build_process import running_project_build
        if running_project_build(project, timestamp) is not None:
            raise RuntimeError("The project build is still using this run; stop it before deleting the record")
        from .qemu_run import QemuRun
        if any(str(getattr(getattr(run, "build", None), "id", "")) == str(build.id)
               for run in QemuRun.running_runs):
            raise RuntimeError("A VM session is using or starting with this build artifact")

        if self.user_input_callback is None:
            raise RuntimeError("Deleting a build record requires an in-chat confirmation")
        answer = self.user_input_callback(
            "Delete saved build?",
            f"Permanently remove {build.stage_name} from {project.name} ({timestamp}), including its artifact, logs, and failure details? Other build records and issue notes will remain.",
            [{"id": "confirm", "label": "Permanently delete this build record", "type": "boolean",
              "placeholder": "Confirm deletion", "required": True}],
        )
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "project": project.name,
                               "stage": build.stage_name, "timestamp": timestamp})

        # Recheck and atomically detach the record on the GTK thread. Starting a build
        # or VM is serialized there, so once hidden under a temporary name, the record
        # cannot become newly active. The expensive recursive deletion stays on the chat worker.
        def reserve_for_deletion():
            if running_project_build(project, timestamp) is not None:
                raise RuntimeError("A build started while you were confirming; the record was not deleted")
            if any(str(getattr(getattr(run, "build", None), "id", "")) == str(build.id)
                   for run in QemuRun.running_runs):
                raise RuntimeError("A VM started using this artifact while you were confirming; it was not deleted")
            from .project_build import builds_location, _is_builds_descendant
            source_path = build.path
            real_root = os.path.realpath(builds_location())
            real_source = os.path.realpath(source_path or "")
            if (not source_path or os.path.islink(source_path)
                    or not _is_builds_descendant(real_source, real_root)):
                raise RuntimeError("Refusing to delete a build record outside the configured Builds folder")
            reserved_path = os.path.join(os.path.dirname(source_path), f".deleting-{build.id.hex}")
            if os.path.lexists(reserved_path):
                raise RuntimeError("A deletion is already in progress for this build record")
            os.replace(source_path, reserved_path)
            build.path = reserved_path
            return source_path, reserved_path

        source_path, reserved_path = self._on_main_thread(reserve_for_deletion)
        try:
            build.delete()
        except Exception:
            # Restore the visible Library record if the recursive removal failed.
            if os.path.lexists(reserved_path) and not os.path.lexists(source_path):
                os.replace(reserved_path, source_path)
            build.path = source_path
            raise
        return self._json({"status": "deleted", "project": project.name,
                           "stage": build.stage_name, "stage_id": str(build.stage_id),
                           "timestamp": timestamp, "build_id": str(build.id),
                           "artifact_and_logs_deleted": True, "issue_notes_deleted": False})

    def _delete_build_run(self, arguments):
        from .project_build import (
            StageBuildStatus, _is_builds_descendant, builds_location,
            load_project_builds, project_builds_directory,
        )
        from .project_build_process import running_project_build
        from .project_build_record import build_records_directory
        from .qemu_run import QemuRun

        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("timestamp", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", timestamp):
            raise ValueError("Pass the exact build timestamp returned by list_builds")
        builds = [build for build in load_project_builds(project)
                  if build.timestamp == timestamp]
        if not builds:
            raise ValueError("No saved build run matches that project and timestamp; refresh with list_builds")
        if any(build.status in {StageBuildStatus.IN_PROGRESS, StageBuildStatus.SCHEDULED}
               for build in builds) or running_project_build(project, timestamp) is not None:
            raise RuntimeError("This build run is active or scheduled; stop it before deleting the run")

        build_ids = {str(build.id) for build in builds}
        if any(str(getattr(getattr(run, "build", None), "id", "")) in build_ids
               for run in QemuRun.running_runs):
            raise RuntimeError("A VM session is using or starting with this build run")
        if self.user_input_callback is None:
            raise RuntimeError("Deleting a build run requires an in-chat confirmation")
        stage_names = [build.stage_name for build in sorted(
            builds, key=lambda item: item.order if item.order is not None else 1_000_000)]
        answer = self.user_input_callback(
            "Delete saved build run?",
            f"Permanently remove all {len(builds)} stage build record(s) for {project.name} ({timestamp}), including artifacts, logs, and failure details?\n"
            + "Stages: " + ", ".join(stage_names)
            + "\nIssue notes linked to failures will be preserved.",
            [{"id": "confirm", "label": "Permanently delete this build run",
              "type": "boolean", "placeholder": "Confirm deletion", "required": True}],
        )
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "project": project.name,
                               "timestamp": timestamp, "stage_build_count": len(builds)})

        project_root = project_builds_directory(project)
        record_path = os.path.join(build_records_directory(project), f"{timestamp}.json")
        reservation_root = os.path.join(
            project_root, f".deleting-run-{uuid.uuid4().hex}")

        def reserve_run():
            if running_project_build(project, timestamp) is not None:
                raise RuntimeError("A build started while you were confirming; nothing was deleted")
            current = [build for build in load_project_builds(project)
                       if build.timestamp == timestamp]
            if {str(build.id) for build in current} != build_ids:
                raise RuntimeError("The build run changed while you were confirming; refresh list_builds")
            if any(build.status in {StageBuildStatus.IN_PROGRESS, StageBuildStatus.SCHEDULED}
                   for build in current):
                raise RuntimeError("A stage became active while you were confirming; nothing was deleted")
            if any(str(getattr(getattr(run, "build", None), "id", "")) in build_ids
                   for run in QemuRun.running_runs):
                raise RuntimeError("A VM started using this build run while you were confirming")

            root = os.path.realpath(builds_location())
            real_project_root = os.path.realpath(project_root)
            if (os.path.islink(project_root)
                    or not _is_builds_descendant(real_project_root, root)
                    or not os.path.isdir(real_project_root)):
                raise RuntimeError("Refusing to delete a build run outside the configured Builds folder")
            movable = []
            for build in current:
                source = build.path
                real_source = os.path.realpath(source or "")
                stage_directory = os.path.dirname(source or "")
                stages_directory = os.path.join(project_root, "stages")
                if (not source or os.path.islink(source) or not os.path.isdir(source)
                        or os.path.islink(stages_directory) or os.path.islink(stage_directory)
                        or not _is_builds_descendant(real_source, root)):
                    raise RuntimeError(f"Refusing to delete unsafe build path for {build.stage_name}")
                destination = os.path.join(reservation_root, build.stage_id.hex, timestamp)
                movable.append((build, source, destination))
            if os.path.lexists(record_path) and os.path.islink(record_path):
                raise RuntimeError("Refusing to move a linked build-run record")

            os.mkdir(reservation_root)
            moved = []
            record_destination = os.path.join(reservation_root, "run.json")
            try:
                for build, source, destination in movable:
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    os.replace(source, destination)
                    build.path = destination
                    moved.append((build, source, destination))
                if os.path.isfile(record_path):
                    os.replace(record_path, record_destination)
            except Exception as reserve_error:
                rollback_errors = []
                for build, source, destination in reversed(moved):
                    try:
                        if os.path.lexists(destination):
                            if os.path.lexists(source):
                                raise RuntimeError(f"Original build path already exists: {source}")
                            os.makedirs(os.path.dirname(source), exist_ok=True)
                            os.replace(destination, source)
                        build.path = source
                    except Exception as error:
                        rollback_errors.append(f"{build.stage_name}: {error}")
                if os.path.lexists(record_destination):
                    try:
                        if os.path.lexists(record_path):
                            raise RuntimeError(f"Original run record already exists: {record_path}")
                        os.makedirs(os.path.dirname(record_path), exist_ok=True)
                        os.replace(record_destination, record_path)
                    except Exception as error:
                        rollback_errors.append(f"run record: {error}")
                if rollback_errors:
                    raise RuntimeError(
                        f"Could not reserve the build run ({reserve_error}); recover remaining files from "
                        f"{reservation_root}. Rollback errors: {'; '.join(rollback_errors)}") from reserve_error
                shutil.rmtree(reservation_root, ignore_errors=True)
                raise
            return moved, record_destination, record_path

        moved, record_destination, original_record_path = self._on_main_thread(reserve_run)
        deleted_stages = []
        failed_entry = None
        failure_message = None
        for index, (build, source, reserved) in enumerate(moved):
            try:
                build.delete()
                deleted_stages.append(build.stage_name)
            except Exception as error:
                failed_entry = index
                failure_message = str(error)
                break

        restore_errors = []
        if failed_entry is not None:
            for build, source, reserved in reversed(moved[failed_entry:]):
                try:
                    if os.path.lexists(reserved):
                        if os.path.lexists(source):
                            raise RuntimeError(f"Original build path already exists: {source}")
                        os.makedirs(os.path.dirname(source), exist_ok=True)
                        os.replace(reserved, source)
                    build.path = source
                except Exception as error:
                    restore_errors.append(f"{build.stage_name}: {error}")
            if os.path.lexists(record_destination):
                try:
                    if os.path.lexists(original_record_path):
                        raise RuntimeError(f"Original run record already exists: {original_record_path}")
                    os.makedirs(os.path.dirname(original_record_path), exist_ok=True)
                    os.replace(record_destination, original_record_path)
                except Exception as error:
                    restore_errors.append(f"run record: {error}")
            if not restore_errors:
                shutil.rmtree(reservation_root, ignore_errors=True)
            return self._json({
                "status": "partial" if deleted_stages else "failed",
                "project": project.name, "timestamp": timestamp,
                "deleted_stage_builds": deleted_stages,
                "run_record_preserved": os.path.isfile(original_record_path),
                "error": failure_message,
                "recovery_path": reservation_root if restore_errors else None,
                "recovery_errors": restore_errors,
                "issue_notes_preserved": True,
            })

        record_deleted = True
        record_restore_error = None
        try:
            if os.path.isfile(record_destination):
                os.remove(record_destination)
        except OSError as error:
            record_deleted = False
            try:
                os.replace(record_destination, original_record_path)
            except OSError as restore_error:
                record_restore_error = str(restore_error)
            failure_message = str(error)
        if record_deleted or not record_restore_error:
            shutil.rmtree(reservation_root, ignore_errors=True)
        if not record_deleted:
            return self._json({"status": "partial", "project": project.name,
                               "timestamp": timestamp, "deleted_stage_builds": deleted_stages,
                               "run_record_preserved": os.path.isfile(original_record_path),
                               "error": failure_message,
                               "recovery_path": reservation_root if record_restore_error else None,
                               "recovery_error": record_restore_error,
                               "issue_notes_preserved": True})
        return self._json({"status": "deleted", "project": project.name,
                           "timestamp": timestamp, "deleted_stage_builds": deleted_stages,
                           "run_record_deleted": True, "issue_notes_preserved": True})

    def _get_snapshot_usage(self, arguments):
        from .repositories import Repository
        from .project_build import load_project_builds

        key = str(arguments.get("snapshot", "")).strip()
        snapshot = next((item for item in Repository.Snapshot.value
                         if item.name == key or item.filename == key), None)
        if snapshot is None:
            raise ValueError(f"No snapshot named or identified {key!r}")

        project_usages = []
        build_usages = []
        read_errors = []
        for project in Repository.ProjectDirectory.value:
            try:
                configured = project.get_snapshot()
                if configured and configured.filename == snapshot.filename:
                    project_usages.append({
                        "project": project.name,
                        "selection": "latest" if project.uses_latest_snapshot else "pinned",
                    })
                for build in load_project_builds(project):
                    used_snapshot = (((build.details or {}).get("inputs") or {})
                                     .get("snapshot") or {}).get("file")
                    if used_snapshot != snapshot.filename:
                        continue
                    build_usages.append({
                        "project": project.name,
                        "stage": build.stage_name,
                        "timestamp": build.timestamp,
                        "date": build.date.isoformat(),
                        "status": build.status.value,
                    })
            except Exception as error:
                # One damaged project/build record should not hide usage from
                # the rest of the Library. Return the per-project read error.
                read_errors.append({"project": project.name, "error": str(error)[:1000]})

        return self._json({
            "snapshot": snapshot.filename,
            "projects": project_usages,
            "saved_builds": build_usages,
            "project_count": len(project_usages),
            "saved_build_count": len(build_usages),
            "read_errors": read_errors,
        })

    def _delete_snapshot(self, arguments):
        from .repositories import Repository
        from .snapshot_manager import SnapshotManager
        key = str(arguments.get("snapshot", "")).strip()
        snapshot = next((item for item in Repository.Snapshot.value
                         if item.name == key or item.filename == key), None)
        if snapshot is None:
            raise ValueError(f"No snapshot named or identified {key!r}")
        manager = SnapshotManager.shared()
        if snapshot not in manager.unused_snapshots() or self._snapshot_used_by_kernel_job(snapshot):
            raise ValueError("This snapshot is the latest, selected by a project, or used by an active operation")
        if self.user_input_callback is None:
            raise RuntimeError("Snapshot deletion requires an in-chat confirmation")
        answer = self.user_input_callback("Delete snapshot?",
            f"Permanently delete {snapshot.filename} and its extracted cache?", [
                {"id": "confirm", "label": "Delete this unused snapshot", "type": "boolean",
                 "placeholder": "Confirm deletion", "required": True}])
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "snapshot": snapshot.filename})
        def action():
            if snapshot not in manager.unused_snapshots() or self._snapshot_used_by_kernel_job(snapshot):
                raise RuntimeError("The snapshot became referenced while confirming; it was not deleted")
            manager.remove_snapshot(snapshot)
            return self._json({"status": "deleted", "snapshot": snapshot.filename})
        return self._on_main_thread(action)

    @staticmethod
    def _snapshot_used_by_kernel_job(snapshot):
        from .multistage_process import MultiStageProcess, MultiStageProcessState
        from .qemu_run import QemuRun
        for process in MultiStageProcess.started_processes:
            if process.status != MultiStageProcessState.IN_PROGRESS:
                continue
            if getattr(getattr(process, "support", None), "kernel_snapshot", None) is snapshot:
                return True
        return any(run.is_running and
                   getattr(getattr(run, "support", None), "kernel_snapshot", None) is snapshot
                   for run in QemuRun.running_runs)

    def _delete_packages_cache(self, arguments):
        from .repositories import Repository
        key = str(arguments.get("packages_directory", "")).strip()
        directory = next((item for item in Repository.PackagesDirectory.value
                          if item.name == key or str(item.id) == key), None)
        if directory is None:
            raise ValueError(f"No binary package cache named or identified {key!r}")
        from .project_build_process import running_project_build
        building = [project.name for project in directory.projects if running_project_build(project)]
        if building:
            raise RuntimeError("Stop active builds using this package cache first: " + ", ".join(building))
        projects = [project.name for project in directory.projects]
        if self.user_input_callback is None:
            raise RuntimeError("Deleting a package cache requires an in-chat confirmation")
        text = f"Permanently delete all binary packages in {directory.name}?"
        if projects:
            text += " Project references will be cleared for: " + ", ".join(projects) + "."
        answer = self.user_input_callback("Delete binary package cache?", text, [
            {"id": "confirm", "label": "Delete this cache and clear project references",
             "type": "boolean", "placeholder": "Confirm permanent deletion", "required": True}])
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "packages_directory": directory.name})
        def action():
            if any(running_project_build(project) for project in directory.projects):
                raise RuntimeError("An active build began using this cache; it was not deleted")
            from .packages_directory import delete_packages_directory
            delete_packages_directory(directory)
            return self._json({"status": "deleted", "packages_directory": directory.name,
                               "projects_unassigned": projects})
        return self._on_main_thread(action)

    def _create_packages_directory(self, arguments):
        from .architecture import Architecture
        from .packages_directory import (create_packages_directory, is_packages_name_available,
                                         project_cpu_flags, unique_packages_name)
        from .repositories import Repository

        project_key = str(arguments.get("project", "")).strip()
        project = self._project(project_key) if project_key else None
        requested_architecture = str(arguments.get("architecture", "")).strip()
        if project is not None:
            architecture = project.get_architecture()
            if architecture is None:
                raise ValueError("The selected project has no architecture configured")
            if requested_architecture:
                selected = next((item for item in Architecture
                                 if item.value == requested_architecture or item.name == requested_architecture), None)
                if selected is None or selected != architecture:
                    raise ValueError(f"Packages for {project.name} must use its {architecture.value} architecture")
            cpu_flags = project_cpu_flags(project)
        else:
            architecture = next((item for item in Architecture
                                 if item.value == requested_architecture or item.name == requested_architecture), None)
            if architecture is None:
                raise ValueError("Choose an architecture from list_options(architectures)")
            cpu_flags = None

        requested_name = str(arguments.get("name", "")).strip()
        name = requested_name or (unique_packages_name(project.name) if project else "")
        if not name:
            raise ValueError("A folder name is required")
        if name.startswith(".") or not is_packages_name_available(name):
            raise ValueError(f"A binary-packages folder named {name!r} already exists or is not allowed")
        if "cpu_flags" in arguments:
            supplied_flags = arguments.get("cpu_flags")
            if not isinstance(supplied_flags, str):
                raise ValueError("cpu_flags must be text")
            if project is not None and supplied_flags != cpu_flags:
                raise ValueError("When assigning a folder to a project, its CPU flags must match the project's effective flags")
            cpu_flags = supplied_flags.strip() or None

        def action():
            if not is_packages_name_available(name):
                raise RuntimeError(f"A binary-packages folder named {name!r} was created while preparing this request")
            if project is not None and project not in Repository.ProjectDirectory.value:
                raise RuntimeError("The project is no longer in the current Library")
            directory = create_packages_directory(name, architecture, cpu_flags)
            if project is not None:
                project.initialize_metadata().packages_directory_id = directory.id
                Repository.ProjectDirectory.save()
            return self._json({"status": "created", "id": str(directory.id), "name": directory.name,
                               "architecture": architecture.value, "cpu_flags": cpu_flags,
                               "assigned_project": project.name if project else None})
        return self._on_main_thread(action)

    def _delete_runner(self, arguments):
        from .repositories import Repository
        key = str(arguments.get("runner", "")).strip()
        runner = next((item for item in Repository.Runner.value
                       if item.name == key or str(item.id) == key), None)
        if runner is None:
            raise ValueError(f"No VM runner named or identified {key!r}")
        if runner.is_used:
            raise RuntimeError("This runner is being used by an operation; stop that operation first")
        if runner.is_running:
            raise RuntimeError("Stop this runner before deleting it")
        if self.user_input_callback is None:
            raise RuntimeError("Deleting a VM runner requires an in-chat confirmation")
        answer = self.user_input_callback("Delete VM runner?",
            f"Delete {runner.name} and its VM disk, including extracted toolsets and package caches stored in it? Shared folders are kept.", [
                {"id": "confirm", "label": "Delete this runner and its VM disk", "type": "boolean",
                 "placeholder": "Confirm permanent deletion", "required": True}])
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "runner": runner.name})
        def action():
            if runner.is_used or runner.is_running:
                raise RuntimeError("The runner became active while confirming; it was not deleted")
            from .build_machine_manager import VMRunnerManager
            VMRunnerManager.shared().remove_runner(runner)
            return self._json({"status": "deletion_started", "runner": runner.name,
                               "vm_disk_deleted_in_background": True})
        return self._on_main_thread(action)

    def _runner(self, key):
        from .repositories import Repository
        from .runner import HOST_RUNNER
        key = str(key).strip()
        if key.lower() in ("host", HOST_RUNNER.name.lower()):
            return HOST_RUNNER
        runner = next((item for item in Repository.Runner.value
                       if item.name == key or str(item.id) == key), None)
        if runner is None:
            raise ValueError(f"No VM runner named or identified {key!r}")
        return runner

    def _get_runner_status(self, arguments):
        from .runner import HOST_RUNNER, runner_is_supported
        runner = self._runner(arguments.get("runner", ""))
        if runner is HOST_RUNNER:
            result = {"id": "host", "name": "Host", "runner_type": "host",
                      "state": "active", "supported": runner_is_supported(runner),
                      "is_used": runner.is_used,
                      "users": runner.users}
            if arguments.get("include_usage"):
                result.update({"usage": None, "usage_unavailable": True})
            return self._json(result)
        runner.refresh_status()
        result = {"id": str(runner.id), "name": runner.name, "state": runner.status.lower(),
                  "is_used": runner.is_used, "users": runner.users, "cpus": runner.cpus,
                  "memory_gib": runner.memory_gib, "workspace_gib": runner.workspace_gib,
                  "swap_gib": runner.swap_gib, "instance_name": runner.instance_name,
                  "machine_directory": runner.machine_directory,
                  "workspace_mounted": bool(getattr(runner, "_workspace_users", 0))}
        if arguments.get("include_usage"):
            usage = runner.read_usage() if runner.is_running else None
            result["usage"] = usage
            result["usage_unavailable"] = usage is None
        return self._json(result)

    def _set_runner_state(self, arguments):
        from .runner import HOST_RUNNER
        runner = self._runner(arguments.get("runner", ""))
        if runner is HOST_RUNNER:
            raise ValueError("The Host runner is always active and has no start/stop operation")
        state = str(arguments.get("state", "")).strip().lower()
        if state not in ("start", "stop"):
            raise ValueError("Runner state must be 'start' or 'stop'")
        runner.refresh_status()
        if runner.status == runner.STATUS_MISSING:
            raise ValueError(f"VM runner {runner.name} is missing")
        if state == "stop" and runner.status == runner.STATUS_STOPPED:
            return self._json({"status": "already_stopped", "runner": runner.name, "state": "stopped"})
        if state == "stop" and runner.is_used:
            raise ValueError("This runner is in use; stop its operation before stopping the VM")
        output = []
        if state == "start":
            succeeded = runner.start(output_handler=output.append)
        else:
            succeeded = runner.stop(output_handler=output.append)
        runner.refresh_status()
        if not succeeded:
            raise RuntimeError(f"Could not {state} VM runner {runner.name}: " + "\n".join(output[-12:]))
        return self._json({"status": "completed", "runner": runner.name, "state": runner.status.lower(),
                           "output": output[-12:]})

    def _update_runner(self, arguments):
        from .runner import HOST_RUNNER
        runner = self._runner(arguments.get("runner", ""))
        if runner is HOST_RUNNER:
            raise ValueError("The Host runner cannot be renamed or resized")
        fields = {key: arguments[key] for key in ("name", "cpus", "memory_gib", "workspace_gib", "swap_gib")
                  if key in arguments}
        if not fields:
            raise ValueError("Specify at least one runner field to update")
        if "name" in fields:
            name = str(fields["name"]).strip()
            if not name:
                raise ValueError("Runner name cannot be empty")
            if name != runner.name:
                from .build_machine_manager import VMRunnerManager
                if not VMRunnerManager.shared().is_name_available(name, machine=runner):
                    raise ValueError(f"A runner named {name!r} already exists")
        resource_names = ("cpus", "memory_gib", "workspace_gib", "swap_gib")
        resources = {key: getattr(runner, key) for key in resource_names}
        bounds = {"cpus": (1, max(1, os.cpu_count() or 1)), "memory_gib": (2, 256),
                  "workspace_gib": (20, 2048), "swap_gib": (0, 512)}
        for key in resource_names:
            if key not in fields:
                continue
            value = fields[key]
            if type(value) is not int:
                raise ValueError(f"{key} must be an integer")
            minimum, maximum = bounds[key]
            if not minimum <= value <= maximum:
                raise ValueError(f"{key} must be between {minimum} and {maximum}")
            resources[key] = value
        if any(key in fields for key in resource_names):
            runner.refresh_status()
            if runner.status != runner.STATUS_STOPPED:
                raise ValueError("Stop the VM runner before changing its resource limits")
            runner.change_resources(resources["cpus"], resources["memory_gib"],
                                    resources["workspace_gib"], resources["swap_gib"])
        if "name" in fields and fields["name"].strip() != runner.name:
            runner.rename(fields["name"].strip())
        return self._json({"status": "updated", "runner": runner.name, "state": runner.status.lower(),
                           "cpus": runner.cpus, "memory_gib": runner.memory_gib,
                           "workspace_gib": runner.workspace_gib, "swap_gib": runner.swap_gib})

    def _toolset(self, key):
        from .repositories import Repository
        key = str(key).strip()
        toolset = next((item for item in Repository.Toolset.value
                        if item.name == key or str(item.uuid) == key), None)
        if toolset is None:
            raise ValueError(f"No toolset named or identified {key!r}")
        return toolset

    def _get_toolset_status(self, arguments):
        toolset = self._toolset(arguments.get("toolset", ""))
        from .toolset import ToolsetApplication
        from datetime import datetime
        from urllib.parse import urlparse
        apps = []
        for app in ToolsetApplication.ALL:
            installed = toolset.get_app_install(app)
            if installed:
                apps.append({"application": app.name, "version": installed.version,
                             "variant": installed.variant.name if installed.variant else None,
                             "patches": [getattr(item, "get_basename", lambda: str(item))()
                                         for item in installed.patches]})
        path = toolset.file_path()
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = None
        metadata = toolset.metadata or {}
        source = str(metadata.get("source") or "")
        bindings = [{"mount_path": item.mount_path, "host_path": item.host_path,
                     "toolset_path": item.toolset_path,
                     "access": "writable" if item.store_changes else "read_only"}
                    for item in (toolset.current_bindings or [])]
        def date_value(key):
            timestamp = metadata.get(key)
            try:
                return datetime.fromtimestamp(timestamp).isoformat() if isinstance(timestamp, (int, float)) else None
            except (OverflowError, OSError, ValueError):
                return None
        return self._json({"id": str(toolset.uuid), "name": toolset.name,
                           "runner": toolset.runs_on_name, "file": path,
                           "size_bytes": size_bytes,
                           "source": os.path.basename(urlparse(source).path) if source else None,
                           "date_created": date_value("date_created"),
                           "date_updated": date_value("date_updated"),
                           "spawned": bool(toolset.spawned), "in_use": bool(toolset.in_use),
                           "reserved": bool(toolset.is_reserved), "busy_reason": toolset.busy_reason,
                           "bindings": bindings,
                           "allow_binary_packages": bool(toolset.metadata.get("allow_binpkgs", False)),
                           "applications": apps})

    def _run_authorized_toolset_action(self, toolset, callback, action_name):
        from .rootless import authorize_toolset_action
        completed = threading.Event()
        outcome = {}
        def execute(authorization_keeper):
            try:
                if authorization_keeper is None:
                    raise RuntimeError("Toolset authorization was not granted")
                outcome["result"] = callback(authorization_keeper)
            except Exception as error:
                outcome["error"] = error
            finally:
                completed.set()
        authorize_toolset_action(callback=execute, name=action_name, machine=toolset.machine)
        if not completed.wait(1800):
            raise TimeoutError(f"Timed out waiting for {action_name}; check the app's operation status")
        if error := outcome.get("error"):
            raise RuntimeError(str(error)) from error
        return outcome.get("result")

    def _set_toolset_mount(self, arguments):
        toolset = self._toolset(arguments.get("toolset", ""))
        action = str(arguments.get("action", "")).strip().lower()
        if action not in ("mount_read_only", "mount_writable", "unmount_save", "unmount_discard"):
            raise ValueError("Unknown toolset mount action")
        if action in ("mount_read_only", "mount_writable"):
            if toolset.busy_reason:
                raise ValueError(toolset.busy_reason)
        else:
            if not toolset.spawned:
                raise ValueError("This toolset is not mounted")
            if toolset.in_use or toolset.is_reserved:
                raise ValueError("This toolset is currently in use")
            if action == "unmount_discard":
                if self.user_input_callback is None:
                    raise RuntimeError("Discarding a writable toolset mount requires confirmation")
                answer = self.user_input_callback("Discard toolset changes?",
                    f"Unmount {toolset.name} without saving its writable changes? Those changes will be lost.", [
                        {"id": "confirm", "label": "Discard the mounted changes", "type": "boolean",
                         "placeholder": "Confirm discard", "required": True}])
                if not answer or not answer.get("confirm"):
                    return self._json({"status": "cancelled", "toolset": toolset.name})

        def operation(_authorization_keeper):
            if not toolset.reserve():
                raise RuntimeError(f"Toolset {toolset.name} became busy before the action started")
            try:
                if action in ("mount_read_only", "mount_writable"):
                    toolset.spawn(store_changes=(action == "mount_writable"))
                    toolset.analyze()
                    return {"status": "mounted", "writable": action == "mount_writable"}
                toolset.unspawn(rebuild_squashfs_if_needed=(action == "unmount_save"))
                return {"status": "unmounted", "changes_saved": action == "unmount_save"}
            finally:
                toolset.release()

        result = self._run_authorized_toolset_action(toolset, operation, f"Manage toolset {toolset.name}")
        return self._json({"toolset": toolset.name, **result})

    def _update_toolset(self, arguments):
        toolset = self._toolset(arguments.get("toolset", ""))
        if toolset.busy_reason:
            raise ValueError(toolset.busy_reason)
        def operation(authorization_keeper):
            from .toolset_update import ToolsetUpdate
            update = ToolsetUpdate(toolset=toolset,
                                   allow_binpkgs=bool(toolset.metadata.get("allow_binpkgs", False)),
                                   update_packages=True)
            update.start(authorization_keeper=authorization_keeper)
            return {"status": "started", "operation": update.title,
                    "message": "Follow progress in Operations."}
        return self._json({"toolset": toolset.name,
                           **self._run_authorized_toolset_action(toolset, operation, f"Update toolset {toolset.name}")})

    def _configure_toolset(self, arguments):
        from pathlib import Path
        from gi.repository import Gio
        from .toolset import ToolsetApplication
        from .toolset_application import ToolsetApplicationSelection

        toolset = self._toolset(arguments.get("toolset", ""))
        if toolset.busy_reason:
            raise ValueError(toolset.busy_reason)
        selected_apps = arguments.get("applications")
        versions = arguments.get("application_versions") or {}
        patches = arguments.get("patches") or {}
        allow_binpkgs = arguments.get("allow_binpkgs")
        if selected_apps is not None and (
                not isinstance(selected_apps, list)
                or any(not isinstance(item, str) for item in selected_apps)):
            raise ValueError("applications must be a list of package IDs from toolset_applications")
        if not isinstance(versions, dict) or not isinstance(patches, dict):
            raise ValueError("application_versions and patches must be objects keyed by package ID")
        if allow_binpkgs is not None and type(allow_binpkgs) is not bool:
            raise ValueError("allow_binpkgs must be true or false")
        if selected_apps is None and (versions or patches):
            raise ValueError("Pass applications when changing application versions or patches")

        selectable = {app.package: app for app in ToolsetApplication.ALL if not app.auto_select}
        if selected_apps is not None:
            listed_apps = self._last_listed_options.get("toolset_applications", set())
            if not listed_apps:
                raise ValueError("Call list_options(toolset_applications) before choosing installed applications")
            if len(set(selected_apps)) != len(selected_apps):
                raise ValueError("applications cannot contain duplicate package IDs")
            if set(selected_apps) - {value for value, _label in listed_apps}:
                raise ValueError("applications must use exact package IDs from the latest toolset_applications options")
            unknown = set(selected_apps) - selectable.keys()
            if unknown:
                raise ValueError("Unknown application package ID(s): " + ", ".join(sorted(unknown)))
        if (set(versions) | set(patches)) - selectable.keys():
            raise ValueError("Versions and patches must use package IDs from toolset_applications")

        selections = None
        update = None
        if selected_apps is not None:
            selections = []
            for app in ToolsetApplication.ALL:
                previous = toolset.get_app_install(app)
                is_selected = (previous is not None if app.auto_select
                               else app.package in selected_apps)
                requested_version = versions.get(app.package)
                if requested_version is not None:
                    version = next((item for item in app.versions
                                    if str(item.id) == str(requested_version)), None)
                    if version is None:
                        raise ValueError(f"Version {requested_version!r} is not available for {app.package}; refresh list_options")
                else:
                    version = previous.variant if previous else (app.versions[0] if app.versions else None)
                if is_selected and version is None:
                    raise ValueError(f"No version is available for {app.package}")
                patch_values = patches.get(app.package)
                if patch_values is None:
                    patch_values = previous.patches if previous else []
                if not isinstance(patch_values, list) or any(not isinstance(path, str) for path in patch_values):
                    raise ValueError(f"Patches for {app.package} must be a list of local file paths")
                patch_files = []
                for path in patch_values:
                    patch_path = Path(path).expanduser()
                    if not patch_path.is_file():
                        raise ValueError(f"Patch file does not exist: {patch_path}")
                    patch_files.append(Gio.File.new_for_path(str(patch_path.resolve())))
                selections.append(ToolsetApplicationSelection(
                    app=app, version=version or (app.versions[0] if app.versions else None),
                    selected=is_selected, patches=patch_files))

            from .toolset_update import ToolsetUpdate
            target_binpkgs = (bool(toolset.metadata.get("allow_binpkgs", False))
                              if allow_binpkgs is None else allow_binpkgs)
            update = ToolsetUpdate(toolset=toolset, allow_binpkgs=target_binpkgs,
                                   update_packages=False, apps_selection=selections)
            if not update.apps_selection and not update.apps_to_remove:
                selections = []

        current_binpkgs = bool(toolset.metadata.get("allow_binpkgs", False))
        target_binpkgs = current_binpkgs if allow_binpkgs is None else allow_binpkgs
        app_changes = update is not None and bool(update.apps_selection or update.apps_to_remove)
        if not app_changes and target_binpkgs == current_binpkgs:
            return self._json({"status": "unchanged", "toolset": toolset.name})

        removal_names = [app.name for app in update.apps_to_remove] if app_changes else []
        if removal_names:
            if self.user_input_callback is None:
                raise RuntimeError("Removing installed toolset applications requires in-chat confirmation")
            answer = self.user_input_callback(
                "Remove installed applications?",
                f"Remove {', '.join(removal_names)} from {toolset.name}? This starts an asynchronous toolset update.",
                [{"id": "confirm", "label": "Remove these installed applications",
                  "type": "boolean", "placeholder": "Confirm application removal", "required": True}],
            )
            if not answer or not answer.get("confirm"):
                return self._json({"status": "cancelled", "toolset": toolset.name})

        def action(authorization_keeper=None):
            if toolset.busy_reason:
                raise RuntimeError(toolset.busy_reason)
            if target_binpkgs != current_binpkgs:
                toolset.metadata["allow_binpkgs"] = target_binpkgs
                from .repositories import Repository
                Repository.Toolset.save()
            if not app_changes:
                return {"status": "updated", "allow_binary_packages": target_binpkgs}
            update.start(authorization_keeper=authorization_keeper)
            return {"status": "started", "operation": update.title,
                    "allow_binary_packages": target_binpkgs,
                    "message": "Follow progress in Operations."}

        if app_changes:
            result = self._run_authorized_toolset_action(
                toolset, action, f"Configure toolset {toolset.name}")
        else:
            result = self._on_main_thread(action)
        return self._json({"toolset": toolset.name, **result})

    def _rename_toolset(self, arguments):
        toolset = self._toolset(arguments.get("toolset", ""))
        name = str(arguments.get("name", "")).strip()
        if not name:
            raise ValueError("Toolset name cannot be empty")
        if toolset.busy_reason:
            raise ValueError(toolset.busy_reason)
        if name == toolset.name:
            return self._json({"status": "unchanged", "name": name})
        from .toolset_manager import ToolsetManager
        manager = ToolsetManager.shared()
        if not manager.is_name_available(name):
            raise ValueError(f"A toolset named {name!r} already exists")
        old_name = toolset.name
        def action():
            if toolset.busy_reason:
                raise RuntimeError(toolset.busy_reason)
            manager.rename_toolset(toolset, name)
            return self._json({"status": "renamed", "old_name": old_name, "name": toolset.name})
        return self._on_main_thread(action)

    def _delete_toolset(self, arguments):
        toolset = self._toolset(arguments.get("toolset", ""))
        if toolset.busy_reason or toolset.in_use:
            raise ValueError(toolset.busy_reason or "This toolset is currently in use")
        from .repositories import Repository
        from . import project_toolsets
        projects = [project.name for project in Repository.ProjectDirectory.value
                    if project_toolsets.get(project.directory_path()) == str(toolset.uuid)]
        references = (" Projects that selected it will fall back to their default: " + ", ".join(projects) + ".") if projects else ""
        if self.user_input_callback is None:
            raise RuntimeError("Deleting a toolset requires an in-chat confirmation")
        answer = self.user_input_callback("Delete toolset?",
            f"Permanently delete {toolset.name} and its toolset file.{references}", [
                {"id": "confirm", "label": f"Permanently delete {toolset.name}", "type": "boolean",
                 "placeholder": "Confirm permanent deletion", "required": True}])
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "toolset": toolset.name})
        if toolset.busy_reason or toolset.in_use:
            raise RuntimeError("The toolset became busy while confirming; it was not deleted")
        def action():
            if toolset.busy_reason or toolset.in_use:
                raise RuntimeError("The toolset became busy while confirming; it was not deleted")
            from .toolset_manager import ToolsetManager
            ToolsetManager.shared().remove_toolset(toolset)
            Repository.Toolset.save()
            for project in Repository.ProjectDirectory.value:
                if project_toolsets.get(project.directory_path()) == str(toolset.uuid):
                    project_toolsets.set(project.directory_path(), None)
            return self._json({"status": "deleted", "toolset": toolset.name,
                               "affected_projects": projects})
        return self._on_main_thread(action)

    def _preview_build_plan(self, arguments):
        """Return the same parent rebuild/reuse plan shown by the build wizard."""
        project = self._project(arguments.get("project", ""))
        stage_values = arguments.get("stages")
        if not isinstance(stage_values, list) or not stage_values:
            raise ValueError("Select at least one stage to preview")
        stage_by_id = {str(stage.id): stage for stage in project.stages}
        selected_values = [str(value) for value in stage_values]
        if len(set(selected_values)) != len(selected_values):
            raise ValueError("Each stage may be selected only once")
        unknown = [value for value in selected_values if value not in stage_by_id]
        if unknown:
            raise ValueError("Some stages are no longer available; refresh list_options(project_stages)")
        selected = [stage_by_id[value] for value in selected_values]
        selected_ids = {stage.id for stage in selected}
        broken_selected = [stage.name for stage in selected if getattr(stage, "is_broken", False)]
        if broken_selected:
            raise ValueError("Cannot build broken stages: " + ", ".join(broken_selected))

        from .project_build import StageBuildPlan, StageBuildMode
        plan = StageBuildPlan(project, selected_ids)
        order = plan.build_order()
        readiness = {}
        requested_toolset = str(arguments.get("toolset", "")).strip()
        if requested_toolset:
            from .repositories import Repository
            from .toolset_selection import build_toolset_requirements, default_toolset, toolset_has_applications
            required_apps = build_toolset_requirements(project)
            toolset = (default_toolset(required_apps) if requested_toolset == "default" else
                       next((item for item in Repository.Toolset.value
                             if str(item.uuid) == requested_toolset), None))
            if toolset is None or not toolset_has_applications(toolset, required_apps):
                raise ValueError("Choose a build toolset from list_options(build_toolsets)")
            if toolset.busy_reason:
                raise ValueError(f"The selected toolset is unavailable: {toolset.busy_reason}")

            # The build wizard checks defaults against working copies. Keep the
            # chat preview equally read-only by cloning stage config containers.
            import copy
            from .project_build_spec import StageSpecContext, missing_required_stage_values
            from .project_stage import refresh_default_stage_arguments
            from .project_stage_arguments import StageArgumentDetails
            evaluation_stages = []
            for original in project.stages:
                stage_copy = copy.copy(original)
                stage_copy.__dict__ = dict(original.__dict__)
                for attribute in ("_default_stage_arguments", "_explicit_stage_arguments",
                                  "_explicit_none_arguments", "_explicit_user_arguments",
                                  "_explicit_user_argument_dates"):
                    value = getattr(stage_copy, attribute, None)
                    if isinstance(value, dict):
                        setattr(stage_copy, attribute, dict(value))
                    elif isinstance(value, set):
                        setattr(stage_copy, attribute, set(value))
                evaluation_stages.append(stage_copy)
            evaluation_project = copy.copy(project)
            evaluation_project.stages = evaluation_stages
            eval_by_id = {stage.id: stage for stage in evaluation_stages}
            defaults_snapshot = project.get_snapshot()
            context = StageSpecContext(
                timestamp="<timestamp>", source_subpath="<stage seed>",
                portage_confdir="<generated portage configuration>", toolset=toolset,
                cache_paths={StageArgumentDetails.pkgcache_path: "<package cache>",
                             StageArgumentDetails.kerncache_path: "<kernel cache>"},
                root_overlay="<generated root overlay>", iso_overlay="<generated ISO overlay>",
                snapshot=defaults_snapshot,
            )
            for stage in evaluation_stages:
                try:
                    refresh_default_stage_arguments(
                        evaluation_project, stage, toolset=toolset, snapshot=defaults_snapshot)
                    missing = missing_required_stage_values(evaluation_project, stage, context)
                    readiness[str(stage.id)] = {"ready": not missing, "missing_required": missing}
                except Exception as error:
                    readiness[str(stage.id)] = {
                        "ready": None, "missing_required": [], "check_error": str(error),
                    }
        entries = []
        for stage in project.stages:
            entry = plan.entries[stage.id]
            if entry.mode == StageBuildMode.NONE:
                continue
            row = {
                "stage_id": str(stage.id),
                "stage": stage.name,
                "target": stage.target,
                "mode": entry.mode.value,
                "selected": stage.id in selected_ids,
                "required_by": [item.name for item in entry.required_by],
                "broken": bool(getattr(stage, "is_broken", False)),
            }
            if entry.reused_build is not None:
                row["reused_build"] = {
                    "timestamp": entry.reused_build.timestamp,
                    "date": entry.reused_build.date.isoformat(),
                }
            if str(stage.id) in readiness:
                row["readiness"] = readiness[str(stage.id)]
            entries.append(row)
        return self._json({
            "project": project.name,
            "selected_stages": [stage.name for stage in selected],
            "build_order": [{"stage_id": str(stage.id), "stage": stage.name} for stage in order],
            "stages": entries,
            "build_count": len(order),
            "reuse_count": sum(entry.mode == StageBuildMode.REUSE for entry in plan.entries.values()),
            "readiness_toolset": requested_toolset or None,
            "note": "Planning only; does not validate every required Catalyst setting or start a build.",
        })

    def _start_build(self, arguments):
        project = self._project(arguments.get("project", ""))
        if project.configuration_error:
            raise ValueError(f"Project configuration is incomplete: {project.configuration_error}")
        from .project_build_process import running_project_build
        if running_project_build(project) is not None:
            raise ValueError("This project already has a build running")
        from .repositories import Repository
        from .runner import runner_by_id, runner_is_supported
        from .snapshot import LATEST_SNAPSHOT
        from .project_build import StageBuildPlan
        from .toolset_selection import build_toolset_requirements, default_toolset, toolset_has_applications
        from . import project_build_process

        stage_values = arguments.get("stages")
        if not isinstance(stage_values, list) or not stage_values:
            raise ValueError("Select at least one stage to build")
        stage_by_id = {str(stage.id): stage for stage in project.stages}
        unknown_stages = [str(value) for value in stage_values if str(value) not in stage_by_id]
        if unknown_stages:
            raise ValueError("Some selected stages are no longer available; refresh list_options(project_stages)")
        selected_ids = {stage_by_id[str(value)].id for value in stage_values}
        broken_selected = [stage_by_id[str(value)].name for value in stage_values
                           if getattr(stage_by_id[str(value)], "is_broken", False)]
        if broken_selected:
            raise ValueError("Cannot build broken stages: " + ", ".join(broken_selected))

        requirements = build_toolset_requirements(project)
        toolset_id = str(arguments.get("toolset", "")).strip()
        toolset = default_toolset(requirements) if toolset_id == "default" else next(
            (item for item in Repository.Toolset.value if str(item.uuid) == toolset_id), None)
        if toolset is None or not toolset_has_applications(toolset, requirements):
            raise ValueError("The selected toolset is missing or does not contain the applications required by this project")
        if toolset.busy_reason:
            raise ValueError(f"The selected toolset is unavailable: {toolset.busy_reason}")

        runner_id = str(arguments.get("runner", "")).strip()
        runner = runner_by_id(runner_id)
        if runner is None or not runner_is_supported(runner):
            raise ValueError("The selected runner is missing or unsupported on this computer")

        snapshot_choice = str(arguments.get("snapshot", "")).strip()
        if snapshot_choice == "latest":
            snapshot, fetch_snapshot, defaults_snapshot = None, True, project.get_snapshot()
        elif snapshot_choice == "project":
            if project.uses_latest_snapshot:
                snapshot, fetch_snapshot, defaults_snapshot = None, True, project.get_snapshot()
            else:
                snapshot = project.get_snapshot()
                if snapshot is None:
                    raise ValueError("This project has no configured snapshot; choose a snapshot or latest")
                snapshot, fetch_snapshot, defaults_snapshot = snapshot, False, snapshot
        else:
            snapshot = next((item for item in Repository.Snapshot.value if item.filename == snapshot_choice), None)
            if snapshot is None:
                raise ValueError("The selected snapshot is no longer available; refresh list_options(build_snapshots)")
            fetch_snapshot, defaults_snapshot = False, snapshot

        update_project_snapshot = bool(arguments.get("update_project_snapshot", False))

        cleanup = bool(arguments.get("cleanup_previous_builds", False))
        def action():
            from .build_queue import BuildQueue, running_build, LATEST
            plan = StageBuildPlan(project, selected_ids)
            if update_project_snapshot and fetch_snapshot:
                metadata = project.initialize_metadata()
                metadata.latest_snapshot = True
                Repository.ProjectDirectory.save()
            if running_build() is not None:
                if update_project_snapshot and not fetch_snapshot:
                    metadata = project.initialize_metadata()
                    metadata.snapshot_id = snapshot.filename
                    metadata.latest_snapshot = False
                    Repository.ProjectDirectory.save()
                BuildQueue.shared().add(project, list(selected_ids), LATEST if fetch_snapshot else snapshot.filename,
                                        cleanup_previous_builds=cleanup, toolset=toolset, runner=runner,
                                        defaults_snapshot=defaults_snapshot)
                return self._json({"status": "queued", "project": project.name,
                                   "stages": [stage_by_id[str(value)].name for value in stage_values]})
            project_build_process.start_project_build(
                project, plan, toolset=toolset, runner=runner,
                snapshot=snapshot, fetch_snapshot=fetch_snapshot,
                update_project_snapshot=update_project_snapshot and not fetch_snapshot,
                cleanup_previous_builds=cleanup, defaults_snapshot=defaults_snapshot)
            return self._json({"status": "start_requested", "project": project.name,
                               "stages": [stage_by_id[str(value)].name for value in stage_values]})
        return self._on_main_thread(action)

    def _stop_build(self, arguments):
        project = self._project(arguments.get("project", ""))
        def action():
            from .project_build_process import running_project_build
            build = running_project_build(project)
            if build is None:
                return f"No build is currently running for {project.name}."
            build.cancel()
            return f"Cancellation requested for the build of {project.name}."
        return self._on_main_thread(action)

    def _start_vm(self, arguments):
        from .project_build import load_project_builds, StageBuildStatus
        from .qemu_run import QemuRun, QemuBuildKind, qemu_support
        from .apple_virtualization import apple_support
        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("timestamp", "")).strip()
        if not timestamp:
            raise ValueError("Pass the exact build timestamp from list_deployable_builds")
        build = next((item for item in load_project_builds(project)
                      if item.timestamp == timestamp), None)
        if build is None or build.status != StageBuildStatus.COMPLETED:
            raise ValueError("That completed build is no longer available; refresh list_deployable_builds")

        requested_backend = str(arguments.get("backend", "auto")).strip().lower() or "auto"
        if requested_backend not in {"auto", "qemu", "apple"}:
            raise ValueError("backend must be auto, qemu, or apple")
        candidates = ([("apple", apple_support(project, build)), ("qemu", qemu_support(project, build))]
                      if requested_backend == "auto" else
                      [(requested_backend, apple_support(project, build) if requested_backend == "apple"
                        else qemu_support(project, build))])
        selected = next(((kind, support) for kind, support in candidates
                         if support.available), None)
        if selected is None:
            reasons = "; ".join(f"{kind}: {support.reason}" for kind, support in candidates)
            raise ValueError(f"No requested VM backend can run this build: {reasons}")
        backend, availability = selected
        support = availability.support if backend == "apple" else availability
        if support is None:
            raise RuntimeError("The selected VM backend did not provide build support details")
        if support.kind == QemuBuildKind.STAGE and not support.boot_kernel:
            raise ValueError("This stage has no boot kernel. Build a kernel into the stage or use the VM setup workflow to build one first.")

        from .repositories import Repository
        settings = Repository.Settings.value
        try:
            cpus = int(arguments.get("cpus", settings.qemu_cpus))
            memory_gib = int(arguments.get("memory_gib", settings.qemu_memory_gib))
        except (TypeError, ValueError) as error:
            raise ValueError("cpus and memory_gib must be whole numbers") from error
        if cpus < 1 or memory_gib < 1:
            raise ValueError("cpus and memory_gib must be at least 1")
        if backend == "apple":
            limits = availability.limits or {}
            if not limits.get("min_cpus", 1) <= cpus <= limits.get("max_cpus", 64):
                raise ValueError(f"Apple Virtualization supports {limits.get('min_cpus', 1)} to {limits.get('max_cpus', 64)} CPUs here")
            memory_mib = memory_gib * 1024
            if not limits.get("min_memory", 1) <= memory_mib <= limits.get("max_memory", 256 * 1024):
                raise ValueError("memory_gib is outside this Mac's Apple Virtualization limits")
        else:
            if cpus > 64 or memory_gib > 256:
                raise ValueError("QEMU supports at most 64 CPUs and 256 GiB memory")
            memory_mib = memory_gib * 1024
        network_enabled = arguments.get("network_enabled", True)
        if not isinstance(network_enabled, bool):
            raise ValueError("network_enabled must be true or false")
        from dataclasses import replace
        from .qemu_run import read_host_resolv_conf
        support = replace(support, network_enabled=network_enabled,
                          host_resolv_conf=(read_host_resolv_conf()
                                            if network_enabled and support.kind == QemuBuildKind.STAGE else None))

        def action():
            if any(run.is_running for run in QemuRun.running_runs):
                raise RuntimeError("Another test virtual machine is already running")
            from .app_events import AppEvents, app_event_bus
            from .qemu_run_view import QemuRunView
            architecture = project.get_architecture()
            title = f"{build.stage_name} — {backend.title()}"
            view = QemuRunView(project, build, memory_mib=memory_mib, cpus=cpus,
                               start_immediately=True, backend=backend, support=support)
            app_event_bus.emit(AppEvents.PRESENT_VIEW, view, title, 1024, 768)
            return self._json({"status": "start_requested", "project": project.name,
                               "stage": build.stage_name, "timestamp": timestamp,
                               "backend": backend, "architecture": architecture.value if architecture else None,
                               "cpus": cpus, "memory_gib": memory_gib,
                               "network_enabled": network_enabled,
                               "view_opened": True})
        return self._on_main_thread(action)

    def _create_snapshot(self, arguments):
        from .repositories import Repository
        from .toolset import ToolsetApplication
        from .toolset_selection import default_toolset, toolset_has_applications
        from .runner import default_runner, runner_by_id, runner_is_supported, RunnerType
        toolset_id = str(arguments.get("toolset", "")).strip()
        toolset = default_toolset([ToolsetApplication.CATALYST]) if toolset_id == "default" else next(
            (item for item in Repository.Toolset.value if str(item.uuid) == toolset_id), None)
        if toolset is None or not toolset_has_applications(toolset, [ToolsetApplication.CATALYST]):
            raise ValueError("Select an available toolset containing Catalyst")
        if toolset.busy_reason:
            raise ValueError(f"The selected toolset is unavailable: {toolset.busy_reason}")
        runner_id = str(arguments.get("runner", "")).strip()
        runner = default_runner() if runner_id == "default" else runner_by_id(runner_id)
        if runner is None or not runner_is_supported(runner):
            raise ValueError("Select a runner supported on this computer")
        def begin(authorization_keeper):
            if not authorization_keeper:
                return
            try:
                from .snapshot_installation import SnapshotInstallation
                installation = SnapshotInstallation(toolset=toolset, runner=runner)
                installation.start(authorization_keeper=authorization_keeper)
            except Exception as error:
                print(f"Failed to start snapshot generation: {error}")
                authorization_keeper.release()

        from .rootless import authorize_toolset_action
        machine = runner if runner.runner_type == RunnerType.VM else None
        authorize_toolset_action(begin, name="Generate Portage snapshot", machine=machine)
        return self._json({"status": "start_requested", "toolset": toolset.name, "runner": runner.name})

    def _import_snapshot(self, arguments):
        from pathlib import Path
        from .repository import Repository, resolve_path
        source_path = Path(str(arguments.get("source_path", "")).strip()).expanduser()
        if not source_path.is_absolute():
            raise ValueError("Pass the absolute path to the user's .sqfs file")
        source_path = source_path.resolve(strict=True)
        if source_path.suffix.lower() != ".sqfs" or not source_path.is_file():
            raise ValueError("The selected source must be a regular .sqfs file")
        source_size = source_path.stat().st_size
        if source_size <= 0:
            raise ValueError("The selected snapshot file is empty")

        filename = source_path.name
        snapshots_location = resolve_path(Repository.Settings.value.snapshots_location)
        destination = os.path.join(snapshots_location, filename)
        if os.path.lexists(destination):
            raise ValueError(f"A snapshot named {filename!r} already exists in the Library")
        if any(snapshot.filename == filename for snapshot in Repository.Snapshot.value):
            raise ValueError(f"A snapshot named {filename!r} is already registered")
        def begin(authorization_keeper):
            if not authorization_keeper:
                return
            try:
                from gi.repository import Gio
                from .snapshot_installation import SnapshotInstallation
                if not source_path.is_file() or source_path.stat().st_size != source_size:
                    raise RuntimeError("The source snapshot changed before the import started")
                if os.path.lexists(destination):
                    raise RuntimeError(f"A snapshot named {filename!r} now exists; nothing was overwritten")
                installation = SnapshotInstallation(file=Gio.File.new_for_path(str(source_path)))
                installation.title = f"Import snapshot {filename}"
                installation.start(authorization_keeper=authorization_keeper)
            except Exception as error:
                print(f"Failed to start snapshot import: {error}")
                authorization_keeper.release()

        from .rootless import authorize_toolset_action
        authorize_toolset_action(begin, name=f"Import snapshot {filename}")
        return self._json({"status": "authorization_requested", "filename": filename,
                           "size_bytes": source_size, "source_kept": True})

    def _create_project(self, arguments):
        name = str(arguments.get("name", "")).strip()
        if not name:
            raise ValueError("Project name can't be empty")
        from .project_manager import ProjectManager
        if not ProjectManager.shared().is_name_available(name):
            raise ValueError(f"A project named {name!r} already exists")

        from .architecture import Architecture
        from .repositories import Repository
        from .snapshot import LATEST_SNAPSHOT
        from .toolset import ToolsetApplication
        from .toolset_selection import default_toolset, sorted_toolsets, toolset_has_applications

        source_name = str(arguments.get("source", "")).strip().lower()
        repository_url = str(arguments.get("repository_url", "")).strip()
        template_key = str(arguments.get("template", "")).strip()
        template_options = arguments.get("template_options") or {}
        if not isinstance(template_options, dict):
            raise ValueError("template_options must be an object of template variable selections")
        architecture_value = str(arguments.get("architecture", "")).strip()
        architecture = next((item for item in Architecture
                             if item.value == architecture_value or item.name == architecture_value), None)
        releng_value = str(arguments.get("releng_directory", "")).strip()
        releng = next((item for item in Repository.RelengDirectory.value
                       if item.name == releng_value or str(item.id) == releng_value), None)
        snapshot_value = str(arguments.get("snapshot", "")).strip()
        snapshot = LATEST_SNAPSHOT if snapshot_value.lower() == "latest" else next(
            (item for item in Repository.Snapshot.value
             if item.name == snapshot_value or item.filename == snapshot_value), None)
        toolsets = [item for item in sorted_toolsets(Repository.Toolset.value, [ToolsetApplication.CATALYST])
                    if toolset_has_applications(item, [ToolsetApplication.CATALYST])
                    and getattr(item, "busy_reason", None) is None]
        toolset_value = str(arguments.get("toolset", "")).strip()
        toolset = default_toolset([ToolsetApplication.CATALYST], toolsets) if toolset_value.lower() in ("default", "app default") else next(
            (item for item in toolsets if item.name == toolset_value or str(item.uuid) == toolset_value), None)

        if source_name not in ("empty", "git", "template"):
            raise ValueError("source must be 'empty', 'git', or 'template'")
        if source_name == "git" and not repository_url:
            raise ValueError("Enter a Git repository URL")
        template = None
        template_selection = None
        if source_name == "template":
            from .project_template import ProjectTemplate, ProjectTemplateSelection, load_app_templates
            template = next((item for item in load_app_templates()
                             if isinstance(item, ProjectTemplate) and
                             (item.repository_path == template_key or item.name == template_key)), None)
            if template is None:
                raise ValueError("That project template is no longer available. Call list_options and ask_user again.")
            generated = template.generate(template.resolve(template_options, project_name=name))
            architecture = generated.architecture or architecture
            template_selection = ProjectTemplateSelection(template=template, selected=template_options)
        if architecture is None or releng is None or snapshot is None:
            raise ValueError("One or more project options are no longer available. Call list_options and ask_user again.")
        if toolset_value.lower() not in ("default", "app default") and toolset is None:
            raise ValueError("The selected Catalyst toolset is not available. Call list_options and ask_user again.")

        def action():
            from .git_installation import GitDirectorySetupConfiguration, GitDirectorySource
            from .project_installation import ProjectInstallation
            from .project_create_view import DefaultProjectDirContentBuilder
            from .multistage_process import MultiStageProcessEvent, MultiStageProcessState
            from .toolset_selection import default_toolset as choose_default

            if not ProjectManager.shared().is_name_available(name):
                raise ValueError(f"A project named {name!r} already exists")
            if source_name == "template":
                source = GitDirectorySource.TEMPLATE
                data = template_selection
            else:
                source = GitDirectorySource.GIT_REPOSITORY if source_name == "git" else GitDirectorySource.CREATE_NEW
                data = repository_url if source == GitDirectorySource.GIT_REPOSITORY else DefaultProjectDirContentBuilder()
            configuration = GitDirectorySetupConfiguration(source=source, name=name, data=data)
            installation = ProjectInstallation(configuration, releng, snapshot, architecture)
            chosen_toolset = toolset or choose_default([ToolsetApplication.CATALYST], toolsets)

            def completed(status):
                if status == MultiStageProcessState.COMPLETED and chosen_toolset and getattr(installation, "directory", None):
                    try:
                        from . import project_toolsets
                        project_toolsets.set(installation.directory.directory_path(), str(chosen_toolset.uuid))
                    except Exception as error:
                        print(f"Failed to set the project's preferred toolset: {error}")
            installation._ai_completed_callback = completed
            installation.event_bus.subscribe(MultiStageProcessEvent.STATE_CHANGED, completed)
            installation.start()
            return self._json({"result": "started", "project": name,
                               "source": ("Template: " + template.name if source == GitDirectorySource.TEMPLATE else
                                          "Git repository" if source == GitDirectorySource.GIT_REPOSITORY else "Empty project"),
                               "architecture": architecture.value,
                               "releng_directory": releng.name,
                               "snapshot": "latest" if snapshot is LATEST_SNAPSHOT else snapshot.name,
                               "toolset": chosen_toolset.name if chosen_toolset else "app default",
                               "status": "Project creation is running in the background. The app will show progress in Operations."})
        return self._on_main_thread(action)

    def _rename_project(self, arguments):
        project = self._project(arguments.get("project", ""))
        name = str(arguments.get("name", "")).strip()
        if not name:
            raise ValueError("Project name can't be empty")
        def action():
            from .project_manager import ProjectManager
            if name == project.name:
                return f"Project is already named {name}."
            ProjectManager.shared().rename_directory(project, name)
            return f"Renamed project to {project.name}."
        return self._on_main_thread(action)

    def _update_project(self, arguments):
        project = self._project(arguments.get("project", ""))
        field = str(arguments.get("field", ""))
        value = arguments.get("value")
        architecture = None
        package_directory_choice = None
        if field == "architecture":
            from .architecture import Architecture
            from .repositories import Repository
            architecture = next((item for item in Architecture
                                 if item.value == str(value) or item.name == str(value)), None)
            if architecture is None:
                raise ValueError("Choose an architecture from list_options(architectures)")
            current_packages = project.get_packages_directory()
            if (current_packages is not None and current_packages.architecture is not None
                    and current_packages.architecture != architecture):
                compatible = [item for item in Repository.PackagesDirectory.value
                              if item.architecture is None or item.architecture == architecture]
                if self.user_input_callback is None:
                    raise RuntimeError("Changing architecture with an incompatible package folder requires an in-chat choice")
                actions = [
                    {"value": "new", "label": "Create and assign a new compatible folder", "default": True},
                    {"value": "keep", "label": f"Keep {current_packages.name} (it will be incompatible)"},
                ]
                if compatible:
                    actions.insert(1, {"value": "choose", "label": "Choose an existing compatible folder"})
                fields = [{"id": "packages_action", "label": "Binary packages folder",
                           "type": "select", "required": True, "options": actions}]
                if compatible:
                    fields.append({
                        "id": "packages_directory", "label": "Compatible folder (if choosing existing)",
                        "type": "select", "required": False,
                        "options": [{"value": str(item.id), "label": item.name,
                                     "default": len(compatible) == 1}
                                    for item in compatible],
                    })
                answer = self.user_input_callback(
                    "Change project architecture?",
                    f"{project.name} uses binary-packages folder {current_packages.name}, which is for a different architecture. Choose what to do with that folder.",
                    fields,
                )
                if not answer:
                    return self._json({"status": "cancelled", "project": project.name,
                                       "architecture": project.get_architecture().value
                                       if project.get_architecture() else None})
                package_directory_choice = str(answer.get("packages_action", ""))
                if package_directory_choice not in {"new", "keep", "choose"} or (
                        package_directory_choice == "choose" and not compatible):
                    raise ValueError("Choose whether to keep, replace, or create a compatible package folder")
                if package_directory_choice == "choose":
                    selected_id = str(answer.get("packages_directory", ""))
                    if not selected_id and len(compatible) == 1:
                        selected_id = str(compatible[0].id)
                    selected = next((item for item in compatible if str(item.id) == selected_id), None)
                    if selected is None:
                        answer = self.user_input_callback(
                            "Choose a compatible package folder",
                            f"Select a folder compatible with {architecture.value}.",
                            [{"id": "packages_directory", "label": "Binary packages folder",
                              "type": "select", "required": True,
                              "options": [{"value": str(item.id), "label": item.name,
                                           "default": len(compatible) == 1}
                                          for item in compatible]}],
                        )
                        if not answer:
                            return self._json({"status": "cancelled", "project": project.name,
                                               "architecture": project.get_architecture().value
                                               if project.get_architecture() else None})
                        selected_id = str(answer.get("packages_directory", ""))
                        if not selected_id and len(compatible) == 1:
                            selected_id = str(compatible[0].id)
                        selected = next((item for item in compatible if str(item.id) == selected_id), None)
                    if selected is None:
                        raise ValueError("Choose a compatible package folder from the displayed options")
                    package_directory_choice = ("choose", selected)
        def action():
            from .architecture import Architecture
            from .repositories import Repository
            configuration = project.initialize_metadata()
            if field == "architecture":
                configuration.architecture = architecture
                if package_directory_choice == "new":
                    from ..packages.packages_directory import create_packages_directory_for_project
                    packages = create_packages_directory_for_project(project)
                    configuration.packages_directory_id = packages.id
                    result = f"{architecture.value} (created and assigned package folder {packages.name})"
                elif (isinstance(package_directory_choice, tuple)
                      and package_directory_choice[0] == "choose"):
                    packages = package_directory_choice[1]
                    if packages not in Repository.PackagesDirectory.value:
                        raise RuntimeError("The selected package folder is no longer available")
                    configuration.packages_directory_id = packages.id
                    result = f"{architecture.value} (assigned package folder {packages.name})"
                else:
                    result = architecture.value
            elif field == "releng_directory":
                releng = self._find_object("releng", str(value))
                configuration.releng_directory_id = releng.id
                result = releng.name
            elif field == "snapshot":
                if str(value).lower() == "latest":
                    configuration.latest_snapshot = True
                    configuration.snapshot_id = None
                    result = "latest"
                else:
                    snapshot = next((item for item in Repository.Snapshot.value
                                     if item.filename == str(value) or item.name == str(value)), None)
                    if snapshot is None:
                        raise ValueError(f"No snapshot named {value!r}")
                    configuration.latest_snapshot = False
                    configuration.snapshot_id = snapshot.filename
                    result = snapshot.name
            elif field == "latest_snapshot":
                if not isinstance(value, bool):
                    raise ValueError("latest_snapshot must be true or false")
                if value:
                    configuration.latest_snapshot = True
                    configuration.snapshot_id = None
                elif configuration.snapshot_id:
                    configuration.latest_snapshot = False
                else:
                    from .snapshot import sorted_snapshots
                    snapshots = sorted_snapshots(Repository.Snapshot.value)
                    if not snapshots:
                        raise ValueError("No snapshot is available to select")
                    configuration.latest_snapshot = False
                    configuration.snapshot_id = snapshots[0].filename
                result = str(value).lower()
            elif field == "packages_directory":
                packages = next((item for item in Repository.PackagesDirectory.value
                                 if item.name == str(value) or str(item.id) == str(value)), None)
                if packages is None:
                    raise ValueError(f"No package cache named {value!r}")
                configuration.packages_directory_id = packages.id
                result = packages.name
            elif field == "toolset":
                from .toolset import ToolsetApplication
                from .toolset_selection import sorted_toolsets, toolset_has_applications
                from . import project_toolsets
                if str(value).lower() in ("default", "app default", ""):
                    project_toolsets.set(project.directory_path(), None)
                    result = "app default"
                else:
                    toolset = next((item for item in sorted_toolsets(
                        Repository.Toolset.value, [ToolsetApplication.CATALYST])
                        if str(item.uuid) == str(value) or item.name == str(value)), None)
                    if toolset is None or not toolset_has_applications(toolset, [ToolsetApplication.CATALYST]):
                        raise ValueError("No available Catalyst toolset matches that value")
                    project_toolsets.set(project.directory_path(), str(toolset.uuid))
                    result = toolset.name
            else:
                raise ValueError("Unsupported project setting")
            Repository.ProjectDirectory.save()
            from .git_directory import GitDirectoryEvent
            project.event_bus.emit(GitDirectoryEvent.CONFIGURATION_CHANGED, project)
            return f"Updated {project.name}.{field} to {result}."
        return self._on_main_thread(action)

    def _check_project_source_updates(self, arguments):
        project = self._project(arguments.get("project", ""))
        from .project_template_update import (
            TemplateState, has_unsaved_changes, repository_origin, template_update_available,
        )

        project_path = project.directory_path()
        template_state = TemplateState.load(project_path)
        busy = bool(project.is_busy)
        local_edits = bool(has_unsaved_changes(project_path))
        if template_state is not None:
            source_kind = "template"
            source_name = template_state.template_name
            update_available = template_update_available(project)
            checked = "live template comparison"
            note = ("Template availability is checked against the latest template definition and, when needed, "
                    "regenerated output. A null result means the check could not determine availability.")
        elif origin := repository_origin(project_path):
            source_kind = "git"
            source_name = origin.branch or origin.url.rsplit("/", 1)[-1]
            update_available = bool(project.has_remote_changes)
            checked = "cached app Git status"
            note = "Git availability is the app's most recently refreshed remote status; this check does not fetch."
        else:
            return self._json({
                "project": project.name, "source": None, "update_available": None,
                "local_edits": local_edits, "busy": busy, "can_start_update": False,
                "note": "This project has no tracked template or Git source.",
            })

        return self._json({
            "project": project.name, "source": source_kind, "source_name": source_name,
            "update_available": update_available, "availability_checked_by": checked,
            "local_edits": local_edits, "busy": busy,
            "can_start_update": not local_edits and not busy,
            "note": note + (" Save or discard local edits before updating." if local_edits else ""),
        })

    def _update_project_template(self, arguments):
        previous_options = dict(self._last_template_variable_options)
        try:
            return self._update_project_template_workflow(arguments)
        finally:
            self._last_template_variable_options.clear()
            self._last_template_variable_options.update(previous_options)

    def _update_project_overlays(self, project, requested):
        """Match the project update UI's optional update of linked overlays."""
        from .project_template_update import project_overlays
        overlays = project_overlays(project)
        result = {"selected": False, "available": [item.name for item in overlays],
                  "updated": [], "errors": []}
        if not overlays:
            return result, None
        if requested is None:
            if self.user_input_callback is None:
                raise RuntimeError("Choose whether to update the project's linked overlays")
            answer = self.user_input_callback(
                "Also update project overlays?",
                "The project uses these Git overlays. The app updates them before preparing the project update:\n"
                + ", ".join(item.name for item in overlays),
                [{"id": "update_overlays", "label": "Also update all listed overlays",
                  "type": "boolean", "default": True, "required": True}],
            )
            if not answer:
                return result, self._json({"status": "cancelled", "project": project.name,
                                           "message": "Project update cancelled before changing anything."})
            if answer.get("__user_feedback__"):
                return result, self._json({"status": "revision_requested", "project": project.name,
                                           "message": str(answer["__user_feedback__"])[:2000]})
            requested = answer.get("update_overlays", False)
        if type(requested) is not bool:
            raise ValueError("update_overlays must be true or false")
        result["selected"] = requested
        if not requested:
            return result, None

        from ..git_directory.git_update import update_git_directory
        for overlay in overlays:
            try:
                update_git_directory(overlay, lambda _line: None)
                result["updated"].append(overlay.name)
            except Exception as error:
                result["errors"].append({"overlay": overlay.name, "error": str(error)[:1000]})
        return result, None

    def _include_project_overlay_results(self, output, overlay_results):
        try:
            result = json.loads(output)
        except (TypeError, ValueError):
            result = {"status": "completed", "message": str(output)}
        if not isinstance(result, dict):
            result = {"status": "completed", "result": result}
        if overlay_results.get("errors") and result.get("status") in {"updated", "up_to_date"}:
            result["project_status"] = result["status"]
            result["status"] = "partial"
        result["overlay_updates"] = overlay_results
        return self._json(result)

    def _update_project_template_workflow(self, arguments):
        project = self._project(arguments.get("project", ""))
        drop_project_changes = arguments.get("drop_project_changes", False)
        if not isinstance(drop_project_changes, bool):
            raise ValueError("drop_project_changes must be true or false")
        from .project_template_update import (
            TemplateState, has_unsaved_changes, own_commits_count, prepare_template_update,
        )
        from .project_template import (
            ProjectTemplate, TemplateVariableType, fetch_template_repository,
            load_app_template_definition,
        )

        state = TemplateState.load(project.directory_path())
        if state is None:
            raise ValueError(f"{project.name} was not created from a project template")
        if project.is_busy:
            raise ValueError(f"{project.name} already has an operation running")
        if has_unsaved_changes(project.directory_path()):
            raise ValueError(f"{project.name} has unsaved changes. Save or discard them before updating its template.")

        overlay_results, interrupted = self._update_project_overlays(
            project, arguments.get("update_overlays"))
        if interrupted:
            return self._include_project_overlay_results(interrupted, overlay_results)

        template = None
        if state.source_kind == "app":
            template = load_app_template_definition(state.repository_path)
        else:
            templates = fetch_template_repository(state.repository_url)
            template = next((item for item in templates
                             if isinstance(item, ProjectTemplate)
                             and item.repository_path == state.repository_path), None)
            if template is None:
                error = next((item[1] for item in templates
                              if not isinstance(item, ProjectTemplate)
                              and item[0] == state.repository_path), None)
                raise ValueError(str(error) if error else
                                 f"Template {state.template_name} is no longer available")

        # Keep the project's selected values where still valid; template.resolve
        # fills new or removed options using the latest template defaults.
        resolved = template.resolve(state.selected, project_name=project.name)
        selected = {key: value for key, value in state.selected.items()
                    if key.startswith("group:")}
        selected.update({variable.id: resolved[variable.id] for variable in template.variables})
        asked_variables = set()

        def ask(title, message, fields):
            result = json.loads(self._ask_user({"title": title, "message": message, "fields": fields}))
            if result.get("status") != "submitted":
                return result
            if result.get("user_feedback"):
                result["status"] = "revision_requested"
            return result

        def form_result_or_return(result):
            status = result.get("status")
            if status == "submitted":
                return None
            if status == "cancelled":
                return self._json({"status": "cancelled", "message": "Template update cancelled; project unchanged."})
            return self._json({"status": "revision_requested", "message": result.get("user_feedback", "")})

        # Ask variable values a form at a time so each submitted selection can
        # reveal or hide variables that depend on it. Field IDs remain the IDs
        # defined by the template.
        while True:
            names = template.resolve(selected, project_name=project.name)
            pending = [variable for variable in template.visible_variables(names)
                       if variable.id not in asked_variables]
            if not pending:
                break
            fields = []
            for variable in pending:
                value = names[variable.id]
                if variable.type == TemplateVariableType.BOOLEAN:
                    field_type = "boolean"
                elif variable.type == TemplateVariableType.MULTIPLE:
                    field_type = "multiselect"
                else:
                    field_type = "select"
                field = {"id": variable.id, "label": variable.title,
                         "type": field_type, "required": (
                             variable.type != TemplateVariableType.MULTIPLE
                             or variable.minimum_selected > 0),
                         "default": value}
                if variable.description:
                    field["description"] = variable.description
                if field_type in ("select", "multiselect"):
                    options = variable.available_options(names)
                    field["options"] = [
                        {"value": option.value, "label": option.title,
                         "default": (option.value in value if isinstance(value, list)
                                     else option.value == value)}
                        for option in options
                    ]
                    self._last_template_variable_options[variable.id] = {
                        (str(option["value"]), str(option["label"]))
                        for option in field["options"]
                    }
                fields.append(field)
            for offset in range(0, len(fields), 50):
                chunk = fields[offset:offset + 50]
                result = ask("Template Update Settings",
                             "Choose the latest template settings. Existing valid values are preselected.", chunk)
                if outcome := form_result_or_return(result):
                    return self._include_project_overlay_results(outcome, overlay_results)
                selected.update(result.get("answers") or {})
                asked_variables.update(field["id"] for field in chunk)

        names = template.resolve(selected, project_name=project.name)
        generated = template.generate(names)
        available_groups = template.available_groups(names)
        if available_groups:
            group_fields = []
            group_stage_answers = {}
            generated_stages = generated.stages
            for group in available_groups:
                previous = selected.get(f"group:{group.id}") or {}
                enabled_id = f"group_{group.id}_enabled"
                stages_id = f"group_{group.id}_stages"
                group_fields.append({"id": enabled_id, "label": group.title,
                                     "type": "boolean", "required": False,
                                     "default": bool(previous.get("enabled", group.default))})
                stage_by_id = {}
                for stage in generated_stages:
                    logical_id = (stage.persistent_id or stage.template_id).partition(":")[0]
                    if logical_id in group.stages:
                        stage_by_id.setdefault(logical_id, stage.name)
                if stage_by_id:
                    options = [{"value": stage_id, "label": stage_name,
                                "default": stage_id in previous.get("stages", group.default_stages)}
                               for stage_id, stage_name in stage_by_id.items()]
                    group_fields.append({"id": stages_id, "label": f"{group.title} stages",
                                         "type": "multiselect", "required": False,
                                         "default": list(previous.get("stages", group.default_stages)),
                                         "options": options})
                    self._last_template_variable_options[stages_id] = {
                        (str(item["value"]), str(item["label"])) for item in options
                    }
                    group_stage_answers[group.id] = stages_id
            for offset in range(0, len(group_fields), 50):
                chunk = group_fields[offset:offset + 50]
                result = ask("Template Update Groups",
                             "Choose which optional template configurations to include and the stages they apply to.",
                             chunk)
                if outcome := form_result_or_return(result):
                    return self._include_project_overlay_results(outcome, overlay_results)
                answers = result.get("answers") or {}
                for group in available_groups:
                    enabled_id = f"group_{group.id}_enabled"
                    stages_id = group_stage_answers.get(group.id)
                    if enabled_id not in answers and not (stages_id and stages_id in answers):
                        continue
                    previous = selected.get(f"group:{group.id}") or {}
                    selected[f"group:{group.id}"] = {
                        "enabled": answers.get(enabled_id, previous.get("enabled", group.default)),
                        "stages": answers.get(stages_id, previous.get("stages", group.default_stages))
                        if stages_id else list(previous.get("stages", group.default_stages)),
                    }

        update = prepare_template_update(
            project, log=lambda _message: None, selected=selected,
            force=drop_project_changes)
        if update is None:
            return self._include_project_overlay_results(
                self._json({"status": "up_to_date", "project": project.name,
                            "message": "The latest template produces no project changes."}), overlay_results)
        try:
            update.drop_project_changes = drop_project_changes
            commits_to_drop = own_commits_count(project.directory_path()) if drop_project_changes else 0
            conflicts = ([] if drop_project_changes else
                         [change for change in update.changes if change.conflict])
            for offset in range(0, len(conflicts), 50):
                chunk = conflicts[offset:offset + 50]
                fields = []
                for index, change in enumerate(chunk, start=offset + 1):
                    field_id = f"template_conflict_{index}"
                    use_template = change.use_template
                    options = [
                        {"value": "project", "label": "Keep project", "default": not use_template},
                        {"value": "template", "label": "Use template", "default": use_template},
                    ]
                    self._last_template_variable_options[field_id] = {
                        (item["value"], item["label"]) for item in options
                    }
                    fields.append({"id": field_id, "label": change.title, "type": "select",
                                   "required": True, "default": "template" if use_template else "project",
                                   "options": options})
                result = ask("Resolve Template Changes",
                             "These values changed both in the project and in the template. Choose which version to keep.",
                             fields)
                if outcome := form_result_or_return(result):
                    return self._include_project_overlay_results(outcome, overlay_results)
                answers = result.get("answers") or {}
                for index, change in enumerate(chunk, start=offset + 1):
                    change.use_template = answers.get(f"template_conflict_{index}") == "template"

            changes = update.clean_changes if drop_project_changes else update.changes
            change_names = [change.title for change in changes[:20]]
            summary = ", ".join(change_names) if change_names else "template version metadata"
            if len(changes) > len(change_names):
                summary += f", and {len(changes) - len(change_names)} more"
            if drop_project_changes:
                impact = (f" This discards {commits_to_drop} saved project commit(s); they remain recoverable through Git's reflog until Git prunes them."
                          if commits_to_drop else "")
                result = ask("Confirm Discarding Project Commits",
                             f"Apply {len(changes)} template change(s) to {project.name}?{impact} Changes: {summary}.",
                             [{"id": "confirm", "label": "Apply and discard saved commits",
                               "type": "boolean", "required": True, "default": False}])
                if outcome := form_result_or_return(result):
                    return self._include_project_overlay_results(outcome, overlay_results)
                if not (result.get("answers") or {}).get("confirm"):
                    return self._include_project_overlay_results(
                        self._json({"status": "cancelled", "message": "Template update cancelled; project unchanged."}),
                        overlay_results)

            self._on_main_thread(lambda: update.apply(project, log=print), timeout=3600)
            return self._include_project_overlay_results(
                self._json({"status": "updated", "project": project.name,
                            "changes": len(changes),
                            "conflicts_resolved": len(conflicts),
                            "saved_commits_dropped": commits_to_drop,
                            "message": ("The template update was applied; saved project commits were discarded."
                                        if drop_project_changes else
                                        "The template update was applied; saved project commits were preserved.")}),
                overlay_results)
        finally:
            update.cleanup()

    def _update_project_repository(self, arguments):
        project = self._project(arguments.get("project", ""))
        drop_project_changes = arguments.get("drop_project_changes", False)
        if not isinstance(drop_project_changes, bool):
            raise ValueError("drop_project_changes must be true or false")

        from .project_template_update import (
            TemplateState, has_unsaved_changes, own_commits_count,
            prepare_repository_update, repository_origin,
        )
        project_path = project.directory_path()
        if TemplateState.load(project_path) is not None:
            raise ValueError(f"{project.name} was created from a template; use update_project_template")
        if project.is_busy:
            raise ValueError(f"{project.name} already has an operation running")
        if repository_origin(project_path) is None:
            raise ValueError(f"{project.name} is not connected to a Git repository")
        if has_unsaved_changes(project_path):
            raise ValueError(f"{project.name} has unsaved changes. Save or discard them before updating.")

        overlay_results, interrupted = self._update_project_overlays(
            project, arguments.get("update_overlays"))
        if interrupted:
            return self._include_project_overlay_results(interrupted, overlay_results)

        previous_options = dict(self._last_template_variable_options)

        def ask(title, message, fields):
            result = json.loads(self._ask_user({"title": title, "message": message, "fields": fields}))
            if result.get("status") == "submitted" and result.get("user_feedback"):
                result["status"] = "revision_requested"
            return result

        def form_result_or_return(result):
            status = result.get("status")
            if status == "submitted":
                return None
            if status == "cancelled":
                return self._json({"status": "cancelled", "message": "Repository update cancelled; project unchanged."})
            return self._json({"status": "revision_requested", "message": result.get("user_feedback", "")})

        try:
            update = prepare_repository_update(project, log=lambda _message: None,
                                               force=drop_project_changes)
            if update is None:
                return self._include_project_overlay_results(
                    self._json({"status": "up_to_date", "project": project.name,
                                "message": "The project is already up to date with its Git upstream."}),
                    overlay_results)
            try:
                commits_to_drop = own_commits_count(project_path) if drop_project_changes else 0
                update.drop_project_changes = drop_project_changes
                conflicts = [] if drop_project_changes else [change for change in update.changes if change.conflict]
                for offset in range(0, len(conflicts), 50):
                    chunk = conflicts[offset:offset + 50]
                    fields = []
                    for index, change in enumerate(chunk, start=offset + 1):
                        field_id = f"repository_conflict_{index}"
                        options = [
                            {"value": "project", "label": "Keep project", "default": not change.use_template},
                            {"value": "upstream", "label": "Use upstream", "default": change.use_template},
                        ]
                        self._last_template_variable_options[field_id] = {
                            (item["value"], item["label"]) for item in options
                        }
                        fields.append({"id": field_id, "label": change.title,
                                       "type": "select", "required": True,
                                       "default": "upstream" if change.use_template else "project",
                                       "options": options})
                    result = ask("Resolve Git Update Changes",
                                 "These values changed both in the project and upstream. Choose which version to keep.",
                                 fields)
                    if outcome := form_result_or_return(result):
                        return self._include_project_overlay_results(outcome, overlay_results)
                    answers = result.get("answers") or {}
                    for index, change in enumerate(chunk, start=offset + 1):
                        change.use_template = answers.get(f"repository_conflict_{index}") == "upstream"

                changes = update.clean_changes if drop_project_changes else update.changes
                change_names = [change.title for change in changes[:20]]
                summary = ", ".join(change_names) if change_names else "repository metadata"
                if len(changes) > len(change_names):
                    summary += f", and {len(changes) - len(change_names)} more"
                if drop_project_changes:
                    impact = (f" This will discard {commits_to_drop} saved project commit(s); they remain recoverable "
                              "through Git's reflog until Git prunes them." if commits_to_drop else "")
                    result = ask("Confirm Discarding Project Commits",
                                 f"Apply {len(changes)} upstream change(s) to {project.name}?{impact} Changes: {summary}.",
                                 [{"id": "confirm", "label": "Apply and discard saved commits",
                                   "type": "boolean", "required": True, "default": False}])
                    if outcome := form_result_or_return(result):
                        return self._include_project_overlay_results(outcome, overlay_results)
                    if not (result.get("answers") or {}).get("confirm"):
                        return self._include_project_overlay_results(
                            self._json({"status": "cancelled", "message": "Repository update cancelled; project unchanged."}),
                            overlay_results)

                self._on_main_thread(lambda: update.apply(project, log=print), timeout=3600)
                return self._include_project_overlay_results(
                    self._json({"status": "updated", "project": project.name,
                                "changes": len(changes), "conflicts_resolved": len(conflicts),
                                "saved_commits_dropped": commits_to_drop,
                                "message": ("The Git project update was applied; saved project commits were discarded." if drop_project_changes
                                            else "The Git project update was applied; saved project commits were preserved.")}),
                    overlay_results)
            finally:
                update.cleanup()
        finally:
            self._last_template_variable_options.clear()
            self._last_template_variable_options.update(previous_options)

    def _create_stage(self, arguments):
        project = self._project(arguments.get("project", ""))
        name = str(arguments.get("name", "")).strip()
        target = str(arguments.get("target", "")).strip()
        if not name or not target:
            raise ValueError("Stage name and Catalyst target are required")
        def action():
            from .repositories import Repository
            from .toolset import ToolsetEnv
            from .toolset_application import ToolsetApplication
            from .toolset_selection import default_toolset, toolset_has_applications
            from . import project_toolsets
            from .project_stage import load_catalyst_targets, load_releng_templates
            from .project_stage_installation import ProjectStageInstallation
            available_toolsets = [item for item in Repository.Toolset.value
                                  if item.env == ToolsetEnv.EXTERNAL
                                  and toolset_has_applications(item, [ToolsetApplication.CATALYST])
                                  and getattr(item, "busy_reason", None) is None]
            toolset_key = str(arguments.get("toolset", "")).strip()
            if toolset_key:
                toolset = next((item for item in available_toolsets
                                if str(item.uuid) == toolset_key or item.name == toolset_key), None)
                if toolset is None:
                    raise ValueError("Selected toolset is unavailable; refresh list_options(project_stage_toolsets)")
            else:
                preferred_id = project_toolsets.get(project.directory_path())
                toolset = next((item for item in available_toolsets if str(item.uuid) == preferred_id), None)
                toolset = toolset or default_toolset([ToolsetApplication.CATALYST], available_toolsets)
            if toolset is None:
                raise ValueError("No available external toolset has Catalyst installed")
            valid_targets = load_catalyst_targets(toolset)
            if target not in valid_targets:
                raise ValueError(f"Target {target!r} is unavailable in toolset {toolset.name!r}; refresh stage_targets")

            parent_key = str(arguments.get("parent", "")).strip()
            parent = next((item for item in project.stages
                           if item.name == parent_key or str(item.id) == parent_key), None) if parent_key else None
            if parent_key and parent is None:
                raise ValueError("Selected seed stage is unavailable; refresh list_options(project_stages)")
            if target != "stage1" and parent is None:
                raise ValueError("A seed parent stage is required for every target except stage1")
            releng_template = str(arguments.get("releng_template", "")).strip() or None
            if releng_template:
                releng_directory = project.get_releng_directory()
                architecture = project.get_architecture()
                valid_specs = (load_releng_templates(releng_directory, target, architecture)
                               if releng_directory is not None and architecture is not None else [])
                if releng_template not in valid_specs:
                    raise ValueError("Selected Releng spec is unavailable; refresh stage_releng_templates")

            from .project_manager import ProjectManager
            manager = ProjectManager.shared()
            if not manager.is_stage_name_available(project, name):
                raise ValueError(f"A stage named {name!r} already exists")
            operation = ProjectStageInstallation(
                project_directory=project, target_name=target,
                releng_template_name=releng_template, stage_name=name,
                parent_id=parent.id if parent else None, toolset=toolset)
            operation.start()
            return self._json({"result": "started", "status": "in_progress", "project": project.name,
                               "stage": name,
                               "target": target, "toolset": toolset.name,
                               "parent": parent.name if parent else None,
                               "releng_template": releng_template,
                               "message": "Stage installation started. Check list_operations for completion status."})
        return self._on_main_thread(action)

    def _set_stage_parent(self, arguments):
        project = self._project(arguments.get("project", ""))
        stage_key = str(arguments.get("stage", ""))
        action_name = str(arguments.get("action", "")).strip().lower()
        if action_name not in ("set", "clear"):
            raise ValueError("Choose set or clear for the parent action")

        def action():
            from .project_manager import ProjectManager
            stage = next((item for item in project.stages
                          if item.name == stage_key or str(item.id) == stage_key), None)
            if stage is None:
                raise ValueError(f"No stage named or identified {stage_key!r}")

            parent = None
            if action_name == "set":
                parent_key = str(arguments.get("parent", "")).strip()
                if not parent_key:
                    raise ValueError("Choose a parent stage name or ID")
                parent = next((item for item in project.stages
                               if item.name == parent_key or str(item.id) == parent_key), None)
                if parent is None:
                    raise ValueError(f"No parent stage named or identified {parent_key!r}")
                if parent is stage:
                    raise ValueError("A stage cannot be its own parent")

                # Refuse a relationship that would make this stage an ancestor
                # of itself. Also stop safely if the existing graph is corrupt.
                current = parent
                visited = set()
                by_id = {item.id: item for item in project.stages}
                while current is not None:
                    if current.id == stage.id:
                        raise ValueError("That parent would create a stage relationship cycle")
                    if current.id in visited:
                        raise ValueError("The existing stage relationships contain a cycle")
                    visited.add(current.id)
                    current = by_id.get(getattr(current, "parent", None))

            from .project_stage_arguments import StageArgumentDetails
            ProjectManager.shared().change_stage_argument(
                project, stage, StageArgumentDetails.parent, parent.id if parent else None)
            return self._json({"result": "updated", "project": project.name,
                               "stage": stage.name,
                               "parent": ({"name": parent.name, "id": str(parent.id)}
                                          if parent else None),
                               "note": "Effective inherited settings may have changed."})

        return self._on_main_thread(action)

    def _update_stage(self, arguments):
        project = self._project(arguments.get("project", ""))
        stage_key = str(arguments.get("stage", ""))
        field = str(arguments.get("field", ""))
        value = str(arguments.get("value", ""))
        if not value:
            raise ValueError("Stage setting can't be empty")
        def action():
            from .project_manager import ProjectManager
            from .project_stage_arguments import StageArgumentDetails
            stage = next((item for item in project.stages
                          if item.name == stage_key or str(item.id) == stage_key), None)
            if stage is None:
                raise ValueError(f"No stage named or identified {stage_key!r}")
            if field == "name":
                ProjectManager.shared().rename_stage(project, stage, value)
            else:
                allowed = {"target", "version_stamp", "subarch", "rel_type", "interpreter", "chost", "cbuild",
                           "cflags", "cxxflags", "fcflags", "fflags", "asflags", "common_flags", "binrepo_path",
                           "portage_prefix", "snapshot_treeish", "source_subpath", "update_seed_command", "rename_regexp"}
                if field not in allowed or StageArgumentDetails.named(field) is None:
                    raise ValueError("Unsupported stage setting")
                ProjectManager.shared().change_stage_argument(project, stage, field, value)
            return f"Updated {project.name}/{stage.name}: {field} = {value}."
        return self._on_main_thread(action)

    def _resolve_stage_settings_context(self, project_key, stage_key, require_toolset=True):
        from .repositories import Repository
        from .toolset_selection import build_toolset_requirements, default_toolset
        from . import project_toolsets
        project = self._project(project_key)
        stage = next((item for item in project.stages
                      if item.name == stage_key or str(item.id) == stage_key), None)
        if stage is None:
            raise ValueError(f"No stage named or identified {stage_key!r}")
        selected_id = project_toolsets.get(project.directory_path())
        toolset = next((item for item in Repository.Toolset.value if str(item.uuid) == selected_id), None)
        toolset = toolset or default_toolset(build_toolset_requirements(project))
        if toolset is None:
            if require_toolset:
                raise ValueError("No Catalyst toolset is available to inspect this stage")
            return project, stage, None, {}
        from .project_stage import load_catalyst_stage_arguments_details
        details = load_catalyst_stage_arguments_details(toolset, stage.target)
        return project, stage, toolset, details

    def _get_stage_settings(self, arguments):
        project, stage, toolset, details = self._resolve_stage_settings_context(
            arguments.get("project", ""), str(arguments.get("stage", "")), require_toolset=False)
        from .project_stage import refresh_default_stage_arguments
        from .project_stage_argument_serialization import ProjectStageArgumentSerialization
        from .project_stage_value_resolver import resolve_stage_argument, UNRESOLVED
        from .project_stage_arguments import StageArgumentDetails
        from .project_stage_value_resolver import format_stage_argument_value

        # Populate runtime defaults in memory only, matching the stage editor's
        # effective-value preview without persisting calculated values.
        if toolset is not None:
            refresh_default_stage_arguments(
                project, stage, toolset=toolset, snapshot=project.get_snapshot(), arguments_details=details)
        requested = str(arguments.get("setting", "")).strip()

        def encode(value):
            if value is UNRESOLVED:
                return {"state": "unresolved"}
            if value is None:
                return None
            return ProjectStageArgumentSerialization.serialize(value)

        supported_names = {argument.attribute_name for argument in details.values()}
        explicit_none = getattr(stage, "_explicit_none_arguments", set())
        structural = {
            "event_bus", "_explicit_none_arguments", "_default_stage_arguments",
            "_explicit_stage_arguments", "_explicit_user_arguments",
            "_explicit_user_argument_dates", "id", "parent", "target", "name", "releng_template",
        }
        stored_names = {
            name for name, value in stage.__dict__.items()
            if name not in structural and (value is not None or name in explicit_none)
        }
        unsupported_names = stored_names - supported_names
        hidden = {StageArgumentDetails.snapshot_treeish, StageArgumentDetails.source_subpath}
        unsupported = []
        for name in sorted(unsupported_names):
            setting_details = StageArgumentDetails.named(name)
            if setting_details in hidden:
                continue
            if requested and requested != name:
                continue
            raw_value = getattr(stage, name, None)
            try:
                configured_value = encode(raw_value)
                json.dumps(configured_value)
            except (TypeError, ValueError):
                configured_value = {"type": type(raw_value).__name__, "value": str(raw_value)}
            unsupported.append({
                "name": name,
                "label": setting_details.display_name if setting_details else name,
                "status": "not_supported_by_target" if toolset else "support_unverified",
                "configured_value": configured_value,
                "stored_as_explicit_none": name in explicit_none,
            })
        if requested and requested not in details and not any(
                item["name"] == requested for item in unsupported):
            raise ValueError("That setting is neither supported by this target nor a saved unsupported value")

        settings = []
        for name, argument in details.items():
            if requested and name != requested:
                continue
            attribute = argument.attribute_name
            configured = getattr(stage, attribute, None)
            explicit = attribute in getattr(stage, "_explicit_stage_arguments", set())
            try:
                effective = resolve_stage_argument(project, stage, name)
            except Exception as error:
                effective = UNRESOLVED
                resolution_error = str(error)
            else:
                resolution_error = None
            source = ("explicit" if explicit else
                      "calculated_default" if attribute in getattr(stage, "_default_stage_arguments", {})
                      else "unset")
            entry = {
                "name": name,
                "label": argument.display_name,
                "required": argument.required,
                "type": argument.type.name,
                "source": source,
                "configured_value": encode(configured),
                "effective_value": encode(effective),
                "effective_display": format_stage_argument_value(effective),
            }
            if resolution_error:
                entry["resolution_error"] = resolution_error
            settings.append(entry)
        return self._json({"project": project.name, "stage": stage.name,
                           "stage_id": str(stage.id), "target": stage.target,
                           "settings": settings, "unsupported_settings": unsupported})

    def _stage_kernel_context(self, arguments):
        project = self._project(arguments.get("project", ""))
        stage_key = str(arguments.get("stage", ""))
        stage = next((item for item in project.stages
                      if item.name == stage_key or str(item.id) == stage_key), None)
        if stage is None:
            raise ValueError(f"No stage named or identified {stage_key!r} in {project.name}")
        return project, stage

    def _list_stage_kernels(self, arguments):
        from .project_stage_kernels import (
            KERNEL_SETTINGS, kernel_setting, own_kernel_names, own_kernel_settings,
            releng_kernel_names, stage_inherits_kernels, stage_kernel_names,
        )
        project, stage = self._stage_kernel_context(arguments)
        inherited_names = set(releng_kernel_names(project, stage))
        own_names = set(own_kernel_names(stage))
        overrides = own_kernel_settings(stage)
        kernels = []
        for name in stage_kernel_names(project, stage):
            settings = []
            for definition in KERNEL_SETTINGS:
                effective, is_own = kernel_setting(project, stage, name, definition.key)
                settings.append({
                    "key": definition.key,
                    "label": definition.title,
                    "type": definition.type.value,
                    "source": "stage" if is_own else "releng" if effective is not None else "unset",
                    "configured_value": overrides.get(name, {}).get(definition.key),
                    "effective_value": effective,
                })
            kernels.append({"name": name,
                            "source": "releng" if name in inherited_names and name not in own_names else "stage",
                            "settings": settings})
        return self._json({"project": project.name, "stage": stage.name,
                           "stage_id": str(stage.id),
                           "includes_releng_kernels": stage_inherits_kernels(stage),
                           "releng_kernels": sorted(inherited_names),
                           "stage_kernels": sorted(own_names),
                           "kernels": kernels})

    def _update_stage_kernel(self, arguments):
        from .project_kernel_packages import default_kernel_package
        from .project_stage_kernels import (
            KERNEL_SETTINGS, KernelSettingType, kernel_name_error, kernel_setting,
            new_kernel_name, own_kernel_names, own_kernel_settings, releng_kernel_setting,
            rename_kernel, set_kernel_names, set_kernel_setting, stage_inherits_kernels,
            stage_kernel_names,
        )
        project, stage = self._stage_kernel_context(arguments)
        action = str(arguments.get("action", ""))
        names = stage_kernel_names(project, stage)

        if action == "add":
            name = str(arguments.get("name") or new_kernel_name(names)).strip()
            if error := kernel_name_error(name, names):
                raise ValueError(error)
            own = own_kernel_names(stage) if stage_inherits_kernels(stage) else names
            package = default_kernel_package(project, stage)

            def add():
                set_kernel_names(project, stage, own + [name], inherit=stage_inherits_kernels(stage))
                if package:
                    set_kernel_setting(project, stage, name, "sources", package)
                set_kernel_setting(project, stage, name, "distkernel", "yes")
                return self._json({"status": "added", "project": project.name,
                                   "stage": stage.name, "kernel": name,
                                   "default_package": package})
            return self._on_main_thread(add)

        if action == "rename":
            old_name = str(arguments.get("name", "")).strip()
            new_name = str(arguments.get("new_name", "")).strip()
            if old_name not in names:
                raise ValueError(f"Kernel {old_name!r} is not in this stage")
            if error := kernel_name_error(new_name, [item for item in names if item != old_name]):
                raise ValueError(error)
            def rename():
                rename_kernel(project, stage, old_name, new_name)
                return self._json({"status": "renamed", "project": project.name,
                                   "stage": stage.name, "old_name": old_name,
                                   "new_name": new_name})
            return self._on_main_thread(rename)

        if action == "set_releng_inheritance":
            if not isinstance(arguments.get("include_releng"), bool):
                raise ValueError("include_releng must be true or false")
            include = arguments["include_releng"]
            def set_inheritance():
                set_kernel_names(project, stage, own_kernel_names(stage), inherit=include)
                return self._json({"status": "updated", "project": project.name,
                                   "stage": stage.name, "includes_releng_kernels": include,
                                   "stage_kernels": own_kernel_names(stage)})
            return self._on_main_thread(set_inheritance)

        if action in {"set_setting", "reset_setting"}:
            name = str(arguments.get("name", "")).strip()
            if name not in names:
                raise ValueError(f"Kernel {name!r} is not in this stage")
            setting_key = str(arguments.get("setting", "")).strip()
            definition = next((item for item in KERNEL_SETTINGS if item.key == setting_key), None)
            if definition is None:
                raise ValueError("Unknown kernel setting; use list_stage_kernels for supported settings")
            if action == "reset_setting":
                def reset():
                    set_kernel_setting(project, stage, name, setting_key, None)
                    return self._json({"status": "reset", "project": project.name,
                                       "stage": stage.name, "kernel": name,
                                       "setting": setting_key})
                return self._on_main_thread(reset)
            value = arguments.get("value")
            if definition.type == KernelSettingType.BOOLEAN:
                if not isinstance(value, bool):
                    raise ValueError("distkernel needs a boolean")
                releng_value = releng_kernel_setting(project, stage, name, setting_key)
                releng_enabled = isinstance(releng_value, str) and releng_value.strip().lower() == "yes"
                stored = None if value == releng_enabled else "yes" if value else "no"
            elif definition.type == KernelSettingType.LIST:
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError(f"{setting_key} needs an array of strings")
                stored = value or None
            else:
                if not isinstance(value, str):
                    raise ValueError(f"{setting_key} needs a string")
                stored = value.strip() or None
            def set_setting():
                set_kernel_setting(project, stage, name, setting_key, stored)
                effective, is_own = kernel_setting(project, stage, name, setting_key)
                return self._json({"status": "updated", "project": project.name,
                                   "stage": stage.name, "kernel": name,
                                   "setting": setting_key, "effective_value": effective,
                                   "source": "stage" if is_own else "releng" if effective is not None else "unset"})
            return self._on_main_thread(set_setting)

        raise ValueError("Unsupported kernel action")

    def _delete_stage_kernel(self, arguments):
        from .project_stage_kernels import (
            remove_kernel, releng_kernel_names, stage_inherits_kernels, stage_kernel_names,
        )
        project, stage = self._stage_kernel_context(arguments)
        name = str(arguments.get("name", "")).strip()
        if name not in stage_kernel_names(project, stage):
            raise ValueError(f"Kernel {name!r} is not in this stage")
        if stage_inherits_kernels(stage) and name in releng_kernel_names(project, stage):
            raise ValueError("This kernel comes from the Releng template. Disable Releng kernel inheritance to replace the template kernel list.")
        if self.user_input_callback is None:
            raise RuntimeError("Removing a stage kernel requires an in-chat confirmation form")
        answer = self.user_input_callback(
            "Confirm kernel removal",
            f"Permanently remove kernel {name} and its stage-specific settings from {project.name}/{stage.name}?",
            [{"id": "confirm", "label": f"Remove kernel {name}", "type": "boolean",
              "placeholder": "I understand this removes the kernel configuration", "required": True}],
        )
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "project": project.name,
                               "stage": stage.name, "kernel": name})
        def remove():
            if name not in stage_kernel_names(project, stage):
                raise RuntimeError("The stage kernel list changed while confirming; nothing was removed")
            if stage_inherits_kernels(stage) and name in releng_kernel_names(project, stage):
                raise RuntimeError("The kernel is now inherited from Releng; nothing was removed")
            remove_kernel(project, stage, name)
            return self._json({"status": "removed", "project": project.name,
                               "stage": stage.name, "kernel": name})
        return self._on_main_thread(remove)

    @staticmethod
    def _decode_stage_value(value, argument, options):
        from .project_stage_arguments import StageArgumentType
        from .project_stage_automatic_option import StageAutomaticOption
        auto_tokens = {
            "@inherit_parent": StageAutomaticOption.INHERIT_FROM_PARENT,
            "@inherit_releng_template": StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE,
            "@automatic": StageAutomaticOption.GENERATE_AUTOMATICALLY,
        }
        allowed_auto = set(argument.details.automatic_options if argument.details else [])
        option_by_token = {AIToolBridge._stage_option_token(item.value): item.value for item in options}

        def decode(token):
            if not isinstance(token, str):
                raise ValueError("Selectable stage option values must be strings from stage_setting_options")
            if token in auto_tokens:
                option = auto_tokens[token]
                if option not in allowed_auto:
                    raise ValueError(f"{token} is not supported for this stage setting")
                return option
            if token not in option_by_token:
                raise ValueError(f"{token!r} is not an available option; call list_options again")
            return option_by_token[token]

        kind = argument.type
        if kind == StageArgumentType.boolean:
            if not isinstance(value, bool):
                raise ValueError("This stage setting needs a boolean")
            return value
        if kind in (StageArgumentType.raw, StageArgumentType.raw_single_line):
            if not isinstance(value, str):
                raise ValueError("This stage setting needs a text value")
            if value in auto_tokens:
                option = auto_tokens[value]
                if option not in allowed_auto:
                    raise ValueError(f"{value} is not supported for this stage setting")
                return option
            return value
        if kind == StageArgumentType.string_list:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError("This stage setting needs an array of text lines")
            decoded = [auto_tokens[item] if item in auto_tokens else item for item in value]
            if any(isinstance(item, StageAutomaticOption) and item not in allowed_auto for item in decoded):
                raise ValueError("This automatic value is not supported for this stage setting")
            if len(decoded) > 1 and not argument.details.allows_additions and any(
                    isinstance(item, StageAutomaticOption) for item in decoded):
                raise ValueError("This setting cannot combine automatic and custom values")
            return decoded
        if kind == StageArgumentType.multiselect:
            if not isinstance(value, list):
                raise ValueError("This stage setting needs an array of option values")
            decoded = [decode(item) for item in value]
            if len(decoded) > 1 and not argument.details.allows_additions and any(
                    isinstance(item, StageAutomaticOption) for item in decoded):
                raise ValueError("This setting cannot combine automatic and custom values")
            return decoded or None
        if kind == StageArgumentType.select:
            if not isinstance(value, str):
                raise ValueError("This stage setting needs one option value")
            return decode(value)
        raise ValueError(f"Unsupported stage setting type: {kind.name}")

    def _set_stage_setting(self, arguments):
        project, stage, _toolset, details = self._resolve_stage_settings_context(
            arguments.get("project", ""), str(arguments.get("stage", "")))
        setting = str(arguments.get("setting", "")).strip()
        argument = details.get(setting)
        if argument is None:
            raise ValueError("That setting is not supported by the selected Catalyst target")
        options = self._stage_setting_options(project, stage, argument, _toolset)
        value = self._decode_stage_value(arguments.get("value"), argument, options)
        def action():
            from .project_manager import ProjectManager
            ProjectManager.shared().change_stage_argument(project, stage, setting, value)
            return self._json({"status": "updated", "project": project.name, "stage": stage.name,
                               "setting": setting, "value": value})
        return self._on_main_thread(action)

    def _reset_stage_setting(self, arguments):
        project, stage, _toolset, details = self._resolve_stage_settings_context(
            arguments.get("project", ""), str(arguments.get("stage", "")))
        setting = str(arguments.get("setting", "")).strip()
        if setting not in details:
            raise ValueError("That setting is not supported by the selected Catalyst target")
        def action():
            from .project_manager import ProjectManager
            ProjectManager.shared().remove_stage_argument(project, stage, setting)
            return self._json({"status": "reset", "project": project.name, "stage": stage.name,
                               "setting": setting})
        return self._on_main_thread(action)

    def _remove_unsupported_stage_setting(self, arguments):
        project, stage, toolset, details = self._resolve_stage_settings_context(
            arguments.get("project", ""), str(arguments.get("stage", "")), require_toolset=False)
        setting = str(arguments.get("setting", "")).strip()
        if not setting:
            raise ValueError("Pass an exact setting name from unsupported_settings")
        from .project_stage_arguments import StageArgumentDetails
        if StageArgumentDetails.named(setting) in {
                StageArgumentDetails.snapshot_treeish, StageArgumentDetails.source_subpath}:
            raise ValueError("This managed stage value cannot be removed as an unsupported setting")
        supported_names = {argument.attribute_name for argument in details.values()}
        if setting in supported_names:
            raise ValueError("This setting is supported; use reset_stage_setting to remove its override")
        explicit_none = getattr(stage, "_explicit_none_arguments", set())
        if not (setting in stage.__dict__ and
                (getattr(stage, setting, None) is not None or setting in explicit_none)):
            raise ValueError("That saved unsupported setting is no longer present; inspect get_stage_settings again")
        if self.user_input_callback is None:
            raise RuntimeError("Removing a saved stage setting requires an in-chat confirmation form")
        answer = self.user_input_callback(
            "Confirm saved-setting removal",
            f"Remove the saved value for unsupported setting {setting} from {project.name}/{stage.name}?",
            [{"id": "confirm", "label": f"Remove {setting}", "type": "boolean",
              "placeholder": "I understand this removes the saved stage value", "required": True}],
        )
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "project": project.name,
                               "stage": stage.name, "setting": setting})

        def action():
            current_explicit_none = getattr(stage, "_explicit_none_arguments", set())
            still_present = setting in stage.__dict__ and (
                getattr(stage, setting, None) is not None or setting in current_explicit_none)
            still_unsupported = setting not in {item.attribute_name for item in details.values()}
            if not still_present or not still_unsupported:
                raise RuntimeError("The stage setting changed while confirming; nothing was removed")
            from .project_manager import ProjectManager
            ProjectManager.shared().remove_stage_argument(project, stage, setting)
            return self._json({"status": "removed", "project": project.name,
                               "stage": stage.name, "setting": setting,
                               "support_verified": toolset is not None})
        return self._on_main_thread(action)

    def _write_stage_script(self, arguments):
        """Write a project stage fsscript, limited to its own stage directory."""
        project, stage, _toolset, details = self._resolve_stage_settings_context(
            arguments.get("project", ""), str(arguments.get("stage", "")))
        setting = str(arguments.get("setting", ""))
        if setting not in {"stage4/fsscript", "livecd/fsscript"} or setting not in details:
            raise ValueError("This stage does not support the requested filesystem script setting")
        content = arguments.get("content")
        if not isinstance(content, str) or len(content) > 100_000:
            raise ValueError("Script content must be text no longer than 100,000 characters")
        project_root = Path(project.directory_path()).resolve()
        stage_root = Path(project.stage_directory_path(stage.name)).resolve()
        if stage_root == project_root or not stage_root.is_relative_to(project_root):
            raise ValueError("The project's stage directory resolves outside the project")
        current_value = getattr(stage, setting, None)
        if current_value not in (None, "") and not (
                isinstance(current_value, str) and current_value.startswith("@STAGE_DIR@/")):
            raise ValueError("This stage script is outside the project and cannot be edited with the app")
        relative = current_value[len("@STAGE_DIR@/"):] if (
            isinstance(current_value, str) and current_value.startswith("@STAGE_DIR@/")
        ) else None
        if relative is None:
            relative = "fsscript.sh"
            suffix = 1
            while (stage_root / relative).exists():
                relative = f"fsscript-{suffix}.sh"
                suffix += 1
        if not relative or Path(relative).is_absolute():
            raise ValueError("The configured script path must be relative to @STAGE_DIR@")
        target = (stage_root / relative).resolve()
        if target == stage_root or not target.is_relative_to(stage_root):
            raise ValueError("The configured script path escapes the project's stage directory")
        already_exists = target.exists()
        if already_exists:
            if not target.is_file() or target.is_symlink():
                raise ValueError("The configured script exists but is not a regular file")
            if self.user_input_callback is None:
                raise RuntimeError("Replacing a stage script requires an in-chat confirmation form")
            answer = self.user_input_callback(
                "Confirm script replacement",
                f"Replace {project.name}/{stage.name}/{relative} with the new {len(content)}-character script?",
                [{"id": "confirm", "label": "Replace this stage script", "type": "boolean",
                  "required": True, "default": False}],
            )
            if not answer or not answer.get("confirm"):
                return self._json({"status": "cancelled", "project": project.name,
                                   "stage": stage.name, "setting": setting,
                                   "message": "The existing script was not changed."})

        def action():
            from .project_manager import ProjectManager
            from .git_directory import GitDirectoryEvent
            expected_current_value = current_value
            if getattr(stage, setting, None) != expected_current_value:
                raise RuntimeError("The configured script path changed while confirming; nothing was written")
            if target.exists() != already_exists:
                raise RuntimeError("The script file changed while preparing the write; nothing was written")
            stage_root.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            if already_exists:
                if not target.is_file() or target.is_symlink():
                    raise RuntimeError("The script target is no longer a regular file; nothing was written")
                target.write_text(content, encoding="utf-8")
            else:
                with target.open("x", encoding="utf-8") as script_file:
                    script_file.write(content)
            if relative != (current_value[len("@STAGE_DIR@/"):] if isinstance(current_value, str)
                           and current_value.startswith("@STAGE_DIR@/") else None):
                ProjectManager.shared().change_stage_argument(
                    project, stage, setting, f"@STAGE_DIR@/{relative}")
            else:
                project.event_bus.emit(GitDirectoryEvent.CONTENT_CHANGED, project)
            return self._json({"status": "written", "project": project.name, "stage": stage.name,
                               "setting": setting, "path": f"@STAGE_DIR@/{relative}",
                               "characters": len(content), "overwrote": already_exists})
        return self._on_main_thread(action)

    def _read_stage_script(self, arguments):
        """Read only an fsscript located within the selected project's stage folder."""
        project, stage, _toolset, details = self._resolve_stage_settings_context(
            arguments.get("project", ""), str(arguments.get("stage", "")))
        setting = str(arguments.get("setting", ""))
        if setting not in {"stage4/fsscript", "livecd/fsscript"} or setting not in details:
            raise ValueError("This stage does not support the requested filesystem script setting")
        value = getattr(stage, setting, None)
        if not isinstance(value, str) or not value.startswith("@STAGE_DIR@/"):
            return self._json({"status": "not_project_owned", "project": project.name,
                               "stage": stage.name, "setting": setting})
        project_root = Path(project.directory_path()).resolve()
        stage_root = Path(project.stage_directory_path(stage.name)).resolve()
        if stage_root == project_root or not stage_root.is_relative_to(project_root):
            raise ValueError("The project's stage directory resolves outside the project")
        relative = value[len("@STAGE_DIR@/"):]
        target = (stage_root / relative).resolve()
        if target == stage_root or not target.is_relative_to(stage_root):
            raise ValueError("The configured script escapes the project stage directory")
        if not target.is_file():
            return self._json({"status": "missing", "project": project.name,
                               "stage": stage.name, "setting": setting, "path": value})
        text = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(text) > 100_000
        if truncated:
            text = text[:100_000] + "\n[Script output truncated at 100,000 characters.]"
        return self._json({"status": "read", "project": project.name, "stage": stage.name,
                           "setting": setting, "path": value, "content": text,
                           "truncated": truncated})

    def _delete_stage(self, arguments):
        project = self._project(arguments.get("project", ""))
        stage_key = str(arguments.get("stage", ""))
        stage = next((item for item in project.stages
                      if item.name == stage_key or str(item.id) == stage_key), None)
        if stage is None:
            raise ValueError(f"No stage named or identified {stage_key!r}")
        children = [item for item in project.stages
                    if getattr(item, "parent", None) == stage.id]
        if self.user_input_callback is None:
            raise RuntimeError("Stage deletion requires an in-chat confirmation form")
        child_text = (" Child stages will remain and have their parent cleared: " +
                      ", ".join(item.name for item in children) + ".") if children else ""
        answers = self.user_input_callback(
            "Confirm stage deletion",
            f"Permanently remove {stage.name} from {project.name}.{child_text}",
            [{"id": "confirm", "label": f"Delete stage {stage.name}", "type": "boolean",
              "placeholder": "I understand this removes the stage configuration", "required": True}],
        )
        if not answers or not answers.get("confirm"):
            return self._json({"status": "cancelled", "project": project.name, "stage": stage.name})
        from .project_build_process import running_project_build
        if running_project_build(project) is not None:
            raise RuntimeError("Stop the project's running build before deleting a stage")
        def action():
            from .project_manager import ProjectManager
            detached = ProjectManager.shared().remove_stage(project, stage)
            return self._json({"status": "deleted", "project": project.name, "stage": stage.name,
                               "detached_children": [item.name for item in detached]})
        return self._on_main_thread(action)

    @staticmethod
    def _on_main_thread(action, timeout: float = 30):
        from gi.repository import GLib
        completed = threading.Event()
        outcome = {}
        def run():
            try:
                outcome["result"] = action()
            except Exception as error:
                outcome["error"] = error
            finally:
                completed.set()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(run)
        if not completed.wait(timeout):
            raise RuntimeError("Timed out waiting for the app action")
        if error := outcome.get("error"):
            raise error
        return outcome.get("result")

    @staticmethod
    def _objects_for_type(kind: str):
        from .repositories import Repository
        repositories = {
            "project": Repository.ProjectDirectory,
            "overlay": Repository.OverlayDirectory,
            "releng": Repository.RelengDirectory,
        }
        if kind not in repositories:
            raise ValueError("Files can only be read from projects, overlays, or Releng directories")
        return repositories[kind].value

    @staticmethod
    def _find_any_object(kind: str, name: str):
        from .repositories import Repository
        repositories = {
            "project": Repository.ProjectDirectory,
            "toolset": Repository.Toolset,
            "snapshot": Repository.Snapshot,
            "releng": Repository.RelengDirectory,
            "overlay": Repository.OverlayDirectory,
            "packages": Repository.PackagesDirectory,
            "runner": Repository.Runner,
        }
        repository = repositories.get(kind)
        if repository is None:
            raise ValueError(f"Unsupported object type: {kind}")
        item = next((item for item in repository.value
                     if getattr(item, "name", None) == name
                     or str(getattr(item, "id", getattr(item, "uuid", ""))) == name), None)
        if item is None:
            raise ValueError(f"No {kind} named or identified {name!r}")
        return item

    def _find_object(self, kind: str, name: str):
        items = self._objects_for_type(kind)
        match = next((item for item in items if item.name == name or str(item.id) == name), None)
        if match is None:
            raise ValueError(f"No {kind} named or identified {name!r}")
        return match

    @staticmethod
    def _read_text(path: str, root: str, end_if_large: bool = True) -> str:
        root_path = Path(root).resolve(strict=True)
        candidate = (root_path / path).resolve(strict=True)
        if not candidate.is_relative_to(root_path) or not candidate.is_file():
            raise ValueError("File must be inside the selected app object")
        if candidate.stat().st_size > MAX_READ_BYTES:
            with candidate.open("rb") as file:
                file.seek(-MAX_READ_BYTES, os.SEEK_END)
                content = file.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
            content = content[-MAX_TEXT_CHARS:]
            return "[Showing the end of the file (final 5 MB read; response capped).]\n" + content
        with candidate.open("r", encoding="utf-8", errors="replace") as file:
            content = file.read(MAX_READ_BYTES + 1)
        if len(content) > MAX_TEXT_CHARS:
            return content[-MAX_TEXT_CHARS:] if end_if_large else content[:MAX_TEXT_CHARS]
        return content

    def _read_object_file(self, arguments):
        kind = str(arguments.get("type", "")).lower()
        if kind not in {"project", "overlay", "releng"}:
            raise ValueError("Files can only be read from a project, overlay, or Releng directory")
        item = self._find_object(kind, str(arguments.get("name", "")))
        relative = str(arguments.get("path", ""))
        path = Path(relative)
        if path.is_absolute() or not path.parts or any(part in ("..", ".git") for part in path.parts):
            raise ValueError("path must be a file path inside the selected object; Git internals are not exposed")
        root = Path(item.directory_path()).resolve(strict=True)
        target = root
        for part in path.parts:
            target = target / part
            if target.is_symlink():
                raise ValueError("File paths must not traverse symlinks")
        target = target.resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("File must be inside the selected app object")
        content = self._read_text(str(target), str(root))
        return f"Read {item.name}/{relative}\n{content}"

    def _list_object_files(self, arguments):
        kind = str(arguments.get("type", "")).lower()
        if kind not in {"project", "overlay", "releng"}:
            raise ValueError("Files can only be listed in a project, overlay, or Releng directory")
        item = self._find_object(kind, str(arguments.get("name", "")))
        root = Path(item.directory_path()).resolve(strict=True)
        relative = str(arguments.get("path", "") or "")
        if Path(relative).is_absolute():
            raise ValueError("path must be relative to the selected object")
        raw_start = root / relative
        cursor = root
        for part in Path(relative).parts:
            if part in ("", "."):
                continue
            if part == ".git":
                raise ValueError("Git internals are not exposed")
            if part == "..":
                cursor = cursor.parent
            else:
                cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("path must not traverse a symlink")
        start = raw_start.resolve(strict=True)
        if not start.is_relative_to(root) or not start.is_dir():
            raise ValueError("path must name a directory inside the selected object")
        recursive = arguments.get("recursive", False)
        if not isinstance(recursive, bool):
            raise ValueError("recursive must be true or false")
        try:
            limit = max(1, min(int(arguments.get("limit", 200)), 500))
        except (TypeError, ValueError):
            raise ValueError("limit must be an integer from 1 to 500")

        entries = []
        for current, directories, files in os.walk(start, followlinks=False):
            directories[:] = sorted(name for name in directories
                                    if name != ".git" and not os.path.islink(os.path.join(current, name)))
            for name in sorted(directories) + sorted(files):
                if name == ".git":
                    continue
                full_path = os.path.join(current, name)
                if os.path.islink(full_path):
                    continue
                is_directory = os.path.isdir(full_path)
                relative_path = os.path.relpath(full_path, root)
                entry = {"path": relative_path, "type": "directory" if is_directory else "file"}
                if not is_directory:
                    try:
                        entry["size_bytes"] = os.path.getsize(full_path)
                    except OSError:
                        continue
                entries.append(entry)
                if len(entries) >= limit:
                    return self._json({"object": item.name, "path": relative or ".",
                                       "entries": entries, "truncated": True})
            if not recursive:
                break
        return self._json({"object": item.name, "path": relative or ".",
                           "entries": entries, "truncated": False})

    def _write_object_file(self, arguments):
        kind = str(arguments.get("type", "")).lower()
        if kind not in {"project", "overlay", "releng"}:
            raise ValueError("Files can only be written in a project, overlay, or Releng directory")
        item = self._find_object(kind, str(arguments.get("name", "")))
        relative = str(arguments.get("path", ""))
        content = arguments.get("content")
        if not isinstance(content, str) or len(content) > 100_000:
            raise ValueError("File content must be text no longer than 100,000 characters")
        path = Path(relative)
        if path.is_absolute() or not path.parts or any(part in ("", ".", "..", ".git") for part in path.parts):
            raise ValueError("path must be a non-empty relative path without traversal or Git internals")

        root = Path(item.directory_path()).resolve(strict=True)
        target = root
        for part in path.parts[:-1]:
            target = target / part
            if target.is_symlink():
                raise ValueError("File paths must not traverse symlinks")
        parent = target.resolve(strict=True)
        if not parent.is_relative_to(root) or not parent.is_dir():
            raise ValueError("The file's parent directory must already exist inside the selected object")
        destination = parent / path.parts[-1]
        if destination.is_symlink():
            raise ValueError("Replacing a symlink is not allowed")
        if destination.exists() and not destination.is_file():
            raise ValueError("The target path is not a regular file")
        old_mode = (destination.stat().st_mode & 0o777) if destination.exists() else 0o644
        verb = "Replace" if destination.exists() else "Create"
        if destination.exists():
            result = json.loads(self._ask_user({
                "title": "Confirm file replacement",
                "message": f"Replace {kind} file {item.name}/{relative} ({len(content)} characters)?",
                "fields": [{"id": "confirm", "label": "Replace this file",
                            "type": "boolean", "required": True, "default": False}],
            }))
            if result.get("status") != "submitted":
                return self._json({"status": "cancelled", "message": "No file was changed."})
            if result.get("user_feedback"):
                return self._json({"status": "revision_requested", "message": result["user_feedback"]})
            if not (result.get("answers") or {}).get("confirm"):
                return self._json({"status": "cancelled", "message": "No file was changed."})

        descriptor, temporary_path = tempfile.mkstemp(prefix=".catalystlab-write-", dir=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary_path, old_mode)
            # Recheck after a replacement confirmation, or just before creating a new file.
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise ValueError("The target path changed while awaiting confirmation")
            os.replace(temporary_path, destination)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
        return self._json({"status": "written", "object_type": kind,
                           "object": item.name, "path": relative,
                           "characters": len(content), "overwrote": verb == "Replace"})

    def _project(self, name):
        return self._find_object("project", str(name))

    def _list_builds(self, arguments):
        from .project_build import load_project_builds
        project = self._project(arguments.get("project", ""))
        status_filter = str(arguments.get("status_filter", "all")).strip().lower()
        allowed_filters = {"all", "completed", "failed_canceled"}
        if status_filter not in allowed_filters:
            raise ValueError("status_filter must be all, completed, or failed_canceled")
        try:
            limit = max(1, min(int(arguments.get("limit", 20)), 100))
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = max(0, min(int(arguments.get("offset", 0)), 1_000_000))
        except (TypeError, ValueError):
            offset = 0
        builds = load_project_builds(project)
        if status_filter == "completed":
            builds = [item for item in builds if item.status.value == "completed"]
        elif status_filter == "failed_canceled":
            # Match the Builds screen's Failed / Canceled filter, including
            # dependent stages skipped after failure and incomplete records.
            builds = [item for item in builds if item.status.value in {
                "failed", "canceled", "stopped", "skipped", "scheduled", "in_progress",
            }]
        total = len(builds)
        selected = builds[offset:offset + limit]
        rows = [{"stage": item.stage_name, "stage_id": str(item.stage_id),
                 "timestamp": item.timestamp, "status": item.status.value,
                 "date": item.date.isoformat(), "failure_summary": item.failure_summary,
                 "details": item.details, "files_available": bool(item.path)} for item in selected]
        next_offset = offset + len(rows) if offset + len(rows) < total else None
        return self._json({"project": project.name, "status_filter": status_filter,
                           "total_count": total, "offset": offset, "limit": limit,
                           "returned_count": len(rows), "next_offset": next_offset,
                           "builds": rows})

    def _get_build_run(self, arguments):
        from .project_build import load_project_builds
        from .project_build_process import running_project_build
        from .project_build_record import build_records_directory, load_build_record

        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("timestamp", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", timestamp):
            raise ValueError("Pass the exact build timestamp returned by list_builds")
        matches = [build for build in load_project_builds(project)
                   if build.timestamp == timestamp]
        if not matches:
            raise ValueError("No saved build run matches that project and timestamp; refresh with list_builds")
        if running_project_build(project, timestamp) is not None:
            raise ValueError("This build is still running; use list_operations and read_operation_log")
        record_path = os.path.join(build_records_directory(project), f"{timestamp}.json")
        if not os.path.isfile(record_path):
            raise ValueError("The saved step-by-step record is unavailable for this build run")
        lines_per_step = arguments.get("lines_per_step", 40)
        if type(lines_per_step) is not int or not 1 <= lines_per_step <= 100:
            raise ValueError("lines_per_step must be between 1 and 100")

        run = load_build_record(project, timestamp)
        remaining_lines = 500
        remaining_chars = 30_000
        steps = []
        omitted_lines = 0
        for step in run.stages:
            output = [str(line) for line in getattr(step, "output_lines", [])]
            selected_source = output[-min(lines_per_step, remaining_lines):] if remaining_lines else []
            selected = []
            line_text_truncated = False
            for line in selected_source:
                if remaining_chars <= 0:
                    break
                if len(line) > 4_000:
                    line = "[Line truncated; showing its end.] " + line[-3_960:]
                    line_text_truncated = True
                if len(line) > remaining_chars:
                    marker = "[Line truncated; showing its end.] "
                    line = (line[-remaining_chars:] if remaining_chars <= len(marker)
                            else marker + line[-(remaining_chars - len(marker)):])
                    line_text_truncated = True
                selected.append(line)
                remaining_chars -= len(line)
            omitted = len(output) - len(selected)
            omitted_lines += omitted
            remaining_lines -= len(selected)
            steps.append({
                "name": step.name,
                "description": step.description,
                "status": getattr(getattr(step, "state", None), "name", "unknown").lower(),
                "progress": step.progress,
                "output_lines_available": len(output),
                "output_lines_returned": len(selected),
                "output_truncated": omitted > 0 or line_text_truncated,
                "output": selected,
            })
        status = "stopped" if run.canceled else run.status.name.lower()
        return self._json({
            "project": project.name, "timestamp": timestamp, "title": run.title,
            "status": status, "step_count": len(steps), "steps": steps,
            "output_budget_lines": 500, "output_budget_characters": 30_000,
            "outputs_truncated": omitted_lines > 0,
            "omitted_output_lines": omitted_lines,
        })

    def _list_deployable_builds(self, arguments):
        from .repositories import Repository
        from .project_build import load_project_builds
        from .deploy_installation import is_deployable
        from .deploy_live_iso import is_live_iso_build, supports_raspberry_pi_iso

        project_key = str(arguments.get("project", "")).strip()
        if project_key:
            projects = [self._project(project_key)]
        else:
            projects = list(Repository.ProjectDirectory.value)
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 50)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        builds = []
        for project in projects:
            for build in load_project_builds(project):
                live_iso = is_live_iso_build(build)
                stage_deploy = is_deployable(project, build)
                if not (live_iso or stage_deploy):
                    continue
                builds.append({
                    "project": project.name,
                    "project_id": str(project.id),
                    "stage": build.stage_name,
                    "stage_id": str(build.stage_id),
                    "timestamp": build.timestamp,
                    "date": build.date.isoformat(),
                    "artifact": build.artifact,
                    "artifact_path": build.artifact_path,
                    "kind": "live_iso" if live_iso else "stage_archive",
                    "raspberry_pi_iso_supported": supports_raspberry_pi_iso(project, build) if live_iso else False,
                })
        builds.sort(key=lambda item: item["date"], reverse=True)
        page = builds[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(builds) else None
        return self._json({"builds": page, "offset": offset, "limit": limit,
                           "total_count": len(builds), "next_offset": next_offset})

    @staticmethod
    def _raspberry_pi_boot_settings(arguments, enabled):
        """Build validated Pi boot settings, preserving the live-ISO wizard defaults."""
        from .deploy_live_iso import RaspberryPiBootSettings

        requested = arguments.get("raspberry_pi_settings")
        if not enabled:
            if requested is not None:
                raise ValueError("raspberry_pi_settings requires raspberry_pi=true")
            return None
        if requested is None:
            return RaspberryPiBootSettings()
        if not isinstance(requested, dict):
            raise ValueError("raspberry_pi_settings must be an object")
        allowed = {"serial_console", "extra_arguments", "config_lines", "older_models_firmware"}
        unexpected = set(requested) - allowed
        if unexpected:
            raise ValueError("raspberry_pi_settings accepts serial_console, extra_arguments, config_lines, and older_models_firmware")
        defaults = RaspberryPiBootSettings()
        serial_console = requested.get("serial_console", defaults.serial_console)
        older_models_firmware = requested.get(
            "older_models_firmware", defaults.older_models_firmware)
        if type(serial_console) is not bool or type(older_models_firmware) is not bool:
            raise ValueError("raspberry_pi_settings serial_console and older_models_firmware must be booleans")
        extra_arguments = requested.get("extra_arguments", defaults.extra_arguments)
        config_lines = requested.get("config_lines", defaults.config_lines)
        if not isinstance(extra_arguments, str) or len(extra_arguments) > 4096 or "\x00" in extra_arguments or "\n" in extra_arguments or "\r" in extra_arguments:
            raise ValueError("raspberry_pi_settings.extra_arguments must be a single-line string up to 4096 characters")
        if not isinstance(config_lines, str) or len(config_lines) > 16384 or "\x00" in config_lines:
            raise ValueError("raspberry_pi_settings.config_lines must be a string up to 16384 characters")
        return RaspberryPiBootSettings(
            serial_console=serial_console,
            extra_arguments=extra_arguments.strip(),
            config_lines=config_lines,
            older_models_firmware=older_models_firmware,
        )

    @staticmethod
    def _summarize_raspberry_pi_boot_settings(settings):
        if settings is None:
            return []
        from .deploy_live_iso import RaspberryPiBootSettings
        defaults = RaspberryPiBootSettings()
        summary = []
        if settings.serial_console != defaults.serial_console:
            summary.append("serial console enabled" if settings.serial_console else "serial console disabled")
        if settings.extra_arguments:
            summary.append("custom kernel arguments")
        if settings.config_lines != defaults.config_lines:
            summary.append("custom config.txt")
        if settings.older_models_firmware != defaults.older_models_firmware:
            summary.append("older-model firmware enabled" if settings.older_models_firmware
                           else "older-model firmware disabled")
        return summary

    def _prepare_live_iso_system_settings(self, arguments):
        """Build optional live-ISO settings and request secrets only through secure form fields."""
        from .deploy_installation import DeploySystemSettings
        from .deploy_system import LocalizationSettings, WifiSettings, local_public_keys

        set_root_password = arguments.get("set_root_password", False)
        enable_ssh = arguments.get("enable_ssh", False)
        if type(set_root_password) is not bool or type(enable_ssh) is not bool:
            raise ValueError("set_root_password and enable_ssh must be booleans")

        locales = arguments.get("locales", [])
        if not isinstance(locales, list) or any(not isinstance(value, str) for value in locales):
            raise ValueError("locales must be a list of locale names")
        localization = LocalizationSettings(
            timezone=str(arguments.get("timezone", "")).strip(),
            locales=locales,
            keymap=str(arguments.get("keymap", "")).strip(),
        )
        if error := localization.error():
            raise ValueError(error)

        key_ids = arguments.get("ssh_key_ids", [])
        if not isinstance(key_ids, list) or any(not isinstance(value, str) for value in key_ids):
            raise ValueError("ssh_key_ids must be a list of IDs from list_options(live_iso_options)")
        import hashlib
        available_keys = {
            hashlib.sha256((key.path + "\0" + key.key).encode()).hexdigest()[:20]: key
            for key in local_public_keys()
        }
        if len(set(key_ids)) != len(key_ids) or any(key_id not in available_keys for key_id in key_ids):
            raise ValueError("Choose SSH keys from list_options(live_iso_options)")
        ssh_keys = [available_keys[key_id].key for key_id in key_ids]

        wifi_data = arguments.get("wifi")
        wifi = None
        wifi_password_required = False
        if wifi_data is not None:
            if not isinstance(wifi_data, dict):
                raise ValueError("wifi must be an object")
            allowed_wifi_fields = {"ssid", "hidden", "country", "password_required"}
            if set(wifi_data) - allowed_wifi_fields:
                raise ValueError("wifi accepts ssid, hidden, country, and password_required; passwords use a secure form")
            hidden = wifi_data.get("hidden", False)
            wifi_password_required = wifi_data.get("password_required", False)
            if type(hidden) is not bool or type(wifi_password_required) is not bool:
                raise ValueError("Wi-Fi hidden and password_required values must be booleans")
            wifi = WifiSettings(
                ssid=str(wifi_data.get("ssid", "")).strip(),
                hidden=hidden,
                country=str(wifi_data.get("country", "")).strip(),
            )
            if error := wifi.error():
                raise ValueError(error)

        changed_localization = bool(localization.timezone or localization.locales or localization.keymap)
        changed = bool(set_root_password or enable_ssh or ssh_keys or wifi or changed_localization)
        if not changed:
            return None, False, []

        system = DeploySystemSettings(
            enable_ssh=enable_ssh,
            ssh_keys=ssh_keys,
            localization=localization,
            wifi=wifi,
        )
        secret_fields = []
        if set_root_password:
            secret_fields.append({"id": "root_password", "label": "Root password", "type": "secret",
                                  "sensitive": True, "placeholder": "Enter root password", "required": True})
        if wifi is not None and wifi_password_required:
            secret_fields.append({"id": "wifi_password", "label": "Wi-Fi password", "type": "secret",
                                  "sensitive": True, "placeholder": "Enter Wi-Fi password", "required": True})
        if secret_fields:
            if self.user_input_callback is None:
                raise RuntimeError("Live system passwords must be collected through a secure in-chat form")
            answers = self.user_input_callback(
                "Secure live system credentials",
                "Passwords are entered securely and are not included in tool arguments.",
                secret_fields,
            )
            if not answers:
                return None, True, []
            system.root_password = answers.get("root_password")
            if set_root_password and not system.root_password:
                raise ValueError("Root password is required")
            if wifi is not None and wifi_password_required:
                wifi.password = answers.get("wifi_password", "")
                if not wifi.password:
                    raise ValueError("Wi-Fi password is required")
                if error := wifi.error():
                    raise ValueError(error)
        summary = []
        if system.root_password:
            summary.append("set root password")
        if enable_ssh:
            summary.append("enable SSH")
        if ssh_keys:
            summary.append(f"authorize {len(ssh_keys)} SSH key(s)")
        if wifi:
            summary.append(f"configure Wi-Fi {wifi.ssid!r}")
        if localization.timezone:
            summary.append(f"timezone {localization.timezone}")
        if localization.locales:
            summary.append(f"locales {', '.join(localization.locales)}")
        if localization.keymap:
            summary.append(f"keyboard layout {localization.keymap}")
        return system, False, summary

    def _create_live_iso(self, arguments):
        from .project_build import load_project_builds
        from .deploy_live_iso import (LiveIsoDeployment, LiveIsoTarget,
                                      is_live_iso_build, supports_raspberry_pi_iso)
        from .deploy_image import image_supported_reason

        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("build_timestamp", "")).strip()
        build = next((item for item in load_project_builds(project) if item.timestamp == timestamp), None)
        if build is None or not is_live_iso_build(build):
            raise ValueError("Choose a completed live ISO from list_deployable_builds")
        raspberry_pi = arguments.get("raspberry_pi", False)
        if type(raspberry_pi) is not bool:
            raise ValueError("raspberry_pi must be a boolean")
        if raspberry_pi:
            if not supports_raspberry_pi_iso(project, build):
                raise ValueError("This live ISO does not have a supported Raspberry Pi kernel")
            if reason := image_supported_reason(project):
                raise ValueError(reason)

        requested_path = str(arguments.get("output_path", "")).strip()
        if not requested_path:
            raise ValueError("Choose a destination .iso file path")
        output_path = Path(requested_path).expanduser()
        if not output_path.is_absolute():
            raise ValueError("Destination path must be absolute")
        parent = output_path.parent.resolve()
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise ValueError("Destination folder must exist and be writable")
        output_path = parent / output_path.name
        if output_path.suffix.lower() != ".iso":
            raise ValueError("Destination filename must end in .iso")
        if os.path.lexists(output_path):
            raise ValueError("Destination already exists; choose a new filename to avoid overwriting it")
        if output_path == Path(build.artifact_path).resolve():
            raise ValueError("Destination must differ from the build's original ISO")
        partial_path = Path(str(output_path) + ".part")
        if os.path.lexists(partial_path):
            raise ValueError("A partial output file already exists at the destination")

        if os.path.lexists(output_path) or os.path.lexists(partial_path):
            raise ValueError("Destination became occupied; choose a new filename")
        settings = self._raspberry_pi_boot_settings(arguments, raspberry_pi)
        system, cancelled, system_changes = self._prepare_live_iso_system_settings(arguments)
        if cancelled:
            return self._json({"status": "cancelled", "project": project.name,
                               "stage": build.stage_name, "output_path": str(output_path)})
        deployment = LiveIsoDeployment(project, build, LiveIsoTarget.ISO_FILE,
                                       raspberry_pi=settings, output_path=str(output_path), system=system)
        self._on_main_thread(lambda: deployment.start())
        return self._json({"status": "started", "project": project.name,
                           "stage": build.stage_name, "output_path": str(output_path),
                           "raspberry_pi": raspberry_pi,
                           "raspberry_pi_settings": self._summarize_raspberry_pi_boot_settings(settings),
                           "system_changes": system_changes})

    def _deploy_live_iso_to_disk(self, arguments):
        """Write a completed live ISO to a locally connected disk using the app's deploy engine."""
        import sys
        from .project_build import load_project_builds
        from .deploy_live_iso import (LiveIsoDeployment, LiveIsoTarget,
                                      is_live_iso_build, supports_raspberry_pi_iso)
        from .deploy_target import TargetFirmware, TargetMachine, machine_architecture, format_size
        from .local_disk_connection import LocalDiskConnection

        if not sys.platform.startswith("linux"):
            raise ValueError("Writing a live ISO to a local disk is available only on Linux")
        if self.user_input_callback is None:
            raise RuntimeError("Writing a live ISO requires in-chat disk selection and destructive confirmation")
        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("build_timestamp", "")).strip()
        build = next((item for item in load_project_builds(project) if item.timestamp == timestamp), None)
        if build is None or not is_live_iso_build(build):
            raise ValueError("Choose a completed live ISO from list_deployable_builds")
        raspberry_pi = arguments.get("raspberry_pi", False)
        if type(raspberry_pi) is not bool:
            raise ValueError("raspberry_pi must be a boolean")
        if raspberry_pi:
            if not supports_raspberry_pi_iso(project, build):
                raise ValueError("This live ISO does not have a supported Raspberry Pi kernel")
            from .deploy_image import image_supported_reason
            if reason := image_supported_reason(project):
                raise ValueError(reason)

        system, cancelled, system_changes = self._prepare_live_iso_system_settings(arguments)
        if cancelled:
            return self._json({"status": "cancelled", "project": project.name,
                               "stage": build.stage_name})

        architecture = project.get_architecture()
        target_architecture = machine_architecture(architecture.value if architecture else None) or ""
        iso_size = os.path.getsize(build.artifact_path)
        connection = LocalDiskConnection()
        error = connection.connect(cancel_event=self.cancel_event)
        if error:
            raise RuntimeError(error)
        started = False
        try:
            machine = TargetMachine.load_local(connection, target_architecture, TargetFirmware.OTHER)
            eligible = [disk for disk in machine.disks
                        if not disk.in_use and disk.size >= iso_size * 1.1]
            if not eligible:
                raise ValueError("No safe writable disk large enough for this live ISO was found")

            disk_path = str(arguments.get("disk", "")).strip()
            disk = next((item for item in eligible if item.path == disk_path), None)
            if disk_path and disk is None:
                raise ValueError("The requested disk is not an available, unmounted target")
            if disk is None:
                response = self.user_input_callback("Choose disk for live ISO",
                    f"Select a writable disk for {project.name} · {build.stage_name} · {timestamp}. "
                    f"The disk must have at least {format_size(int(iso_size * 1.1))} available capacity.", [
                        {"id": "disk", "label": "USB drive or SD card", "type": "select", "required": True,
                         "options": [{"value": item.path,
                                      "label": f"{item.path} · {format_size(item.size)} · "
                                               f"{item.model or item.transport or 'disk'}"
                                               + (" · removable" if item.removable else "")}
                                     for item in eligible]}])
                disk_path = str((response or {}).get("disk", "")).strip()
                disk = next((item for item in eligible if item.path == disk_path), None)
                if disk is None:
                    raise ValueError("Choose one of the available disks")

            pi_settings = self._raspberry_pi_boot_settings(arguments, raspberry_pi)
            boot_details = self._summarize_raspberry_pi_boot_settings(pi_settings)
            boot_mode = "Raspberry Pi firmware" if raspberry_pi else "as-built live ISO"
            if boot_details:
                boot_mode += " (" + ", ".join(boot_details) + ")"
            system_description = (". Also configure live system: " + ", ".join(system_changes)
                                 if system_changes else "")
            answer = self.user_input_callback("Erase and write disk?",
                f"This permanently erases every partition and file on {disk.path} "
                f"({disk.model or disk.transport or 'disk'}, {format_size(disk.size)}) and writes "
                f"{project.name} · {build.stage_name} · {timestamp} ({format_size(iso_size)}) as {boot_mode}"
                f"{system_description}. "
                "This cannot be undone.", [
                    {"id": "confirm", "label": f"Erase {disk.path} and write live ISO", "type": "boolean",
                     "placeholder": "Confirm erasing this disk", "required": True}])
            if not answer or not answer.get("confirm"):
                return self._json({"status": "cancelled", "project": project.name,
                                   "stage": build.stage_name, "disk": disk.path})

            # Re-read disk state after confirmation to catch removal or a newly mounted/system disk.
            refreshed = TargetMachine.load_local(connection, target_architecture, TargetFirmware.OTHER)
            current_disk = next((item for item in refreshed.disks if item.path == disk.path), None)
            if current_disk is None or current_disk.in_use or current_disk.size < iso_size * 1.1:
                raise ValueError("The selected disk changed or became unavailable after confirmation; nothing was written")
            deployment = LiveIsoDeployment(
                project, build, LiveIsoTarget.LOCAL_DISK, disk=current_disk,
                disk_connection=connection,
                raspberry_pi=pi_settings,
                system=system)
            self._on_main_thread(lambda: deployment.start())
            started = True
            return self._json({"status": "started", "project": project.name,
                               "stage": build.stage_name, "disk": current_disk.path,
                               "raspberry_pi": raspberry_pi,
                               "raspberry_pi_settings": boot_details,
                               "system_changes": system_changes})
        finally:
            if not started:
                connection.close()

    def _create_stage_image(self, arguments):
        from .project_build import load_project_builds
        from .deploy_installation import (DeployInstallation, DeploySystemSettings, DeployRepository,
                                          DeployUser, is_deployable, local_mount_point)
        from .deploy_target import (TargetFirmware, PartitionTable, PartitionPlan, TargetMachine,
                                    machine_architecture, default_image_layout)
        from .deploy_boot import Bootloader, Kernel, StageContents, default_bootloader
        from .deploy_system import (NetworkService, NetworkSettings, LocalizationSettings,
                                    WifiSettings, local_public_keys, default_network_service)
        from .deploy_image import image_supported_reason, image_connection, image_path, initial_image_size

        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("build_timestamp", "")).strip()
        build = next((item for item in load_project_builds(project) if item.timestamp == timestamp), None)
        if build is None or not is_deployable(project, build):
            raise ValueError("Choose a completed stage archive from list_deployable_builds")
        if reason := image_supported_reason(project):
            raise ValueError(reason)
        architecture = project.get_architecture()
        machine_arch = machine_architecture(architecture.value if architecture else None)
        if machine_arch is None:
            raise ValueError("The project's architecture cannot be used to create a disk image")

        firmware_key = str(arguments.get("firmware", "")).strip()
        firmware = next((item for item in TargetFirmware.options(machine_arch) if item.value == firmware_key), None)
        if firmware is None:
            raise ValueError("Choose firmware from list_options(stage_image_options)")
        table_key = str(arguments.get("partition_table", PartitionTable.GPT.value)).strip()
        table = next((item for item in PartitionTable if item.value == table_key), None)
        if table is None:
            raise ValueError("partition_table must be gpt or dos")

        contents = StageContents.scan(build.artifact_path)
        uefi = firmware == TargetFirmware.UEFI
        raspberry_pi = firmware == TargetFirmware.RASPBERRY_PI
        bootloader_key = str(arguments.get("bootloader", "")).strip()
        bootloader = next((item for item in Bootloader
                           if item.value == bootloader_key and item.supported(machine_arch, uefi, False)), None)
        if bootloader is None:
            raise ValueError("Choose a supported bootloader from list_options(stage_image_options)")
        kernels = [Kernel.DISTRIBUTION_BINARY, Kernel.DISTRIBUTION]
        if Kernel.RASPBERRY_PI.supported(machine_arch):
            kernels.insert(0 if raspberry_pi else 2, Kernel.RASPBERRY_PI)
        kernels = ([Kernel.STAGE] if contents.kernels else []) + kernels + [Kernel.NONE]
        kernel_key = str(arguments.get("kernel", "")).strip()
        kernel = next((item for item in kernels if item.value == kernel_key), None)
        if kernel is None:
            raise ValueError("Choose a kernel from list_options(stage_image_options)")

        network_options = [item for item in NetworkService if item.supported(contents.init)
                           and (item in contents.network_services or item in (NetworkService.DHCPCD, NetworkService.NONE))]
        network_key = str(arguments.get("network_service", "")).strip()
        network_service = next((item for item in network_options if item.value == network_key), None)
        if network_key and network_service is None:
            raise ValueError("Choose a network service from list_options(stage_image_options)")
        if network_service is None:
            network_service = default_network_service(contents.network_services, contents.init)

        static_network_data = arguments.get("static_network") or {}
        if not isinstance(static_network_data, dict):
            raise ValueError("static_network must be an object")
        static_network = NetworkSettings(
            service=network_service, static=bool(static_network_data),
            interface=str(static_network_data.get("interface", "")).strip(),
            address=str(static_network_data.get("address", "")).strip(),
            gateway=str(static_network_data.get("gateway", "")).strip(),
            dns=str(static_network_data.get("dns", "")).strip())
        if static_network.static and network_service == NetworkService.NONE:
            raise ValueError("Choose a network service to configure static networking")
        if error := static_network.error():
            raise ValueError(error)

        users_data = arguments.get("users", [])
        if not isinstance(users_data, list) or len(users_data) > 8:
            raise ValueError("users must be a list of at most eight accounts")
        user_specs = []
        user_names = set()
        allowed_groups = contents.groups
        for index, user in enumerate(users_data):
            if not isinstance(user, dict):
                raise ValueError("Each user must be an object")
            username = str(user.get("name", "")).strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}\$?", username):
                raise ValueError("User names must be valid lowercase Linux account names")
            if username in user_names or username in {"root", "bin", "daemon", "nobody"}:
                raise ValueError(f"User name {username!r} is duplicated or reserved")
            user_names.add(username)
            groups = user.get("groups", [])
            if not isinstance(groups, list) or any(not isinstance(group, str) for group in groups):
                raise ValueError(f"Groups for user {username!r} must be a list of names")
            if len(groups) != len(set(groups)) or any(group not in allowed_groups for group in groups):
                raise ValueError(f"Choose available groups for user {username!r} from stage_image_options")
            if type(user.get("password_required", False)) is not bool:
                raise ValueError(f"password_required for {username!r} must be boolean")
            full_name = str(user.get("full_name", "")).strip()
            if len(full_name) > 200 or any(character in full_name for character in "\r\n"):
                raise ValueError(f"Full name for {username!r} is too long or contains a line break")
            user_specs.append({"name": username, "full_name": str(user.get("full_name", "")).strip(),
                               "groups": groups, "password_required": user.get("password_required", False),
                               "password_id": f"user_password_{index}"})

        set_root_password = arguments.get("set_root_password", False)
        enable_ssh = arguments.get("enable_ssh", False)
        if type(set_root_password) is not bool or type(enable_ssh) is not bool:
            raise ValueError("set_root_password and enable_ssh must be booleans")
        if enable_ssh and not contents.ssh_installed:
            raise ValueError("SSH server is not installed in this stage, so it cannot be enabled")
        key_ids = arguments.get("ssh_key_ids", [])
        if not isinstance(key_ids, list) or any(not isinstance(item, str) for item in key_ids):
            raise ValueError("ssh_key_ids must be a list of IDs from stage_image_options")
        import hashlib
        available_keys = {hashlib.sha256((key.path + "\0" + key.key).encode()).hexdigest()[:20]: key
                          for key in local_public_keys()}
        if len(set(key_ids)) != len(key_ids) or any(key_id not in available_keys for key_id in key_ids):
            raise ValueError("Choose SSH keys from list_options(stage_image_options)")
        ssh_keys = [available_keys[key_id].key for key_id in key_ids]

        repository_key = str(arguments.get("repository", "")).strip()
        repository = next((item for item in DeployRepository if item.value == repository_key), None) if repository_key else None
        if repository_key and repository is None:
            raise ValueError("Choose a Gentoo repository from stage_image_options")
        repository_snapshot = None

        wifi_data = arguments.get("wifi")
        wifi = None
        if wifi_data is not None:
            if not isinstance(wifi_data, dict):
                raise ValueError("wifi must be an object")
            if network_service == NetworkService.NONE:
                raise ValueError("Choose a network service to configure Wi-Fi")
            if type(wifi_data.get("hidden", False)) is not bool or type(wifi_data.get("password_required", False)) is not bool:
                raise ValueError("Wi-Fi hidden and password_required values must be booleans")
            wifi = WifiSettings(ssid=str(wifi_data.get("ssid", "")).strip(),
                                hidden=wifi_data.get("hidden", False),
                                country=str(wifi_data.get("country", "")).strip())
            if error := wifi.error():
                raise ValueError(error)

        compress = arguments.get("compress", True)
        install_firmware = arguments.get("install_firmware", not contents.kernels and not contents.firmware
                                         and not raspberry_pi and not contents.firmware_conflict)
        if type(compress) is not bool or type(install_firmware) is not bool:
            raise ValueError("compress and install_firmware must be booleans")
        if install_firmware and (contents.firmware or contents.firmware_conflict or raspberry_pi):
            raise ValueError("Firmware package cannot be installed for this stage/firmware selection")
        needs_packages = (
            bootloader.needs_package and bootloader not in contents.bootloaders
            or bool(kernel.package)
            or bool(network_service.package and network_service not in contents.network_services)
            or install_firmware
            or bool(wifi and network_service not in (NetworkService.NETWORK_MANAGER, NetworkService.NONE)
                    and not contents.wpa_supplicant)
        )
        if repository is None:
            repository = DeployRepository.LATEST if needs_packages else DeployRepository.NONE
        elif repository == DeployRepository.NONE and needs_packages:
            raise ValueError("This configuration needs packages from Gentoo; choose latest or the build snapshot")
        if repository == DeployRepository.BUILD_SNAPSHOT:
            from .repositories import Repository
            snapshot_file = (((getattr(build, "details", None) or {}).get("inputs") or {})
                             .get("snapshot") or {}).get("file")
            snapshot = next((item for item in Repository.Snapshot.value
                             if item.filename == snapshot_file), None) if snapshot_file else None
            if snapshot is None:
                raise ValueError("The snapshot used by this build is no longer available")
            repository_snapshot = snapshot.file_path()

        hostname = str(arguments.get("hostname", "gentoo")).strip() or "gentoo"
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?", hostname):
            raise ValueError("hostname must be a valid DNS-style name")
        locales = arguments.get("locales", [])
        if not isinstance(locales, list) or any(not isinstance(item, str) for item in locales):
            raise ValueError("locales must be a list of locale names")
        localization = LocalizationSettings(timezone=str(arguments.get("timezone", "")).strip(),
                                            locales=locales,
                                            keymap=str(arguments.get("keymap", "")).strip())
        if error := localization.error():
            raise ValueError(error)

        requested_path = str(arguments.get("output_path", "")).strip()
        if not requested_path:
            raise ValueError("Choose a destination image path")
        output_path = Path(requested_path).expanduser()
        if not output_path.is_absolute():
            raise ValueError("Destination path must be absolute")
        parent = output_path.parent.resolve()
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise ValueError("Destination folder must exist and be writable")
        output_path = parent / output_path.name
        expected_suffix = ".img.xz" if compress else ".img"
        if not str(output_path).lower().endswith(expected_suffix):
            raise ValueError(f"Destination filename must end in {expected_suffix}")
        if os.path.lexists(output_path) or os.path.lexists(str(output_path) + ".part"):
            raise ValueError("Destination or its partial-output file already exists; choose a new filename")

        connection = image_connection(project)
        temp_image = image_path(build)
        size = os.path.getsize(build.artifact_path)
        machine = TargetMachine.for_image(connection, machine_arch, firmware, temp_image,
                                          initial_image_size(size))
        custom_layout = self._partition_layout_from_arguments(arguments.get("partition_layout"))
        layout = (custom_layout if custom_layout is not None else
                  default_image_layout(machine.uefi, machine.architecture, table,
                                       raspberry_pi=machine.raspberry_pi))
        plan = PartitionPlan(disk=machine.disks[0], uefi=machine.uefi,
                             layout=layout,
                             table=table, local=True, raspberry_pi=machine.raspberry_pi,
                             image_path=temp_image, compress_image=compress)
        if error := plan.error():
            raise ValueError(f"Invalid image partition plan: {error}")
        settings = DeploySystemSettings(
            hostname=hostname,
            network=static_network,
            localization=localization,
            bootloader=bootloader,
            kernel=kernel,
            install_firmware=install_firmware,
            enable_ssh=enable_ssh,
            ssh_keys=ssh_keys,
            repository=repository,
            repository_snapshot=repository_snapshot,
            wifi=wifi,
        )
        secret_fields = []
        if set_root_password:
            secret_fields.append({"id": "root_password", "label": "Root password", "type": "secret",
                                  "sensitive": True, "placeholder": "Enter root password", "required": True})
        for user in user_specs:
            if user["password_required"]:
                secret_fields.append({"id": user["password_id"], "label": f"Password for {user['name']}",
                                      "type": "secret", "sensitive": True,
                                      "placeholder": "Enter account password", "required": True})
        if wifi and wifi_data.get("password_required", False):
            secret_fields.append({"id": "wifi_password", "label": "Wi-Fi password", "type": "secret",
                                  "sensitive": True, "placeholder": "Enter Wi-Fi password", "required": True})
        secret_answers = {}
        if secret_fields:
            if self.user_input_callback is None:
                raise RuntimeError("Secure in-chat password entry is unavailable")
            secret_answers = self.user_input_callback("Enter deployment passwords",
                "These password fields are handled locally and are not sent to the assistant model.", secret_fields)
            if not secret_answers:
                return self._json({"status": "cancelled", "project": project.name,
                                   "stage": build.stage_name, "output_path": str(output_path)})
            for password in secret_answers.values():
                if not isinstance(password, str) or not password or any(character in password for character in "\r\n:"):
                    raise ValueError("Passwords must be non-empty and cannot contain a colon or line break")
        settings.root_password = secret_answers.get("root_password")
        settings.users = [DeployUser(name=user["name"], full_name=user["full_name"], groups=user["groups"],
                                     password=secret_answers.get(user["password_id"], ""))
                          for user in user_specs]
        if wifi is not None and wifi_data.get("password_required", False):
            wifi.password = secret_answers.get("wifi_password", "")
            if error := wifi.error():
                raise ValueError(error)
        if os.path.lexists(output_path) or os.path.lexists(str(output_path) + ".part"):
            raise ValueError("Destination became occupied; choose a new filename")

        plan.root = local_mount_point()
        installation = DeployInstallation(build=build, project_name=project.name, connection=connection,
                                           machine=machine, plan=plan, settings=settings, contents=contents)
        installation.project_id = project.id
        installation.output_path = str(output_path)
        self._on_main_thread(lambda: installation.start())
        return self._json({"status": "started", "project": project.name,
                           "stage": build.stage_name, "output_path": str(output_path),
                           "firmware": firmware.value, "bootloader": bootloader.value,
                           "kernel": kernel.value, "network_service": network_service.value,
                           "compress": compress})

    def _deploy_stage_to_target(self, arguments):
        """Install a stage on a selected physical disk using the app's checked deploy engine."""
        import sys
        from .project_build import load_project_builds
        from .deploy_installation import (DeployInstallation, DeploySystemSettings, DeployRepository,
                                          DeployUser, is_deployable, local_mount_point)
        from .deploy_target import (TargetFirmware, TargetMachine, PartitionTable, PartitionPlan,
                                    default_layout, machine_architecture, architecture_supported, format_size)
        from .deploy_boot import Bootloader, Kernel, StageContents, default_bootloader
        from .deploy_system import (NetworkService, NetworkSettings, LocalizationSettings, WifiSettings,
                                    default_network_service, local_public_keys)

        if self.user_input_callback is None:
            raise RuntimeError("Direct deployment requires in-chat forms for credentials and destructive confirmation")
        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("build_timestamp", "")).strip()
        build = next((item for item in load_project_builds(project) if item.timestamp == timestamp), None)
        if build is None or not is_deployable(project, build):
            raise ValueError("Choose a completed stage archive from list_deployable_builds")
        from .deploy_live_iso import is_live_iso_build
        if is_live_iso_build(build):
            raise ValueError("A live ISO cannot be installed as a stage archive; use create_live_iso")
        arch = project.get_architecture()
        build_arch = arch.value if arch else None
        expected_machine_arch = machine_architecture(build_arch)
        if expected_machine_arch is None:
            raise ValueError("The project's architecture cannot be deployed")

        def ask(title, message, fields):
            response = self.user_input_callback(title, message, fields)
            if not response:
                raise ValueError("Deployment canceled by the user")
            return response

        method = str(arguments.get("target_method", "")).strip()
        if method not in ("local_disk", "ssh"):
            response = ask("Choose deployment target", "Install this build on a physical disk or a remote machine?", [
                {"id": "target_method", "label": "Target", "type": "select", "required": True,
                 "options": [{"value": "local_disk", "label": "Disk connected to this computer (Linux only)"},
                             {"value": "ssh", "label": "Machine through SSH"}]}])
            method = str(response.get("target_method", ""))

        connection = None
        started = False
        try:
            if method == "local_disk":
                if not sys.platform.startswith("linux"):
                    raise ValueError("Direct local-disk deployment is only available on Linux; create a disk image instead")
                firmware_options = TargetFirmware.options(expected_machine_arch)
                firmware_key = str(arguments.get("firmware", "")).strip()
                firmware = next((item for item in firmware_options if item.value == firmware_key), None)
                if firmware is None:
                    response = ask("Choose target firmware",
                                   "Select the firmware of the machine that will boot from this disk.", [
                        {"id": "firmware", "label": "Firmware", "type": "select", "required": True,
                         "options": [{"value": item.value, "label": item.display_name()} for item in firmware_options]}])
                    firmware_key = str(response.get("firmware", ""))
                    firmware = next((item for item in firmware_options if item.value == firmware_key), None)
                if firmware is None:
                    raise ValueError("Choose a supported firmware option")
                from .local_disk_connection import LocalDiskConnection
                connection = LocalDiskConnection()
                error = connection.connect(cancel_event=self.cancel_event)
                if error:
                    raise RuntimeError(error)
                machine = TargetMachine.load_local(connection, expected_machine_arch, firmware)
            elif method == "ssh":
                host = str(arguments.get("host", "")).strip()
                port = arguments.get("port", 22)
                user = str(arguments.get("user", "root")).strip() or "root"
                if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                    raise ValueError("SSH port must be an integer from 1 to 65535")
                response = ask("Connect through SSH",
                               "Enter the target connection details. Password is optional when SSH key authentication is configured; it is handled locally and never sent to the model.", [
                    {"id": "host", "label": "Host", "type": "text", "required": True,
                     "default": host, "placeholder": "hostname or IP address"},
                    {"id": "port", "label": "Port", "type": "text", "required": True,
                     "default": str(port)},
                    {"id": "user", "label": "User", "type": "text", "required": True,
                     "default": user},
                    {"id": "password", "label": "SSH password (leave empty for key authentication)",
                     "type": "secret", "sensitive": True, "required": False}])
                host = str(response.get("host", "")).strip()
                user = str(response.get("user", "root")).strip() or "root"
                try:
                    port = int(response.get("port", 22))
                except (TypeError, ValueError):
                    raise ValueError("SSH port must be an integer from 1 to 65535")
                if (not host or host.startswith("-") or any(char.isspace() for char in host)
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*\$?", user)
                        or not 1 <= port <= 65535):
                    raise ValueError("Enter a valid SSH host, username, and port")
                password = response.get("password") or None
                if password and any(character in password for character in "\r\n"):
                    raise ValueError("SSH passwords cannot contain line breaks")
                from .ssh_connection import SSHConnection
                connection = SSHConnection(host=host, port=port, user=user)
                error = connection.connect(password)
                if error:
                    raise RuntimeError(f"SSH connection failed: {error}")
                machine = TargetMachine.load(connection)
            else:
                raise ValueError("Choose local_disk or ssh as the deployment target")
            if method == "ssh" and str(arguments.get("firmware", "")).strip():
                raise ValueError("Omit firmware for SSH targets; their firmware is detected automatically")

            if architecture_supported(build_arch, machine.architecture) is False:
                raise ValueError(f"Build architecture {build_arch} cannot run on target architecture {machine.architecture}")
            if not machine.disks:
                raise ValueError("No writable target disks were found")
            safe_disks = [disk for disk in machine.disks if not disk.in_use]
            if not safe_disks:
                reasons = "; ".join(f"{disk.path}: {disk.in_use_reason or 'in use'}" for disk in machine.disks)
                raise ValueError(f"No safe target disk is available ({reasons})")
            disk_path = str(arguments.get("disk", "")).strip()
            disk = next((item for item in safe_disks if item.path == disk_path), None)
            if disk is None:
                response = ask("Choose target disk", "Every partition and file on the selected disk will be erased.", [
                    {"id": "disk", "label": "Disk to erase", "type": "select", "required": True,
                     "options": [{"value": item.path,
                                  "label": f"{item.path} · {format_size(item.size)} · {item.model or item.transport or 'disk'}"
                                  + (" · removable" if item.removable else "")}
                                 for item in safe_disks]}])
                disk_path = str(response.get("disk", ""))
                disk = next((item for item in safe_disks if item.path == disk_path), None)
            if disk is None:
                raise ValueError("Choose an available disk listed by the target")

            contents = StageContents.scan(build.artifact_path)
            default_table = PartitionTable.MBR if machine.raspberry_pi else PartitionTable.GPT
            table_key = str(arguments.get("partition_table", default_table.value)).strip()
            table = next((item for item in PartitionTable if item.value == table_key), None)
            if table is None:
                raise ValueError("partition_table must be gpt or dos")
            custom_layout = self._partition_layout_from_arguments(arguments.get("partition_layout"))
            layout = (custom_layout if custom_layout is not None else
                      default_layout(disk, machine.uefi, machine.memory, machine.architecture,
                                     table, raspberry_pi=machine.raspberry_pi))
            plan = PartitionPlan(
                disk=disk, uefi=machine.uefi,
                layout=layout,
                table=table, local=machine.local, raspberry_pi=machine.raspberry_pi)
            if error := plan.error():
                raise ValueError(f"Partition layout is invalid: {error}")
            bootloader_options = [item for item in Bootloader
                                  if item.supported(machine.architecture, machine.uefi, machine.local)]
            bootloader_default = default_bootloader(contents, machine.architecture, machine.uefi,
                                                    machine.local, machine.raspberry_pi)
            bootloader_key = str(arguments.get("bootloader", "")).strip()
            bootloader = next((item for item in bootloader_options if item.value == bootloader_key), None)
            if bootloader_key and bootloader is None:
                raise ValueError("Choose a bootloader supported by the target")
            bootloader = bootloader or bootloader_default
            if bootloader not in bootloader_options:
                raise ValueError("No supported bootloader exists for this target")

            kernel_options = [Kernel.DISTRIBUTION_BINARY, Kernel.DISTRIBUTION]
            if Kernel.RASPBERRY_PI.supported(machine.architecture):
                kernel_options.insert(0 if machine.raspberry_pi else 2, Kernel.RASPBERRY_PI)
            if contents.kernels:
                kernel_options.insert(0, Kernel.STAGE)
            kernel_options.append(Kernel.NONE)
            kernel_key = str(arguments.get("kernel", "")).strip()
            kernel = next((item for item in kernel_options if item.value == kernel_key), None)
            if kernel_key and kernel is None:
                raise ValueError("Choose a kernel supported by the target from stage_image_options")
            kernel = kernel or (Kernel.STAGE if contents.kernels else
                                Kernel.RASPBERRY_PI if machine.raspberry_pi else Kernel.DISTRIBUTION_BINARY)

            network_options = [item for item in NetworkService if item.supported(contents.init)
                               and (item in contents.network_services or item in (NetworkService.DHCPCD, NetworkService.NONE))]
            network_key = str(arguments.get("network_service", "")).strip()
            network_service = next((item for item in network_options if item.value == network_key), None)
            if network_key and network_service is None:
                raise ValueError("Choose a network service from list_options(stage_image_options)")
            network_service = network_service or default_network_service(contents.network_services, contents.init)
            static_data = arguments.get("static_network") or {}
            if not isinstance(static_data, dict):
                raise ValueError("static_network must be an object")
            network = NetworkSettings(service=network_service, static=bool(static_data),
                                      interface=str(static_data.get("interface", "")).strip(),
                                      address=str(static_data.get("address", "")).strip(),
                                      gateway=str(static_data.get("gateway", "")).strip(),
                                      dns=str(static_data.get("dns", "")).strip())
            if network.static and network_service == NetworkService.NONE:
                raise ValueError("Choose a network service to configure static networking")
            if error := network.error():
                raise ValueError(error)

            hostname = str(arguments.get("hostname", "gentoo")).strip() or "gentoo"
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?", hostname):
                raise ValueError("hostname must be a valid DNS-style name")
            locales = arguments.get("locales", [])
            if not isinstance(locales, list) or any(not isinstance(item, str) for item in locales):
                raise ValueError("locales must be a list of locale names")
            localization = LocalizationSettings(timezone=str(arguments.get("timezone", "")).strip(),
                                                locales=locales,
                                                keymap=str(arguments.get("keymap", "")).strip())
            if error := localization.error():
                raise ValueError(error)

            users_data = arguments.get("users", [])
            if not isinstance(users_data, list) or len(users_data) > 8:
                raise ValueError("users must be a list of at most eight accounts")
            user_specs, user_names = [], set()
            for index, user_data in enumerate(users_data):
                if not isinstance(user_data, dict):
                    raise ValueError("Each user must be an object")
                username = str(user_data.get("name", "")).strip()
                if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}\$?", username):
                    raise ValueError("User names must be valid lowercase Linux account names")
                if username in user_names or username in {"root", "bin", "daemon", "nobody"}:
                    raise ValueError(f"User name {username!r} is duplicated or reserved")
                user_names.add(username)
                groups = user_data.get("groups", [])
                if (not isinstance(groups, list) or any(not isinstance(group, str) for group in groups)
                        or len(groups) != len(set(groups)) or any(group not in contents.groups for group in groups)):
                    raise ValueError(f"Choose available groups for user {username!r} from stage_image_options")
                password_required = user_data.get("password_required", False)
                if type(password_required) is not bool:
                    raise ValueError(f"password_required for {username!r} must be boolean")
                full_name = str(user_data.get("full_name", "")).strip()
                if len(full_name) > 200 or any(char in full_name for char in "\r\n"):
                    raise ValueError(f"Full name for {username!r} is too long or contains a line break")
                user_specs.append({"name": username, "full_name": full_name, "groups": groups,
                                   "password_required": password_required, "password_id": f"user_password_{index}"})

            set_root_password = arguments.get("set_root_password", False)
            enable_ssh = arguments.get("enable_ssh", False)
            reboot = arguments.get("reboot", False)
            install_firmware = arguments.get("install_firmware", not contents.kernels and not contents.firmware
                                             and not machine.raspberry_pi and not contents.firmware_conflict)
            if any(type(value) is not bool for value in (set_root_password, enable_ssh, reboot, install_firmware)):
                raise ValueError("set_root_password, enable_ssh, reboot, and install_firmware must be booleans")
            if reboot and machine.local:
                raise ValueError("reboot is available only for an SSH deployment target")
            if enable_ssh and not contents.ssh_installed:
                raise ValueError("SSH server is not installed in this stage, so it cannot be enabled")
            if install_firmware and (contents.firmware or contents.firmware_conflict or machine.raspberry_pi):
                raise ValueError("Firmware package cannot be installed for this stage/target")

            import hashlib
            key_ids = arguments.get("ssh_key_ids", [])
            if not isinstance(key_ids, list) or any(not isinstance(key_id, str) for key_id in key_ids):
                raise ValueError("ssh_key_ids must be a list of IDs from stage_image_options")
            available_keys = {hashlib.sha256((key.path + "\0" + key.key).encode()).hexdigest()[:20]: key
                              for key in local_public_keys()}
            if len(set(key_ids)) != len(key_ids) or any(key_id not in available_keys for key_id in key_ids):
                raise ValueError("Choose SSH keys from list_options(stage_image_options)")
            ssh_keys = [available_keys[key_id].key for key_id in key_ids]

            wifi_data = arguments.get("wifi")
            wifi = None
            if wifi_data is not None:
                if not isinstance(wifi_data, dict):
                    raise ValueError("wifi must be an object")
                if network_service == NetworkService.NONE:
                    raise ValueError("Choose a network service to configure Wi-Fi")
                if type(wifi_data.get("hidden", False)) is not bool or type(wifi_data.get("password_required", False)) is not bool:
                    raise ValueError("Wi-Fi hidden and password_required values must be booleans")
                wifi = WifiSettings(ssid=str(wifi_data.get("ssid", "")).strip(),
                                    hidden=wifi_data.get("hidden", False),
                                    country=str(wifi_data.get("country", "")).strip())
                if error := wifi.error():
                    raise ValueError(error)

            repository_key = str(arguments.get("repository", "")).strip()
            repository = next((item for item in DeployRepository if item.value == repository_key), None)
            if repository_key and repository is None:
                raise ValueError("Choose a repository from list_options(stage_image_options)")
            needs_packages = (bootloader.needs_package and bootloader not in contents.bootloaders
                              or bool(kernel.package)
                              or bool(network_service.package and network_service not in contents.network_services)
                              or install_firmware
                              or bool(wifi and network_service not in (NetworkService.NETWORK_MANAGER, NetworkService.NONE)
                                      and not contents.wpa_supplicant))
            if repository is None:
                repository = DeployRepository.LATEST if needs_packages else DeployRepository.NONE
            elif repository == DeployRepository.NONE and needs_packages:
                raise ValueError("This configuration needs packages from Gentoo; choose latest or the build snapshot")
            repository_snapshot = None
            if repository == DeployRepository.BUILD_SNAPSHOT:
                from .repositories import Repository
                snapshot_file = (((getattr(build, "details", None) or {}).get("inputs") or {})
                                 .get("snapshot") or {}).get("file")
                snapshot = next((item for item in Repository.Snapshot.value
                                 if item.filename == snapshot_file), None) if snapshot_file else None
                if snapshot is None:
                    raise ValueError("The snapshot used by this build is no longer available")
                repository_snapshot = snapshot.file_path()

            settings = DeploySystemSettings(
                hostname=hostname, network=network, localization=localization,
                bootloader=bootloader, kernel=kernel, install_firmware=install_firmware,
                enable_ssh=enable_ssh, ssh_keys=ssh_keys, wifi=wifi, repository=repository,
                repository_snapshot=repository_snapshot, reboot=reboot)
            if machine.local:
                plan.root = local_mount_point()
            network_summary = f"{network_service.display_name} · DHCP"
            if network.static:
                network_summary = f"{network_service.display_name} · static {network.address} on {network.interface}"
                if network.gateway:
                    network_summary += f" · gateway {network.gateway}"
                if network.dns:
                    network_summary += f" · DNS {network.dns}"
            detail = (f"Build: {project.name} · {build.stage_name} · {timestamp}\n"
                      f"Target: {machine.hostname or connection.host} ({machine.description})\n"
                      f"Disk to erase: {disk.path} · {format_size(disk.size)} · {disk.model or disk.transport or 'disk'}\n"
                      f"Layout: {table.display_name}, " + ", ".join(
                          f"{part.mount_point or part.type.value} {format_size(plan.size_of(part))}"
                          for part in plan.layout) + "\n"
                      f"Bootloader: {bootloader.display_name} · Kernel: {kernel.display_name}\n"
                      f"Network: {network_summary}"
                      f" · Hostname: {hostname} · Timezone: {localization.timezone or 'unchanged'}\n"
                      f"Locales: {', '.join(localization.locales) or 'unchanged'} · Keymap: {localization.keymap or 'unchanged'}\n"
                      f"Users: {', '.join(user['name'] + (' (' + ', '.join(user['groups']) + ')' if user['groups'] else '') for user in user_specs) or 'none'} · "
                      f"SSH: {'enabled' if enable_ssh else 'unchanged'} · SSH key IDs: {', '.join(key_ids) or 'none'} · "
                      f"Root password: {'set' if set_root_password else 'unchanged'}\n"
                      f"Wi-Fi: {wifi.ssid + (' (hidden)' if wifi.hidden else '') + (' · ' + wifi.country if wifi.country else '') if wifi else 'not configured'} · "
                      f"Repository: {repository.value} · Firmware package: {'install' if install_firmware else 'unchanged'} · "
                      f"Reboot after deployment: {'yes' if reboot else 'no'}\n"
                      "All existing data and partitions on this disk will be destroyed.")
            confirmation = ask("Erase disk and install stage?", detail, [
                {"id": "confirm", "label": f"Erase {disk.path} and install", "type": "boolean",
                 "required": True}])
            if not confirmation.get("confirm"):
                return self._json({"status": "cancelled", "project": project.name,
                                   "stage": build.stage_name, "disk": disk.path})

            secret_fields = []
            if set_root_password:
                secret_fields.append({"id": "root_password", "label": "Root password", "type": "secret",
                                      "sensitive": True, "placeholder": "Enter root password", "required": True})
            for user_spec in user_specs:
                if user_spec["password_required"]:
                    secret_fields.append({"id": user_spec["password_id"],
                                          "label": f"Password for {user_spec['name']}",
                                          "type": "secret", "sensitive": True,
                                          "placeholder": "Enter account password", "required": True})
            if wifi and wifi_data.get("password_required", False):
                secret_fields.append({"id": "wifi_password", "label": "Wi-Fi password", "type": "secret",
                                      "sensitive": True, "placeholder": "Enter Wi-Fi password", "required": True})
            secret_answers = {}
            if secret_fields:
                secret_answers = ask("Enter deployment passwords",
                    "These password fields are handled locally and are not sent to the assistant model.",
                    secret_fields)
                for password_value in secret_answers.values():
                    if (not isinstance(password_value, str) or not password_value
                            or any(char in password_value for char in "\r\n:")):
                        raise ValueError("Passwords must be non-empty and cannot contain a colon or line break")
            settings.root_password = secret_answers.get("root_password")
            settings.users = [
                DeployUser(name=user_spec["name"], full_name=user_spec["full_name"],
                           groups=user_spec["groups"],
                           password=secret_answers.get(user_spec["password_id"], ""))
                for user_spec in user_specs
            ]
            if wifi is not None and wifi_data.get("password_required", False):
                wifi.password = secret_answers.get("wifi_password", "")
                if error := wifi.error():
                    raise ValueError(error)

            installation = DeployInstallation(
                build=build, project_name=project.name, connection=connection,
                machine=machine, plan=plan, settings=settings, contents=contents)
            installation.project_id = project.id
            self._on_main_thread(lambda: installation.start())
            started = True
            return self._json({"status": "started", "project": project.name,
                               "stage": build.stage_name, "target": machine.hostname or connection.host,
                               "disk": disk.path, "layout": table.value,
                               "bootloader": bootloader.value, "kernel": kernel.value,
                               "network_service": network_service.value,
                               "repository": repository.value, "hostname": hostname,
                               "users": [user["name"] for user in user_specs],
                               "reboot": reboot})
        finally:
            if connection is not None and not started:
                connection.close()

    def _list_build_files(self, arguments):
        from .project_build import load_project_builds
        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("timestamp", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", timestamp):
            raise ValueError("Pass the exact build timestamp returned by list_builds")
        build = next((item for item in load_project_builds(project)
                      if item.timestamp == timestamp), None)
        if build is None or not build.path or not os.path.isdir(build.path):
            raise ValueError("No stored build folder matches that project and timestamp")

        root = Path(build.path).resolve(strict=True)
        relative = str(arguments.get("path", "") or "")
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(part in ("..", ".git") for part in relative_path.parts):
            raise ValueError("path must stay inside the selected build folder")
        start = root
        for part in relative_path.parts:
            if part in ("", "."):
                continue
            start = start / part
            if start.is_symlink():
                raise ValueError("Build file paths must not traverse symlinks")
        start = start.resolve(strict=True)
        if not start.is_relative_to(root) or not start.is_dir():
            raise ValueError("path must name a directory inside the selected build folder")

        recursive = arguments.get("recursive", False)
        if not isinstance(recursive, bool):
            raise ValueError("recursive must be true or false")
        try:
            limit = int(arguments.get("limit", 200))
            offset = int(arguments.get("offset", 0))
        except (TypeError, ValueError) as error:
            raise ValueError("offset and limit must be integers") from error
        if not 1 <= limit <= 500 or not 0 <= offset <= 9_500:
            raise ValueError("offset must be 0–9500 and limit must be 1–500")

        entries = []
        scanned = 0
        has_more = False
        scan_limit = 10_000
        for current, directories, files in os.walk(start, topdown=True, followlinks=False):
            directories[:] = sorted(
                name for name in directories
                if name != ".git" and not os.path.islink(os.path.join(current, name))
            )
            names = [(name, True) for name in directories]
            names += [(name, False) for name in files if name != ".git"]
            names.sort(key=lambda item: (not item[1], item[0].casefold(), item[0]))
            for name, is_directory in names:
                full_path = os.path.join(current, name)
                if os.path.islink(full_path):
                    continue
                scanned += 1
                if scanned > offset + limit:
                    has_more = True
                    break
                if scanned > offset:
                    try:
                        entry = {"path": os.path.relpath(full_path, root),
                                 "type": "directory" if is_directory else "file"}
                        if not is_directory:
                            entry["size_bytes"] = os.path.getsize(full_path)
                        entries.append(entry)
                    except OSError:
                        continue
                if scanned >= scan_limit:
                    has_more = bool(directories or files)
                    break
            if has_more or (scanned >= scan_limit) or not recursive:
                break
        next_offset = (offset + len(entries)
                       if has_more and offset + len(entries) < scan_limit else None)
        return self._json({"project": project.name, "stage": build.stage_name,
                           "timestamp": timestamp, "path": relative or ".",
                           "recursive": recursive, "offset": offset, "limit": limit,
                           "entries": entries, "returned_count": len(entries),
                           "next_offset": next_offset,
                           "scan_limit_reached": scanned >= scan_limit})

    def _read_build_file(self, arguments):
        from .project_build import load_project_builds
        project = self._project(arguments.get("project", ""))
        timestamp = str(arguments.get("timestamp", ""))
        build = next((item for item in load_project_builds(project) if item.timestamp == timestamp), None)
        if build is None or not build.path:
            raise ValueError("No stored build matches that timestamp")
        relative = str(arguments.get("path", ""))
        content = self._read_text(relative, build.path)
        return f"Read {project.name} build {build.stage_name} ({build.timestamp})/{relative}\n{content}"

    def _list_repository_packages(self, arguments):
        kind = str(arguments.get("repository_type", "")).strip().lower()
        if kind not in ("snapshot", "overlay"):
            raise ValueError("repository_type must be 'snapshot' or 'overlay'")
        key = str(arguments.get("repository", "")).strip()
        if not key:
            raise ValueError("Pass a repository name or ID")
        repository = self._find_any_object(kind, key)
        if kind == "snapshot" and not os.path.isfile(repository.file_path()):
            raise ValueError(f"Snapshot file is unavailable: {repository.name}")
        packages = repository.load_ebuilds()
        query = str(arguments.get("query", "")).strip()
        if query:
            normalized_query = query.casefold()
            filtered = {}
            for category, category_packages in packages.items():
                if "/" in normalized_query:
                    matches = {name: versions for name, versions in category_packages.items()
                               if normalized_query in f"{category}/{name}".casefold()}
                elif normalized_query in category.casefold():
                    matches = category_packages
                else:
                    matches = {name: versions for name, versions in category_packages.items()
                               if normalized_query in name.casefold()}
                if matches:
                    filtered[category] = matches
            packages = filtered
        try:
            limit = int(arguments.get("limit", 200))
            offset = int(arguments.get("offset", 0))
        except (TypeError, ValueError) as error:
            raise ValueError("offset and limit must be whole numbers") from error
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        include_versions = arguments.get("include_versions", True)
        if not isinstance(include_versions, bool):
            raise ValueError("include_versions must be true or false")

        package_rows = [(category, name, versions)
                        for category, category_packages in packages.items()
                        for name, versions in category_packages.items()]
        total = len(package_rows)
        selected = package_rows[offset:offset + limit]
        next_offset = offset + len(selected) if offset + len(selected) < total else None
        categories = {}
        for category, name, versions in selected:
            row = {"name": name}
            if include_versions:
                row["versions"] = versions
            categories.setdefault(category, []).append(row)
        return self._json({"repository_type": kind, "repository": repository.name,
                           "query": query or None, "category_count": len(categories),
                           "package_count": total, "offset": offset,
                           "returned_count": len(selected), "next_offset": next_offset,
                           "truncated": next_offset is not None, "categories": categories,
                           "versions_included": include_versions})

    def _package_cache_stats(self, arguments):
        from .repositories import Repository
        key = str(arguments.get("packages_directory", "")).strip()
        directory = next((item for item in Repository.PackagesDirectory.value
                          if item.name == key or str(item.id) == key), None)
        if directory is None:
            raise ValueError(f"No binary package cache named or identified {key!r}")
        cache_path = directory.directory_path()
        if os.path.islink(cache_path):
            raise ValueError("Refusing to inspect a linked package cache folder")
        try:
            root = Path(cache_path).resolve(strict=True)
        except OSError as error:
            raise ValueError(f"The package cache folder is unavailable: {error}") from error
        if not root.is_dir():
            raise ValueError("The selected package cache is not a directory")
        wanted = str(arguments.get("release_type", "")).strip()
        include_packages = bool(arguments.get("include_packages", False))
        try:
            limit = max(1, min(int(arguments.get("limit", 100)), 1000))
        except (TypeError, ValueError):
            limit = 100

        indexes = []
        all_cpvs = set()
        size_bytes = directory.size()

        def folder_size(path):
            total = 0
            for current, subdirectories, filenames in os.walk(path, followlinks=False):
                subdirectories[:] = [name for name in subdirectories
                                     if not os.path.islink(os.path.join(current, name))]
                for name in filenames:
                    try:
                        total += os.lstat(os.path.join(current, name)).st_size
                    except OSError:
                        continue
            return total

        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories
                              if not name.startswith(".") and not os.path.islink(os.path.join(current, name))]
            if "Packages" not in files:
                continue
            index_path = Path(current) / "Packages"
            if index_path.is_symlink() or not index_path.is_file():
                continue
            relative = index_path.parent.relative_to(root).as_posix()
            if wanted and relative != wanted and Path(relative).name != wanted:
                continue
            cpvs = []
            try:
                with index_path.open(encoding="utf-8", errors="replace") as file:
                    for line in file:
                        if line.startswith("CPV:"):
                            cpv = line.partition(":")[2].strip()
                            if cpv:
                                cpvs.append(cpv)
            except OSError as error:
                indexes.append({"release_type": relative, "index": str(index_path),
                                "error": str(error), "artifact_count": None, "unique_cpv_count": None})
                continue
            all_cpvs.update(cpvs)
            indexes.append({"release_type": relative or ".", "index": str(index_path),
                            "size_bytes": folder_size(current),
                            "artifact_count": len(cpvs), "unique_cpv_count": len(set(cpvs)),
                            "packages": sorted(cpvs)[:limit] if include_packages else None,
                            "package_list_truncated": include_packages and len(cpvs) > limit})

        if wanted and not indexes:
            raise ValueError(f"No Portage Packages index found for release type {wanted!r}")
        known_counts = [item["artifact_count"] for item in indexes if item.get("artifact_count") is not None]
        complete = bool(indexes) and len(known_counts) == len(indexes)
        return self._json({
            "packages_directory": directory.name,
            "architecture": directory.architecture.value if directory.architecture else None,
            "cpu_flags": directory.cpu_flags,
            "size_bytes": size_bytes,
            "projects_using_cache": [project.name for project in directory.projects],
            "index_count": len(indexes),
            "artifact_count": sum(known_counts) if complete else None,
            "unique_cpv_count": len(all_cpvs) if complete else None,
            "count_complete": complete,
            "count_note": "artifact_count counts CPV records in Portage Packages indexes; duplicate CPVs in different indexes count as separate cached artifacts. It does not count historical compile attempts.",
            "indexes": indexes,
            "note": None if indexes else "No Portage Packages index was found. The cache may be empty, or package index files may not be present locally.",
        })

    def _list_library_storage(self, arguments):
        from .library_storage import scan_library_storage
        from .repository import library_directory
        section = str(arguments.get("section", "overview")).strip().lower()
        try:
            limit = max(1, min(int(arguments.get("limit", 100)), 200))
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(arguments.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        storage = scan_library_storage(library_directory())
        if storage.get("error"):
            raise RuntimeError(f"Could not scan the Library: {storage['error']}")
        if section == "overview":
            items = storage["sections"]
            page = items[offset:offset + limit]
            return self._json({"total_size": storage["total_size"], "total_sections": len(items),
                "shown": len(page), "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(items) else None,
                "sections": [
                {"name": item["name"], "size": item["size"], "description": item["description"]}
                for item in page]})
        keys = {"cache": ("cache_items", "Cache"),
                "temporary": ("temporary_items", "Temporary"),
                "packages": ("binpkg_items", "Packages"),
                "snapshots": ("snapshot_items", "Snapshots"),
                "projects": ("project_items", "Projects"),
                "builds": ("build_items", "Builds")}
        if section not in keys:
            raise ValueError("Choose overview, cache, temporary, packages, snapshots, projects, or builds")
        key, label = keys[section]
        items = storage[key]
        allowed_fields = ("name", "relative_path", "category", "size", "details", "description",
                          "usage", "projects", "in_use", "in_use_reason", "metadata", "status",
                          "stage", "timestamp", "build_count")
        page = items[offset:offset + limit]
        rows = [{name: item[name] for name in allowed_fields if name in item}
                for item in page]
        return self._json({"section": label, "total_items": len(items),
                           "shown": len(rows), "offset": offset,
                           "next_offset": offset + len(rows) if offset + len(rows) < len(items) else None,
                           "items": rows})

    def _delete_library_storage_item(self, arguments):
        from .library_storage import remove_library_item, scan_library_storage
        from .repository import library_directory

        section_key = str(arguments.get("section", "")).strip().lower()
        section = {"cache": "Cache", "temporary": "Temporary"}.get(section_key)
        category = str(arguments.get("category", "")).strip()
        relative_path = str(arguments.get("relative_path", "")).strip()
        if section is None or not category or not relative_path:
            raise ValueError("Choose Cache or Temporary and pass an exact category and relative_path from list_library_storage")
        library_path = library_directory()

        def locate():
            storage = scan_library_storage(library_path)
            if storage.get("error"):
                raise RuntimeError(f"Could not scan the Library: {storage['error']}")
            items = storage["cache_items" if section == "Cache" else "temporary_items"]
            return next((item for item in items
                         if item.get("category") == category
                         and item.get("relative_path") == relative_path), None)

        item = locate()
        if item is None:
            raise ValueError("That exact Cache or Temporary item is not present; list Library storage again")
        from .library_section import LibrarySection

        def safety_reason(candidate):
            reason = LibrarySection._item_use_reason(library_path, section, candidate)
            if reason or section != "Temporary":
                return reason
            # Temporary entries have no dedicated Library UI page with item-level
            # controls, so retain the broad active-work guard for these files.
            from .multistage_process import MultiStageProcess, MultiStageProcessState
            from .qemu_run import QemuRun
            if any(process.status == MultiStageProcessState.IN_PROGRESS
                   for process in MultiStageProcess.started_processes):
                return "Temporary cleanup is blocked while an app operation is active"
            if any(run.is_running for run in QemuRun.running_runs):
                return "Temporary cleanup is blocked while a VM is running"
            from .repositories import Repository
            if any(getattr(runner, "is_used", False) or getattr(runner, "_workspace_users", 0) > 0
                   for runner in Repository.Runner.value):
                return "Temporary cleanup is blocked while a VM runner is in use"
            return None

        reason = self._on_main_thread(lambda: safety_reason(item))
        if reason:
            raise RuntimeError(f"Can't delete this Library item: {reason}")
        if self.user_input_callback is None:
            raise RuntimeError("Deleting a Library item requires in-chat confirmation")
        answer = self.user_input_callback(
            f"Delete {section} item?",
            f"Permanently delete {item['relative_path']} ({item.get('description', item.get('name', category))}, {item.get('size', 0)} bytes) from the Library?",
            [{"id": "confirm", "label": "Permanently delete this item", "type": "boolean",
              "placeholder": "Confirm deletion", "required": True}],
        )
        if not answer or not answer.get("confirm"):
            return self._json({"status": "cancelled", "section": section,
                               "category": category, "relative_path": relative_path})

        def delete_if_idle():
            current = locate()
            if current is None:
                raise RuntimeError("The selected item changed while you were confirming; nothing was deleted")
            reason = safety_reason(current)
            if reason:
                raise RuntimeError(f"Can't delete this Library item: {reason}")
            remove_library_item(library_path, section, category, relative_path)
            return self._json({"status": "deleted", "section": section,
                               "category": category, "relative_path": relative_path,
                               "name": current.get("name"), "size": current.get("size")})

        return self._on_main_thread(delete_if_idle)
