import json
import os
import threading
import re
import time
from datetime import datetime

from gi.repository import Gtk, Adw, Gdk, GLib

from .ollama_client import OllamaCancelled, OllamaClient, OllamaError
from .ai_tool_bridge import AIToolBridge
from .ai_flows import AI_FLOWS
from .ai_conversation_log import DEBUG_FLAG, save_conversation


class _GemmaCallArguments:
    """Small recursive reader for Gemma's JSON-like function argument syntax."""

    def __init__(self, source: str):
        self.source = source.strip()
        self.position = 0

    def _space(self):
        while self.position < len(self.source) and self.source[self.position].isspace():
            self.position += 1

    def _string(self):
        marker = '<|"|>'
        if not self.source.startswith(marker, self.position):
            raise ValueError("Expected Gemma string marker")
        self.position += len(marker)
        end = self.source.find(marker, self.position)
        if end < 0:
            raise ValueError("Unterminated Gemma string")
        value = self.source[self.position:end]
        self.position = end + len(marker)
        return value

    def _value(self):
        self._space()
        if self.position >= len(self.source):
            raise ValueError("Missing argument value")
        if self.source.startswith('<|"|>', self.position):
            return self._string()
        char = self.source[self.position]
        if char == '[':
            self.position += 1
            values = []
            self._space()
            while self.position < len(self.source) and self.source[self.position] != ']':
                values.append(self._value())
                self._space()
                if self.position < len(self.source) and self.source[self.position] == ',':
                    self.position += 1
                    continue
                if self.position >= len(self.source) or self.source[self.position] != ']':
                    raise ValueError("Expected comma or closing bracket")
            if self.position >= len(self.source):
                raise ValueError("Unterminated array")
            self.position += 1
            return values
        if char == '{':
            self.position += 1
            result = {}
            self._space()
            while self.position < len(self.source) and self.source[self.position] != '}':
                key_start = self.position
                while self.position < len(self.source) and self.source[self.position] not in ':,}':
                    self.position += 1
                key = self.source[key_start:self.position].strip()
                if not key or self.position >= len(self.source) or self.source[self.position] != ':':
                    raise ValueError("Expected object key and colon")
                self.position += 1
                result[key] = self._value()
                self._space()
                if self.position < len(self.source) and self.source[self.position] == ',':
                    self.position += 1
                    continue
                if self.position >= len(self.source) or self.source[self.position] != '}':
                    raise ValueError("Expected comma or closing brace")
            if self.position >= len(self.source):
                raise ValueError("Unterminated object")
            self.position += 1
            return result
        start = self.position
        while self.position < len(self.source) and self.source[self.position] not in ',]}':
            self.position += 1
        primitive = self.source[start:self.position].strip()
        if primitive == "true":
            return True
        if primitive == "false":
            return False
        if primitive == "null":
            return None
        try:
            return json.loads(primitive)
        except json.JSONDecodeError:
            # Gemma may omit string delimiters for simple enum values.
            if primitive and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:/+-]*", primitive):
                return primitive
            raise ValueError("Invalid argument value")

    def parse(self):
        self._space()
        if self.position >= len(self.source) or self.source[self.position] != '{':
            raise ValueError("Expected argument object")
        value = self._value()
        if not isinstance(value, dict):
            raise ValueError("Arguments must be an object")
        return value


class OllamaChatView(Gtk.Box):
    """Simple one-session chat with the selected Ollama model."""
    ACTION_TOOLS = {
        "open_section", "open_project", "open_object", "open_wizard",
        "create_git_directory", "create_vm_runner", "create_toolset", "delete_git_directory",
        "delete_project", "delete_snapshot", "delete_packages_cache", "delete_runner", "delete_toolset",
        "delete_build_run",
        "rename_git_directory", "rename_toolset", "rename_project", "update_git_directory",
        "commit_git_changes", "discard_git_changes", "update_settings", "set_bug_status", "remove_bug",
        "add_bug_note", "edit_bug_note", "delete_bug_note", "set_toolset_mount", "update_toolset",
        "open_bugzilla",
        "configure_toolset",
        "set_runner_state", "update_runner", "create_project", "update_project", "update_project_template", "update_project_repository", "create_stage",
        "update_stage", "set_stage_parent", "set_stage_setting", "reset_stage_setting", "write_stage_script", "delete_stage", "create_live_iso",
        "create_stage_image", "deploy_stage_to_target", "start_build", "stop_build", "start_vm", "create_snapshot", "stop_operation", "control_vm", "send_vm_console_input",
        "create_packages_directory", "pull_ollama_model", "delete_build_record",
        "resume_build_queue",
        "delete_library_storage_item",
        "change_library_location",
        "update_stage_kernel", "delete_stage_kernel",
        "import_snapshot",
        "deploy_live_iso_to_disk",
        "open_local_folder",
        "write_object_file",
    }

    def __init__(self, base_url: str, model: str, content_navigation_view=None, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self.base_url = base_url
        self.model = model
        self.content_navigation_view = content_navigation_view
        self.debug_conversation_logging = os.environ.get(DEBUG_FLAG) == "1"
        self._debug_conversation_save_requested = False
        self._debug_conversation_saved = False
        self._debug_events = []
        self._debug_events_lock = threading.Lock()
        self.bridge = AIToolBridge(content_navigation_view=content_navigation_view,
                                   user_input_callback=self._request_user_input)
        self._turn_active = False
        self._turn_cancel_event = None
        self._active_client = None
        self._active_form_cancel = None
        self._active_form_activity = None
        self.flow = AI_FLOWS.create("mcp", self.bridge)
        tool_contract = [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in self.flow.startup_tools()
        ]
        system_message = self.flow.system_message() + (
            "\n\nStartup tools (discover other app tool schemas on demand):\n"
            + json.dumps(tool_contract, ensure_ascii=False, separators=(",", ":"))
        )
        if "gemma" in str(model).lower():
            system_message += (
                "\n\nTool-call protocol for this model: Gemma. Emit app actions only as "
                "`<|tool_call>call:name{argument:<|\"|>string<|\"|>}<tool_call|>`. "
                "Use braces for objects, brackets for arrays, `<|\"|>...<|\"|>` for strings, "
                "and JSON literals for booleans, numbers, and null. Example: "
                "`<|tool_call>call:list_options{categories:[<|\"|>projects<|\"|>]}<tool_call|>`. "
                "Wait for each tool result; do not print channel markers or repeat a pending call."
            )
        else:
            system_message += (
                "\n\nTool-call protocol for this model: JSON inside the tool_call envelope. Emit app actions "
                "only as `<tool_call>{\"name\":\"tool_name\",\"arguments\":{...}}</tool_call>`. "
                "The arguments value must be a JSON object that exactly matches the tool schema; use exact "
                "IDs and option values returned by app tools. Call one tool at a time and wait for its result. "
                "Example: `<tool_call>{\"name\":\"list_options\",\"arguments\":{\"categories\":[\"projects\"]}}"
                "</tool_call>`. Do not use function/parameter tags, channel markers, or another call format."
            )
        self.messages: list[dict] = [{"role": "system", "content": system_message}]

        self.transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                                  margin_start=24, margin_end=24, margin_top=18, margin_bottom=18)
        self.scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.set_child(self.transcript)

        self.raw_output_buffer = Gtk.TextBuffer()
        self.raw_output_view = Gtk.TextView(buffer=self.raw_output_buffer, editable=False,
                                            cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                            monospace=True, left_margin=24, right_margin=24,
                                            top_margin=18, bottom_margin=18)
        self.raw_output_view.add_css_class("chat-raw-output")
        self.raw_output_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.raw_output_scroll.set_child(self.raw_output_view)

        self.chat_stack = Gtk.Stack(hexpand=True, vexpand=True,
                                    transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.chat_stack.add_titled(self.scroll, "parsed-chat", "Chat")
        self.chat_stack.add_titled(self.raw_output_scroll, "raw-output", "Raw output")
        self.chat_stack.set_visible_child_name("parsed-chat")
        self.chat_tabs = Gtk.StackSwitcher(stack=self.chat_stack, halign=Gtk.Align.CENTER)
        self.chat_tabs.set_margin_top(6)
        self.append(self.chat_tabs)
        self.append(self.chat_stack)
        self._scroll_source_id = 0

        composer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                           margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        self.entry = Gtk.Entry(placeholder_text="Message…", hexpand=True)
        self.entry.connect("activate", self._send)
        composer.append(self.entry)
        self.send_button = Gtk.Button(label="Send")
        self.send_button.add_css_class("suggested-action")
        self.send_button.connect("clicked", self._composer_button_clicked)
        composer.append(self.send_button)
        self.append(composer)

        self._add_message("Ollama", f"Chatting with {model}")
        if self.debug_conversation_logging:
            application = Gtk.Application.get_default()
            if application is not None:
                application.connect(
                    "shutdown", lambda *_args: self.save_debug_conversation(force=True))

    def _composer_button_clicked(self, *_args):
        if self._turn_active:
            self._stop_turn()
        else:
            self._send()

    def _set_composer_button(self, stopping=False):
        self.send_button.set_label("Stopping…" if stopping else "Stop" if self._turn_active else "Send")
        if self._turn_active:
            self.send_button.remove_css_class("suggested-action")
            self.send_button.add_css_class("destructive-action")
            self.send_button.set_sensitive(not stopping)
        else:
            self.send_button.remove_css_class("destructive-action")
            self.send_button.add_css_class("suggested-action")
            self.send_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _stop_turn(self):
        if not self._turn_active or self._turn_cancel_event is None:
            return
        self._turn_cancel_event.set()
        if self._active_client is not None:
            self._active_client.cancel()
        self.bridge.cancel_active_tool()
        if self._active_form_cancel is not None:
            self._active_form_cancel()
        self.send_button.set_label("Stopping…")
        self.send_button.set_sensitive(False)

    def _send(self, *_args):
        prompt = self.entry.get_text().strip()
        if not prompt or self._turn_active:
            return
        self.bridge.begin_user_turn()
        self.entry.set_text("")
        self._add_message("You", prompt)
        response_label = self._add_message("Ollama", "", formatted=True, with_activity=True)
        self._active_turn_container = response_label._turn_container
        self._turn_active = True
        self._turn_cancel_event = threading.Event()
        self.bridge.cancel_event = self._turn_cancel_event
        self._active_client = OllamaClient(
            self.base_url, model=self.model, cancel_event=self._turn_cancel_event)
        self._set_composer_button()
        self.entry.set_sensitive(False)

        def request_reply():
            try:
                client = self._active_client
                reply = self._run_flow_turn(client, prompt, response_label)
                error = None if reply and reply.strip() else (
                    "The selected model returned no text. Try again or select a different model.")
            except OllamaCancelled:
                response_label._turn_stopped = True
                reply, error = "Stopped.", None
            except (OllamaError, ValueError) as exception:
                reply, error = "", str(exception)
            except Exception as exception:
                reply, error = "", f"Assistant tool error: {exception}"
            latest_response = getattr(response_label._turn_container, "_latest_response", response_label)
            GLib.idle_add(self._finish_reply, latest_response, reply, error)

        threading.Thread(target=request_reply, name="ollama-chat", daemon=True).start()

    def _request_user_input(self, title: str, message: str, fields: list[dict]) -> dict | None:
        """Pause the tool worker while the user completes a model-generated chat form."""
        completed = threading.Event()
        outcome = {}
        GLib.idle_add(self._show_user_input_safely, title, message, fields, completed, outcome,
                      self._turn_cancel_event)
        while not completed.wait(0.2):
            if self._turn_cancel_event is not None and self._turn_cancel_event.is_set():
                GLib.idle_add(self._cancel_active_user_form)
                completed.wait(0.5)
                outcome.setdefault("selection", None)
                break
        if outcome.get("error"):
            raise RuntimeError(f"Couldn't display the requested form: {outcome['error']}")
        return outcome.pop("selection", None)

    def _show_user_input_safely(self, title: str, message: str, fields: list[dict],
                                completed, outcome, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            outcome["selection"] = None
            completed.set()
            return GLib.SOURCE_REMOVE
        try:
            return self._build_user_input(title, message, fields, completed, outcome)
        except Exception as error:
            outcome["error"] = str(error)
            completed.set()
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.add_css_class("chat-input-form")
            card.append(Gtk.Label(label=f"Couldn't display the form: {error}", xalign=0, wrap=True))
            turn_container = getattr(self, "_active_turn_container", None)
            (turn_container or self.transcript).append(card)
            self._scroll_to_bottom()
            return GLib.SOURCE_REMOVE

    def _build_user_input(self, title: str, message: str, fields: list[dict], completed, outcome):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("chat-input-form")
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("heading")
        card.append(title_label)
        if message:
            description = Gtk.Label(label=message, xalign=0, wrap=True)
            description.add_css_class("dim-label")
            card.append(description)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        card.append(grid)
        controls = {}
        for row, field in enumerate(fields):
            identifier = str(field["id"])
            field_type = field["type"]
            fallback_label = identifier.replace("_", " ").replace("-", " ").capitalize()
            label = Gtk.Label(label=str(field.get("label") or fallback_label or identifier),
                              xalign=0, valign=Gtk.Align.START)
            grid.attach(label, 0, row, 1, 1)
            if field_type in ("text", "secret"):
                widget = Gtk.Entry(placeholder_text=str(field.get("placeholder", "")), hexpand=True,
                                   visibility=field_type != "secret",
                                   input_purpose=(Gtk.InputPurpose.PASSWORD if field_type == "secret"
                                                  else Gtk.InputPurpose.FREE_FORM))
                if field_type != "secret" and field.get("default") is not None:
                    widget.set_text(str(field["default"]))
                controls[identifier] = (field, widget)
                grid.attach(widget, 1, row, 1, 1)
            elif field_type == "select":
                options = field["options"]
                widget = Gtk.DropDown.new_from_strings(["Choose an option…"] + [str(item["label"]) for item in options])
                default_value = field.get("default")
                default_index = next((index for index, item in enumerate(options)
                                      if item.get("default") or item.get("value") == default_value), None)
                if default_index is None and field.get("required", True) and len(options) == 1:
                    default_index = 0
                if default_index is not None:
                    widget.set_selected(default_index + 1)
                controls[identifier] = (field, widget)
                grid.attach(widget, 1, row, 1, 1)
            elif field_type == "multiselect":
                options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                options = []
                for item in field["options"]:
                    option = Gtk.CheckButton(label=str(item["label"]))
                    default_values = field.get("default", [])
                    if not isinstance(default_values, list):
                        default_values = [default_values]
                    option.set_active(bool(item.get("default") or item.get("default_selected")
                                           or item.get("value") in default_values))
                    options_box.append(option)
                    options.append((item, option))
                controls[identifier] = (field, options)
                grid.attach(options_box, 1, row, 1, 1)
            else:
                widget = Gtk.CheckButton(label=str(field.get("placeholder", "Yes")))
                widget.set_active(bool(field.get("default", False)))
                controls[identifier] = (field, widget)
                grid.attach(widget, 1, row, 1, 1)

        feedback_label = Gtk.Label(label="Note or correction (optional)", xalign=0)
        feedback_label.add_css_class("dim-label")
        feedback_entry = Gtk.Entry(
            placeholder_text="For example: an architecture is missing",
            hexpand=True)
        grid.attach(feedback_label, 0, len(fields), 1, 1)
        grid.attach(feedback_entry, 1, len(fields), 1, 1)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        submit = Gtk.Button(label="Continue")
        submit.add_css_class("suggested-action")
        actions.append(cancel)
        actions.append(submit)
        card.append(actions)

        def finish(selection):
            self._active_form_cancel = None
            outcome["selection"] = selection
            card.set_sensitive(False)
            if any(field.get("sensitive") or field.get("type") == "secret" for field in fields):
                # Secret answers are handed directly to the waiting local tool call,
                # never retained in the transcript or left visible in disabled inputs.
                for field, widget in controls.values():
                    if field.get("sensitive") or field.get("type") == "secret":
                        widget.set_text("")
                parent = card.get_parent()
                if parent is not None:
                    parent.remove(card)
            completed.set()

        def submit_form(*_args):
            answers = {"__user_feedback__": feedback_entry.get_text().strip()}
            for identifier, (field, widget) in controls.items():
                field_type = field["type"]
                required = field.get("required", True)
                if field_type in ("text", "secret"):
                    value = widget.get_text() if field_type == "secret" else widget.get_text().strip()
                    if required and not value:
                        widget.add_css_class("error")
                        widget.grab_focus()
                        return
                    answers[identifier] = value
                elif field_type == "select":
                    index = widget.get_selected() - 1
                    if index < 0 and required:
                        widget.add_css_class("error")
                        return
                    answers[identifier] = field["options"][index]["value"] if index >= 0 else ""
                elif field_type == "multiselect":
                    values = [str(option["value"]) for option, button in widget if button.get_active()]
                    if required and not values:
                        return
                    answers[identifier] = values
                else:
                    answers[identifier] = widget.get_active()
            finish(answers)
        submit.connect("clicked", submit_form)
        cancel.connect("clicked", lambda *_: finish(None))
        self._active_form_cancel = lambda: finish(None)
        turn_container = getattr(self, "_active_turn_container", None)
        (turn_container or self.transcript).append(card)
        if self._active_form_activity is not None:
            self._active_form_activity.set_label("Waiting for your answer…")
        self._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    def _run_flow_turn(self, client: OllamaClient, user_prompt: str,
                       response_label: Gtk.Label) -> str:
        """Run Ollama tool calls until it returns a user-facing answer."""
        action_prompt = user_prompt
        if re.fullmatch(r"\s*(?:please\s+)?(?:try again|retry|continue|go ahead|do it|do that|proceed)[.!\s]*",
                        user_prompt, re.IGNORECASE):
            action_prompt = next((str(message.get("content", ""))
                                  for message in reversed(self.messages)
                                  if message.get("role") == "user"
                                  and self._requires_app_action(str(message.get("content", "")))
                                  and not str(message.get("content", "")).startswith(
                                      ("The request is an app action.", "Continue the requested"))), user_prompt)
        # The user message is already visible in the transcript; append it to history for the model.
        self.messages.append({"role": "user", "content": user_prompt})
        tools = {tool.name: tool for tool in self.flow.tools()}
        streamed_text = []
        executed_calls = set()
        total_calls = 0
        current_response = response_label
        retried_missing_action = False
        retried_tool_failure = False
        tool_failures = []
        used_tools = set()
        successful_action_tools = set()
        action_cancelled = False
        pending_form_activity = [None]
        for _round in range(16):
            client._check_cancelled()
            # Render normal assistant content as it arrives, including while
            # an app action is being decided. Thinking is shown only after an
            # actual thinking-channel delta arrives.
            suppress_provisional_text = False
            GLib.idle_add(self._clear_thinking, current_response)
            streamed_tool_activities = {}
            streamed_tool_text = [""]
            streamed_tool_previews = {}
            repeated_closer_tail = [""]

            def on_chunk(event):
                # Small local models can enter a decoding loop and emit the
                # same empty XML-style parameter closer indefinitely. Stop
                # this response early; otherwise the UI appears hung and the
                # model may eventually produce an unrelated, guessed action.
                logprobs = event.get("logprobs") or []
                fragments = [item.get("token") for item in logprobs
                             if isinstance(item, dict) and isinstance(item.get("token"), str)]
                if not fragments:
                    message = event.get("message") or {}
                    fragment = message.get("content") or message.get("thinking") or ""
                    if isinstance(fragment, str) and fragment:
                        fragments = [fragment]
                for token in fragments:
                    repeated_closer_tail[0] = (repeated_closer_tail[0] + token)[-256:]
                    if re.search(r"(?:</parameter>\s*){8,}$", repeated_closer_tail[0]):
                        raise OllamaError(
                            "The model repeated empty tool-call parameter tags. "
                            "Generation was stopped before any tool action ran; retry with a different model or prompt.")
                self._stream_chunk(
                    current_response, event, streamed_text,
                    suppress_content=suppress_provisional_text,
                    tool_call_text=streamed_tool_text)
                self._stream_tool_call_progress(
                    event, current_response._turn_container, streamed_tool_activities,
                    pending_form_activity, streamed_tool_previews, tools)

            for stream_attempt in range(3):
                try:
                    self._sanitize_tool_call_history()
                    result = client.chat_stream(
                        self.messages,
                        on_chunk=on_chunk,
                        think=True,
                        logprobs=True,
                    )
                    break
                except OllamaError as error:
                    error_text = str(error).strip().lower()
                    is_eof = error_text in {"eof", "unexpected eof", "end of file"}
                    self._record_debug_event(
                        "STREAM INTERRUPTED" if is_eof else "STREAM ERROR",
                        text=f"Attempt {stream_attempt + 1}/3: {error}")
                    if not is_eof:
                        raise

                    recovered_call = self._recover_complete_streamed_tool_call(current_response)
                    if recovered_call is not None:
                        # Ollama sometimes emits a complete JSON call and then
                        # closes the stream before its outer </tool_call> marker
                        # or final done event. Execute only fully decoded JSON;
                        # the normal tool path still validates its schema.
                        result = {"message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [recovered_call],
                        }}
                        break

                    # EOF can arrive after partial tool-call tokens. Nothing from
                    # that incomplete response has been executed or added to model
                    # history; preserve its visible segment and retry in a new one.
                    self._call_on_ui_thread(
                        self._flush_incomplete_stream_text, current_response)
                    self._call_on_ui_thread(
                        self._set_thinking_status, current_response,
                        "Generation interrupted · retrying…")
                    self._call_on_ui_thread(self._collapse_turn_thinking, current_response)
                    preparation = getattr(current_response, "_tool_preparation_frame", None)
                    if preparation is not None and preparation.get_visible():
                        self._call_on_ui_thread(
                            self._set_activity_status, preparation,
                            ("Tool call interrupted · retrying" if stream_attempt < 2 else
                             "Tool call interrupted · failed"))
                        self._call_on_ui_thread(preparation.set_expanded, False)
                    for activity in streamed_tool_activities.values():
                        self._call_on_ui_thread(
                            self._set_activity_status, activity,
                            ("Tool call not run · Ollama stream ended (EOF)" if stream_attempt == 2 else
                             f"Ollama stream interrupted · retrying ({stream_attempt + 1}/2)…"))
                    streamed_text.clear()
                    streamed_tool_text[0] = ""
                    streamed_tool_previews.clear()
                    repeated_closer_tail[0] = ""
                    streamed_tool_activities.clear()
                    if stream_attempt == 2:
                        raise OllamaError(
                            "Ollama ended generation unexpectedly (EOF) three times. "
                            "No tool call from the interrupted responses was run. "
                            "Check that Ollama is still running and the selected model is available, then retry.") from error
                    current_response = self._call_on_ui_thread(
                        lambda: self._append_response_segment(
                            current_response._turn_container, raw_header=False))
                    self._call_on_ui_thread(
                        self._set_thinking_status, current_response,
                        f"Thinking · retrying after interrupted stream ({stream_attempt + 1}/2)…")
                    time.sleep(0.25 * (stream_attempt + 1))
            client._check_cancelled()
            assistant = result.get("message") or {}
            # Some Ollama models place the textual tool call in their separate
            # thinking field. Parse it there too, after the stream preview path.
            assistant_thinking = assistant.get("thinking", "")
            if isinstance(assistant.get("content"), str):
                assistant = dict(assistant)
                assistant["content"] = assistant["content"].replace("<think>", "").replace("</think>", "")
            original_content = assistant.get("content", "")
            parsed_calls, clean_content = self._extract_tool_calls(original_content)
            if not parsed_calls and assistant_thinking:
                parsed_calls, _ = self._extract_tool_calls(assistant_thinking)
            if parsed_calls:
                assistant["content"] = clean_content
                if assistant_thinking and not self._extract_tool_calls(original_content)[0]:
                    assistant.pop("thinking", None)
            elif self._contains_text_tool_envelope(original_content):
                assistant["content"] = clean_content
            has_text_tool_envelope = self._contains_text_tool_envelope(original_content) or (
                assistant_thinking and self._contains_text_tool_envelope(assistant_thinking))
            calls = parsed_calls if has_text_tool_envelope else (assistant.get("tool_calls") or parsed_calls)
            calls = self._normalize_tool_call_shape(calls)
            if calls and not assistant.get("tool_calls"):
                assistant["tool_calls"] = calls
            if not calls:
                calls = self._collect_streamed_tool_calls(streamed_tool_previews)
                if calls:
                    assistant = dict(assistant)
                    assistant["tool_calls"] = calls
            if not calls:
                # A malformed textual envelope never reaches _run_tool_call,
                # so convert it into the same schema-aware repair path used
                # for tool execution errors. Do not execute partially parsed
                # arguments.
                raw_tool_text = "\n".join(
                    str(value or "") for value in (
                        original_content,
                        assistant_thinking,
                        streamed_tool_text[0],
                        getattr(current_response, "_tool_call_text", ""),
                        getattr(current_response, "_generated_token_text", ""),
                    ))
                if self._contains_text_tool_envelope(raw_tool_text):
                    tool_match = re.search(
                        r"<function=([A-Za-z_][A-Za-z0-9_]*)>|"
                        r'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"|'
                        r"<\|tool_call\>\s*call:([A-Za-z_][A-Za-z0-9_]*)",
                        raw_tool_text)
                    if tool_match:
                        failed_name = next((group for group in tool_match.groups() if group), "")
                        if not any(name == failed_name for name, _ in tool_failures):
                            # The malformed envelope is otherwise removed from
                            # assistant history before the repair turn. Preserve
                            # its tag structure (never argument values) so the
                            # model can correct the actual delimiter/order error.
                            envelope_start = raw_tool_text.find("<tool_call>")
                            if envelope_start < 0:
                                envelope_start = raw_tool_text.find("<|tool_call>")
                            excerpt = raw_tool_text[envelope_start:] if envelope_start >= 0 else raw_tool_text
                            excerpt = excerpt[:6000]
                            tag_pattern = re.compile(
                                r"<(?P<close>/)?(?P<kind>tool_call|function|parameter)"
                                r"(?:=(?P<name>[A-Za-z_][A-Za-z0-9_]*))?[^>]*>")
                            structures = []
                            for tag in tag_pattern.finditer(excerpt):
                                closing, kind, name = tag.group("close", "kind", "name")
                                structures.append(
                                    f"</{kind}>" if closing else
                                    f"<{kind}={name}>" if name else f"<{kind}>")
                                if len(structures) >= 80:
                                    break
                            if not structures:
                                structures = re.findall(r"<\|tool_call>|<tool_call\|>", excerpt)[:80]
                            structure_summary = " ".join(structures)
                            detail = (
                                f" Generated tag sequence: {structure_summary}."
                                if structures else "")
                            if re.search(r"<function=ask_user\s*>\s*\{", excerpt):
                                detail += (
                                    " The call used JSON inside the legacy function wrapper. "
                                    "For ask_user, send title, message, and fields as separate "
                                    "<parameter=name>JSON value</parameter> entries; do not mix "
                                    "JSON-object and XML-parameter syntax. Ensure the JSON is "
                                    "complete and has no trailing commas before closing tags.")
                            if re.search(r"\btool[-_]call\s*=\s*\{", excerpt, re.IGNORECASE):
                                detail += (
                                    " The model emitted an unwrapped tool_call object. No action was run. "
                                    "Retry using exactly <tool_call>{\"name\":\"tool_name\","
                                    "\"arguments\":{...}}</tool_call>, matching the supplied tool "
                                    "definition. For destructive actions, ask for confirmation first.")
                            tool_failures.append((
                                failed_name,
                                "The generated tool-call envelope was malformed and no action was run. "
                                "Regenerate the call using the exact tool schema; keep parameter values valid JSON "
                                "and close every parameter, function, and tool_call tag in order." + detail))
            # Execute one call per model turn. In particular, ask_user must
            # pause generation until the user submits its form; later calls
            # from the same response were planned against unanswered fields.
            if len(calls) > 1:
                for call_index, call in enumerate(calls[1:], start=1):
                    self._call_on_ui_thread(
                        self._finish_streamed_tool_activity,
                        streamed_tool_activities.get(self._streamed_tool_index(call, call_index)),
                        "Discarded · a new call will be generated after the previous result")
                calls = calls[:1]
                assistant = dict(assistant)
                assistant["tool_calls"] = calls
            if calls:
                # A follow-up call means the model is already acting on the
                # preceding tool result. Retry only if it instead stops in prose.
                tool_failures.clear()
                # Promote provisional tokens when Ollama identifies the
                # structured call; the frame is removed once this model response
                # has finished, while the activity row remains for execution.
                completed_call = calls[0] if isinstance(calls[0], dict) else {}
                self._call_on_ui_thread(
                    self._promote_tool_preparation, current_response, completed_call)
                # The full model response is complete now; keep its activity row,
                # but remove the temporary token/parameter preparation frame.
                self._call_on_ui_thread(
                    self._remove_tool_preparation_frames, current_response._turn_container)
                self._call_on_ui_thread(self._collapse_turn_thinking, current_response)
            self.messages.append(assistant)
            if not calls:
                reply = (assistant.get("content") or "".join(streamed_text)
                         or getattr(current_response, "_visible_streamed_answer", ""))
                if not reply.strip():
                    # Some Ollama/model combinations return an empty final streaming event even
                    # though the request succeeded. Retry once without streaming before showing
                    # an empty-response error.
                    self.messages.pop()
                    fallback = client.chat(self.messages, think=True)
                    assistant = fallback.get("message") or {}
                    original_content = assistant.get("content", "")
                    fallback_thinking = assistant.get("thinking", "")
                    if not getattr(current_response, "_raw_streamed_text", False):
                        self._call_on_ui_thread(
                            self._append_raw_output,
                            str(fallback_thinking or "") + str(original_content or ""))
                        current_response._raw_streamed_text = True
                    parsed_calls, clean_content = self._extract_tool_calls(original_content)
                    if not parsed_calls and fallback_thinking:
                        parsed_calls, _ = self._extract_tool_calls(fallback_thinking)
                    if parsed_calls:
                        assistant = dict(assistant)
                        assistant["content"] = clean_content
                        if fallback_thinking and not self._extract_tool_calls(original_content)[0]:
                            assistant.pop("thinking", None)
                    elif self._contains_text_tool_envelope(original_content):
                        assistant = dict(assistant)
                        assistant["content"] = clean_content
                    has_text_tool_envelope = self._contains_text_tool_envelope(original_content) or (
                        fallback_thinking and self._contains_text_tool_envelope(fallback_thinking))
                    calls = parsed_calls if has_text_tool_envelope else (assistant.get("tool_calls") or parsed_calls)
                    if calls:
                        calls = calls[:1]
                        assistant = dict(assistant)
                        assistant["tool_calls"] = calls
                        self.messages.append(assistant)
                        streamed_text.clear()
                        # The model chose a follow-up tool after seeing the previous
                        # result, so let that call resolve the error before prompting
                        # it again with the same failure.
                        tool_failures.clear()
                        for call in calls:
                            client._check_cancelled()
                            total_calls += 1
                            if total_calls > 16:
                                raise OllamaError("The assistant requested too many app lookups. Please ask a more specific question.")
                            call_key = self._tool_call_key(call)
                            if call_key in executed_calls:
                                raise OllamaError("The assistant repeated the same app lookup. Please try asking again.")
                            executed_calls.add(call_key)
                            used_tools.add((call.get("function") or {}).get("name", ""))
                            activity = streamed_tool_activities.get(
                                self._streamed_tool_index(call, 0))
                            output = self._run_tool_call(
                                call, tools, current_response._turn_container,
                                activity_view=activity)
                            tool_name = (call.get("function") or {}).get("name", "")
                            failure = self._tool_failure_message(output)
                            if failure:
                                tool_failures.append((tool_name, failure))
                            else:
                                if tool_name in self.ACTION_TOOLS:
                                    successful_action_tools.add(tool_name)
                            try:
                                action_cancelled = json.loads(output).get("status") == "cancelled"
                            except (TypeError, ValueError, AttributeError):
                                pass
                        if action_cancelled:
                            reply = "Cancelled; no changes were made."
                            self.messages.append({"role": "assistant", "content": reply})
                            GLib.idle_add(self._finalize_response, current_response)
                            return reply
                        current_response = self._call_on_ui_thread(
                            self._append_response_segment, current_response._turn_container)
                        continue
                    reply = assistant.get("content") or fallback.get("response") or ""
                    self.messages.append(assistant)
                if not reply.strip() and self._is_project_creation_request(user_prompt):
                    recovered = self._recover_template_project_from_form_history(
                        tools, current_response._turn_container)
                    if recovered:
                        reply = recovered
                        self.messages.append({"role": "assistant", "content": reply})
                        GLib.idle_add(self._finalize_response, current_response)
                        return reply
                for activity in streamed_tool_activities.values():
                    self._call_on_ui_thread(
                        self._finish_streamed_tool_activity, activity,
                        "Tool call did not complete · no action was run")
                if tool_failures and not retried_tool_failure and not action_cancelled:
                    tool_name, failure = tool_failures[-1]
                    failed_tool = tools.get(tool_name)
                    if failed_tool is not None:
                        tool_definition = json.dumps({
                            "name": failed_tool.name,
                            "description": failed_tool.description,
                            "parameters": failed_tool.parameters,
                        }, ensure_ascii=False, separators=(",", ":"))[:12000]
                    else:
                        tool_definition = "Tool definition unavailable; search list_tools and read_tool_schema."
                    self.messages.pop()  # Replace the model's premature error narration with a repair attempt.
                    repair = (
                        f"Your previous {tool_name or 'app tool'} call failed. Use this exact tool definition "
                        f"when correcting it: {tool_definition}\n"
                        "The app returned this exact error: "
                        f"{failure[:1600]}\n"
                        "Use the definition and error to correct the next call; reload required options/context "
                        "if needed. Do not repeat the same invalid arguments, "
                        "do not claim success, and do not merely describe a plan. Continue the user's request "
                        "with app tools; if a choice is genuinely needed, use ask_user with 1–50 valid fields."
                    )
                    self.messages.append({"role": "user", "content": repair})
                    streamed_text.clear()
                    current_response = self._call_on_ui_thread(
                        self._append_response_segment, current_response._turn_container)
                    retried_tool_failure = True
                    continue
                if (not retried_missing_action and not action_cancelled
                        and self._requires_app_action(action_prompt)
                        and not (successful_action_tools & self.ACTION_TOOLS)):
                    last_error = next((str(message.get("content", ""))
                                       for message in reversed(self.messages)
                                       if message.get("role") == "tool"
                                       and self._tool_failure_message(message.get("content", ""))), "")
                    self.messages.pop()  # Discard the assistant's unsupported promise from model history.
                    repair = (
                        "The request is an app action. Continue using app tools; do not stop after describing a plan or an error. "
                        "If a tool failed, correct the call using the exact schema and returned choices. "
                        "If details are missing, call ask_user with a title, message, and 1–50 valid fields. "
                        "Emit exactly one tool call now and wait for its result. Use exact option values and labels."
                    )
                    if last_error:
                        repair += " The latest tool error was: " + last_error[:1600]
                    self.messages.append({"role": "user", "content": repair})
                    streamed_text.clear()
                    # Keep the first answer in the transcript. Clearing it here made
                    # completed model output appear to vanish when the action retry ran.
                    current_response = self._call_on_ui_thread(
                        self._append_response_segment, current_response._turn_container)
                    retried_missing_action = True
                    continue
                if (retried_missing_action and not action_cancelled
                        and self._requires_app_action(action_prompt)
                        and not (successful_action_tools & self.ACTION_TOOLS)
                        and not self._is_capability_explanation(reply)):
                    if self._is_project_creation_request(action_prompt):
                        # Small local models sometimes narrate the project form
                        # instead of calling ask_user. Recover with a form built
                        # from live Library options, then let the model continue
                        # using the submitted answers.
                        try:
                            source_options = json.loads(self.bridge._list_options(
                                {"categories": ["project_sources"]})).get("project_sources") or []
                            if not source_options:
                                raise ValueError("No project sources are available")
                            fields = [
                                {"id": "source", "label": "Source type", "type": "select",
                                 "required": True, "options": source_options},
                                {"id": "name", "label": "Project name", "type": "text",
                                 "required": True, "placeholder": "Enter a unique project name"},
                            ]
                            if self.messages and self.messages[-1].get("role") == "assistant":
                                self.messages.pop()
                            form_arguments = {
                                "title": "Create New Project",
                                "message": "First choose where the project comes from and give it a name. We’ll ask only for settings that apply to that source next.",
                                "fields": fields,
                            }
                            call = {"function": {"name": "ask_user", "arguments": form_arguments}}
                            self.messages.append({"role": "assistant", "tool_calls": [call]})
                            used_tools.add("ask_user")
                            self._run_tool_call(call, tools, current_response._turn_container)
                            form_result = json.loads(self.messages[-1].get("content", "{}"))
                            if form_result.get("status") == "cancelled":
                                reply = "Project creation cancelled; nothing was changed."
                                self.messages.append({"role": "assistant", "content": reply})
                                GLib.idle_add(self._finalize_response, current_response)
                                return reply
                            answers = form_result.get("answers", {})
                            source = str(answers.get("source", ""))
                            if source in ("empty", "git", "template") and not form_result.get("user_feedback"):
                                categories = ["architectures", "releng_directories", "snapshots", "toolsets"]
                                if source == "template":
                                    categories.append("project_templates")
                                available = json.loads(self.bridge._list_options({"categories": categories}))
                                next_fields = []
                                for identifier, category, label in (
                                        ("architecture", "architectures", "Architecture"),
                                        ("releng_directory", "releng_directories", "Releng directory"),
                                        ("snapshot", "snapshots", "Snapshot"),
                                        ("toolset", "toolsets", "Toolset"),
                                        ("template", "project_templates", "Project template")):
                                    if identifier == "template" and source != "template":
                                        continue
                                    options = available.get(category) or []
                                    if not options:
                                        raise ValueError(f"No available options for {label.lower()}")
                                    field = {"id": identifier, "label": label, "type": "select",
                                             "required": True, "options": options}
                                    default = next((item.get("value") for item in options
                                                    if item.get("default")), None)
                                    if default is not None:
                                        field["default"] = default
                                    next_fields.append(field)
                                if source == "git":
                                    next_fields.append({"id": "repository_url", "label": "Git repository URL",
                                                        "type": "text", "required": True,
                                                        "placeholder": "https://…"})
                                next_arguments = {
                                    "title": "Project Settings",
                                    "message": "Choose the remaining settings for this source. Defaults are preselected where available.",
                                    "fields": next_fields,
                                }
                                next_call = {"function": {"name": "ask_user", "arguments": next_arguments}}
                                self.messages.append({"role": "assistant", "tool_calls": [next_call]})
                                self._run_tool_call(next_call, tools, current_response._turn_container)
                                next_result = json.loads(self.messages[-1].get("content", "{}"))
                                if next_result.get("status") == "cancelled":
                                    reply = "Project creation cancelled; nothing was changed."
                                    self.messages.append({"role": "assistant", "content": reply})
                                    GLib.idle_add(self._finalize_response, current_response)
                                    return reply
                                answers.update(next_result.get("answers", {}))
                                source = str(answers.get("source", source))
                                if next_result.get("user_feedback"):
                                    form_result["user_feedback"] = next_result["user_feedback"]
                            if source == "template" and not form_result.get("user_feedback"):
                                template_key = str(answers.get("template", "")).strip()
                                if not template_key:
                                    raise ValueError("Choose a project template before configuring its options")
                                template_answers = {}
                                requested_variable_ids = set()
                                for _variable_round in range(10):
                                    variable_options = json.loads(self.bridge._list_options({
                                        "categories": ["template_variables"],
                                        "context": {"template": template_key,
                                                    "project_name": str(answers.get("name", "")),
                                                    **{f"selected.{key}": value
                                                       for key, value in template_answers.items()}},
                                    })).get("template_variables", [])
                                    new_variables = [item for item in variable_options
                                                     if item.get("id") not in requested_variable_ids]
                                    if not new_variables:
                                        break
                                    variable_fields = []
                                    for variable in new_variables:
                                        variable_id = str(variable["id"])
                                        variable_type = variable.get("type")
                                        field_type = {
                                            "choice": "select", "architecture": "select",
                                            "multiple": "multiselect", "boolean": "boolean",
                                        }.get(variable_type, "text")
                                        field = {
                                            "id": variable_id,
                                            "label": str(variable.get("label") or variable_id),
                                            "type": field_type,
                                            "required": (variable_type != "multiple" or
                                                         int(variable.get("minimum_selected", 0)) > 0),
                                        }
                                        if variable.get("description"):
                                            field["description"] = variable["description"]
                                        if field_type in ("select", "multiselect"):
                                            field["options"] = variable.get("options", [])
                                        if variable.get("default") is not None:
                                            field["default"] = variable["default"]
                                        variable_fields.append(field)
                                        requested_variable_ids.add(variable_id)
                                    variable_call = {"function": {"name": "ask_user", "arguments": {
                                        "title": "Template Settings",
                                        "message": "Set the options for the selected project template. Defaults are preselected where available.",
                                        "fields": variable_fields,
                                    }}}
                                    self.messages.append({"role": "assistant", "tool_calls": [variable_call]})
                                    self._run_tool_call(variable_call, tools, current_response._turn_container)
                                    variable_result = json.loads(self.messages[-1].get("content", "{}"))
                                    if variable_result.get("status") == "cancelled":
                                        reply = "Project creation cancelled; nothing was changed."
                                        self.messages.append({"role": "assistant", "content": reply})
                                        GLib.idle_add(self._finalize_response, current_response)
                                        return reply
                                    template_answers.update(variable_result.get("answers", {}))
                                    if variable_result.get("user_feedback"):
                                        form_result["user_feedback"] = variable_result["user_feedback"]
                                        break
                                if not form_result.get("user_feedback"):
                                    create_arguments = {key: answers[key] for key in (
                                        "name", "architecture", "releng_directory", "snapshot", "toolset")
                                        if key in answers}
                                    create_arguments.update({"source": "template", "template": template_key,
                                                             "template_options": template_answers})
                                    create_call = {"function": {"name": "create_project",
                                                                 "arguments": create_arguments}}
                                    self.messages.append({"role": "assistant", "tool_calls": [create_call]})
                                    used_tools.add("create_project")
                                    self._run_tool_call(create_call, tools, current_response._turn_container)
                                    creation_output = self.messages[-1].get("content", "")
                                    if not creation_output.startswith("Tool failed:"):
                                        creation_result = json.loads(creation_output)
                                        if creation_result.get("result") == "started":
                                            reply = (f'Project “{creation_result["project"]}” creation has started. '
                                                     "You can follow its progress in Operations.")
                                            self.messages.append({"role": "assistant", "content": reply})
                                            GLib.idle_add(self._finalize_response, current_response)
                                            return reply
                            if source in ("empty", "git") and not form_result.get("user_feedback"):
                                repository_url = str(answers.get("repository_url", "")).strip()
                                if source == "git" and not repository_url:
                                    url_form = {"title": "Git Repository",
                                                "message": "Enter the repository URL to clone.",
                                                "fields": [{"id": "repository_url", "label": "Repository URL",
                                                            "type": "text", "required": True,
                                                            "placeholder": "https://…"}]}
                                    url_call = {"function": {"name": "ask_user", "arguments": url_form}}
                                    self.messages.append({"role": "assistant", "tool_calls": [url_call]})
                                    self._run_tool_call(url_call, tools, current_response._turn_container)
                                    url_result = json.loads(self.messages[-1].get("content", "{}"))
                                    if url_result.get("status") == "cancelled":
                                        reply = "Project creation cancelled; nothing was changed."
                                        self.messages.append({"role": "assistant", "content": reply})
                                        GLib.idle_add(self._finalize_response, current_response)
                                        return reply
                                    repository_url = str((url_result.get("answers") or {}).get(
                                        "repository_url", "")).strip()
                                    if url_result.get("user_feedback"):
                                        form_result["user_feedback"] = url_result["user_feedback"]
                                if not form_result.get("user_feedback"):
                                    create_arguments = {key: answers[key] for key in (
                                        "name", "source", "architecture", "releng_directory", "snapshot", "toolset")
                                        if key in answers}
                                    if source == "git":
                                        create_arguments["repository_url"] = repository_url
                                    create_call = {"function": {"name": "create_project",
                                                                 "arguments": create_arguments}}
                                    self.messages.append({"role": "assistant", "tool_calls": [create_call]})
                                    used_tools.add("create_project")
                                    self._run_tool_call(create_call, tools, current_response._turn_container)
                                    creation_output = self.messages[-1].get("content", "")
                                    if not creation_output.startswith("Tool failed:"):
                                        creation_result = json.loads(creation_output)
                                        if creation_result.get("result") == "started":
                                            reply = (f'Project “{creation_result["project"]}” creation has started. '
                                                     "You can follow its progress in Operations.")
                                            self.messages.append({"role": "assistant", "content": reply})
                                            GLib.idle_add(self._finalize_response, current_response)
                                            return reply
                            self.messages.append({"role": "user", "content":
                                "Continue the requested project creation using the submitted ask_user answers. "
                                "Honor any user_feedback correction. If the selected source is template, load the exact "
                                "template and its variables, then ask only for those dependent details in a valid form. "
                                "If source is git, collect a repository URL if the submitted value is empty. Use returned "
                                "defaults and call create_project; do not ask for already submitted values."})
                            streamed_text.clear()
                            current_response = self._call_on_ui_thread(
                                self._append_response_segment, current_response._turn_container)
                            continue
                        except OllamaCancelled:
                            raise
                        except Exception as error:
                            print(f"Project form recovery failed: {error}")
                    reply = ("I couldn't complete that action because the model response did not contain a valid "
                             "app-tool call or ask_user form. Nothing was changed. Please try again, or provide "
                             "more detail if the request needs a choice.")
                    if self.messages and self.messages[-1].get("role") == "assistant":
                        self.messages.pop()
                    self.messages.append({"role": "assistant", "content": reply})
                if pending_form_activity[0] is not None:
                    final_form_status = (
                        "Options loaded · action started" if used_tools & self.ACTION_TOOLS
                        else "No form requested · response finished" if retried_missing_action
                        else "No form needed · response ready")
                    self._call_on_ui_thread(
                        self._finish_pending_form_activity, pending_form_activity[0],
                        final_form_status)
                    pending_form_activity[0] = None
                GLib.idle_add(self._finalize_response, current_response)
                return reply
            streamed_text.clear()
            round_tools = set()
            started_operation = None
            for call_index, call in enumerate(calls):
                client._check_cancelled()
                total_calls += 1
                if total_calls > 16:
                    raise OllamaError("The assistant requested too many app lookups. Please ask a more specific question.")
                call_key = self._tool_call_key(call)
                if call_key in executed_calls:
                    raise OllamaError("The assistant repeated the same app lookup. Please try asking again.")
                executed_calls.add(call_key)
                tool_name = (call.get("function") or {}).get("name", "")
                used_tools.add(tool_name)
                round_tools.add(tool_name)
                if tool_name == "ask_user" and pending_form_activity[0] is not None:
                    self._call_on_ui_thread(
                        self._finish_pending_form_activity, pending_form_activity[0],
                        "Form requested", False)
                    pending_form_activity[0] = None
                activity = streamed_tool_activities.get(self._streamed_tool_index(call, call_index))
                output = self._run_tool_call(call, tools, current_response._turn_container,
                                             activity_view=activity)
                failure = self._tool_failure_message(output)
                if failure:
                    tool_failures.append((tool_name, failure))
                else:
                    if tool_name in self.ACTION_TOOLS:
                        successful_action_tools.add(tool_name)
                    try:
                        tool_result = json.loads(output)
                    except (TypeError, ValueError):
                        tool_result = {}
                    status = str(tool_result.get("status", "")).lower()
                    result_state = str(tool_result.get("result", "")).lower()
                    if status in {"started", "start_requested", "running", "in_progress", "queued"} or (
                            tool_name == "create_project" and result_state == "started"):
                        started_operation = (tool_name, tool_result)
                try:
                    action_cancelled = json.loads(output).get("status") == "cancelled"
                except (TypeError, ValueError, AttributeError):
                    pass
            if getattr(self.bridge, "restart_after_turn", False):
                reply = "The Library location was changed. Catalyst Lab will restart now."
                GLib.idle_add(self._finalize_response, current_response)
                return reply
            if action_cancelled:
                reply = "Cancelled; no changes were made."
                self.messages.append({"role": "assistant", "content": reply})
                GLib.idle_add(self._finalize_response, current_response)
                return reply
            if started_operation is not None:
                tool_name, tool_result = started_operation
                state = str(tool_result.get("status") or tool_result.get("result") or "started").lower()
                project_name = str(tool_result.get("project") or "").strip()
                if state == "queued":
                    reply = f"The {tool_name.replace('_', ' ')} for {project_name or 'the requested item'} is queued. You can follow its progress in Operations."
                else:
                    descriptions = {
                        "start_build": "The build",
                        "create_stage": "Stage creation",
                        "create_project": "Project creation",
                        "create_snapshot": "Snapshot creation",
                        "create_stage_image": "Stage image creation",
                        "deploy_stage_to_target": "Stage deployment",
                        "deploy_live_iso_to_disk": "Live ISO deployment",
                        "start_vm": "The VM",
                    }
                    subject = descriptions.get(tool_name, tool_name.replace("_", " ").capitalize())
                    reply = f"{subject}{f' for {project_name}' if project_name else ''} has started. You can follow its progress in Operations."
                self.messages.append({"role": "assistant", "content": reply})
                if pending_form_activity[0] is not None:
                    self._call_on_ui_thread(
                        self._finish_pending_form_activity, pending_form_activity[0],
                        "Action started · follow progress in Operations")
                    pending_form_activity[0] = None
                self._call_on_ui_thread(self._append_response_text, current_response, reply)
                GLib.idle_add(self._finalize_response, current_response)
                return reply
            if "list_options" in round_tools and "ask_user" not in round_tools:
                if pending_form_activity[0] is not None:
                    self._call_on_ui_thread(
                        self._finish_pending_form_activity, pending_form_activity[0],
                        "Options loaded · choosing the next step…", False)
                pending_form_activity[0] = self._call_on_ui_thread(
                    self._create_activity_view, current_response._turn_container,
                    "Options loaded · choosing what to ask…")
            current_response = self._call_on_ui_thread(
                self._append_response_segment, current_response._turn_container)
        # A long but valid workflow can need more tool rounds than the per-turn
        # safety limit. Let the user decide whether to continue, and keep the
        # existing conversation/tool results as context for the next chunk.
        continuation_call = {"function": {"name": "ask_user", "arguments": {
            "title": "Continue this task?",
            "message": ("The assistant has made 16 consecutive tool requests without "
                        "finishing. Would you like it to continue with the same request?"),
            "fields": [{
                "id": "continue",
                "label": "Continue",
                "type": "select",
                "required": True,
                "default": "stop",
                "options": [
                    {"value": "continue", "label": "Continue"},
                    {"value": "stop", "label": "Stop here"},
                ],
            }],
        }}}
        self.messages.append({"role": "assistant", "tool_calls": [continuation_call]})
        self._run_tool_call(continuation_call, tools, current_response._turn_container)
        try:
            continuation_result = json.loads(self.messages[-1].get("content", "{}"))
        except (TypeError, ValueError):
            continuation_result = {}
        if (continuation_result.get("status") == "submitted" and
                continuation_result.get("answers", {}).get("continue") == "continue"):
            return self._run_flow_turn(client, "Continue.", response_label)

        reply = "Stopped after 16 tool requests. No further actions were run."
        self.messages.append({"role": "assistant", "content": reply})
        GLib.idle_add(self._append_response_text, current_response, reply)
        GLib.idle_add(self._finalize_response, current_response)
        return reply

    def _cancel_active_user_form(self):
        if self._active_form_cancel is not None:
            self._active_form_cancel()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _tool_failure_message(output) -> str:
        """Return a useful failure detail for plain and structured tool errors."""
        text = str(output or "")
        if text.startswith(("Tool failed:", "Unknown tool:")):
            return text
        try:
            result = json.loads(text)
        except (TypeError, ValueError):
            return ""
        if not isinstance(result, dict) or str(result.get("status", "")).lower() not in {
                "failed", "failure", "error"}:
            return ""
        detail = result.get("error") or result.get("message") or text
        return str(detail)[:2000]

    @staticmethod
    def _is_project_creation_request(prompt: str) -> bool:
        text = str(prompt or "").lower()
        return bool(re.search(r"\b(create|start|make|set\s+up|new)\b.{0,50}\bprojects?\b|\bprojects?\b.{0,50}\b(create|start|make|set\s+up)\b", text))

    def _recover_template_project_from_form_history(self, tools: dict,
                                                    turn_container: Gtk.Box) -> str | None:
        """Complete a template project when the model goes silent after submitted forms."""
        submitted_forms = []
        for index, message in enumerate(self.messages[:-1]):
            if message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls") or []
            ask_call = next((call for call in calls
                             if (call.get("function") or {}).get("name") == "ask_user"), None)
            if ask_call is None or index + 1 >= len(self.messages):
                continue
            tool_result = self.messages[index + 1]
            if tool_result.get("role") != "tool" or tool_result.get("tool_name") != "ask_user":
                continue
            try:
                form_result = json.loads(tool_result.get("content", "{}"))
            except (TypeError, ValueError):
                continue
            if form_result.get("status") == "submitted":
                submitted_forms.append(((ask_call.get("function") or {}).get("arguments") or {}, form_result))

        if not submitted_forms or any(result.get("user_feedback") for _, result in submitted_forms):
            return None
        answers = {}
        for _, result in submitted_forms:
            answers.update(result.get("answers") or {})
        aliases = {
            "name": ("name", "project_name"),
            "source": ("source", "source_type", "project_source"),
            "template": ("template", "project_template"),
            "architecture": ("architecture",),
            "releng_directory": ("releng_directory", "releng_dir", "releng"),
            "snapshot": ("snapshot", "snapshot_id"),
            "toolset": ("toolset",),
        }
        normalized = {key: next((answers[name] for name in names if name in answers), None)
                      for key, names in aliases.items()}
        if str(normalized["source"] or "").lower() != "template":
            return None
        if not all(normalized[key] not in (None, "")
                   for key in ("name", "template", "releng_directory", "snapshot", "toolset")):
            return None

        # The form that configures the selected template defines its variable IDs.
        # Use its field list so shared project settings never leak into template_options.
        reserved = {name for names in aliases.values() for name in names}
        reserved.add("repository_url")
        template_form_fields = []
        template_form_score = 0
        for arguments, _ in submitted_forms:
            title = str(arguments.get("title", "")).lower()
            candidate_fields = arguments.get("fields") or []
            if "template" in title:
                template_form_fields = candidate_fields
                template_form_score = len(candidate_fields) + 100
                continue
            candidate_score = sum(
                isinstance(field, dict) and str(field.get("id", "")) in answers
                and str(field.get("id", "")) not in reserved
                for field in candidate_fields
            )
            if candidate_score > template_form_score:
                template_form_fields = candidate_fields
                template_form_score = candidate_score
        template_options = {
            str(field.get("id")): answers[field["id"]]
            for field in template_form_fields
            if isinstance(field, dict) and field.get("id") in answers
            and (str(field.get("id")) not in reserved or
                 str(field.get("id")) in getattr(self.bridge, "_last_template_variable_options", {}))
        }
        create_arguments = {
            "name": normalized["name"], "source": "template",
            "template": normalized["template"], "template_options": template_options,
            "releng_directory": normalized["releng_directory"],
            "snapshot": normalized["snapshot"], "toolset": normalized["toolset"],
        }
        if normalized["architecture"] not in (None, ""):
            create_arguments["architecture"] = normalized["architecture"]
        call = {"function": {"name": "create_project", "arguments": create_arguments}}
        self.messages.append({"role": "assistant", "tool_calls": [call]})
        self._run_tool_call(call, tools, turn_container)
        output = self.messages[-1].get("content", "")
        if str(output).startswith("Tool failed:"):
            return str(output)
        try:
            result = json.loads(output)
        except (TypeError, ValueError):
            return "The project creation tool returned an unreadable result."
        if result.get("result") != "started":
            return str(output)
        return (f'Project “{result["project"]}” creation has started. '
                "You can follow its progress in Operations.")

    @staticmethod
    def _requires_app_action(prompt: str) -> bool:
        """Recognize direct action requests so a tool-free promise is never treated as completion."""
        text = re.sub(r"\s+", " ", str(prompt).strip().lower())
        if re.search(r"\b(?:how to|how do i|explain|what is|what does|should i|would it)\b", text):
            return False
        # Clarifications commonly omit the action verb (for example, after
        # resolving an ambiguity: "I meant new project"). Still route these
        # through the project form/recovery path instead of accepting prose.
        if OllamaChatView._is_project_creation_request(text):
            return True
        verb = r"(?:create|make|start|build|rebuild|run|add|assign|link|unlink|delete|remove|rename|update|change|edit|set|configure|install|upgrade|download|deploy|mount|unmount|commit|generate|import|export|cancel|stop|open|pause|resume|refresh|sync|fetch|clone|copy|move|reorder|save)"
        prefix = r"(?:(?:let'?s|please|i want to|i'd like to|i need to|(?:can|could|would) you)\s+)?"
        return bool(re.match(rf"^{prefix}{verb}\b", text))

    @staticmethod
    def _is_capability_explanation(reply: str) -> bool:
        return bool(re.search(r"\b(?:cannot|can't|not supported|not available|don't have a tool|no tool)\b",
                              str(reply), re.IGNORECASE))

    @staticmethod
    def _tool_call_key(call: dict) -> str:
        function = call.get("function") or {}
        arguments = function.get("arguments") or {}
        return repr((function.get("name", ""), arguments))

    @staticmethod
    def _normalize_tool_call_shape(calls) -> list[dict]:
        """Unwrap list-wrapped tool calls some Ollama models return inconsistently."""
        normalized = []
        pending = list(calls) if isinstance(calls, list) else [calls]
        while pending:
            call = pending.pop(0)
            if isinstance(call, list):
                pending[:0] = call
                continue
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, list):
                # A few local model adapters wrap the one function object in a
                # list even though Ollama's call schema requires an object.
                pending[:0] = [dict(call, function=item) for item in function
                               if isinstance(item, dict)]
                continue
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name.strip():
                normalized.append(call)
        return normalized

    def _sanitize_tool_call_history(self):
        """Remove textual tool envelopes before replaying assistant history to Ollama.

        A streamed call can be recovered from partial tokens even when its
        assistant content contains an unclosed XML wrapper. Ollama's template
        parser then rejects the next request while reparsing that history.
        Structured `tool_calls` entries are preserved; only their duplicate
        textual protocol form is removed.
        """
        patterns = (
            r"<tool_call\b[^>]*>.*?(?:</tool_call\s*>|$)",
            r"<\|tool_call\s*>.*?(?:<tool_call\|>|$)",
            r"<function=[A-Za-z_][A-Za-z0-9_]*\b.*?(?:</function\s*>|$)",
            r"\btool[-_]call\s*=\s*\{.*",
        )
        for message in self.messages:
            if message.get("role") != "assistant":
                continue
            for key in ("content", "thinking"):
                value = message.get(key)
                if not isinstance(value, str) or not value:
                    continue
                cleaned = value
                for pattern in patterns:
                    cleaned = re.sub(pattern, "", cleaned,
                                     count=1, flags=re.IGNORECASE | re.DOTALL)
                message[key] = cleaned.strip()

    @staticmethod
    def _streamed_tool_index(call: dict, fallback: int) -> int:
        try:
            return int(call.get("index", fallback))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _collect_streamed_tool_calls(previews: dict[int, dict]) -> list[dict]:
        """Rebuild calls from partial stream events when Ollama omits them at done."""
        calls = []
        for index, partial in sorted(previews.items()):
            name = str(partial.get("name") or "").strip()
            if not name:
                continue
            arguments = partial.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            if not isinstance(arguments, dict):
                continue
            calls.append({"index": index, "function": {
                "name": name, "arguments": arguments,
            }})
        return calls

    @staticmethod
    def _normalize_tool_arguments(name: str, arguments, tool):
        """Decode JSON-encoded structured arguments using the registered schema."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as error:
                raise ValueError(f"{name} arguments are not valid JSON: {error.msg}") from error
        if not isinstance(arguments, dict):
            raise ValueError(f"{name} arguments must be a JSON object")
        schema = getattr(tool, "parameters", {}) or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        normalized = dict(arguments)
        for key, value in normalized.items():
            property_schema = properties.get(key, {})
            expected = property_schema.get("type") if isinstance(property_schema, dict) else None
            if isinstance(expected, list):
                expected = next((item for item in expected if item in ("array", "object")), None)
            if isinstance(value, str) and expected in ("array", "object"):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError as error:
                    raise ValueError(f"{name}.{key} must be a JSON {expected}: {error.msg}") from error
                if expected == "array" and not isinstance(decoded, list):
                    raise ValueError(f"{name}.{key} must be a JSON array")
                if expected == "object" and not isinstance(decoded, dict):
                    raise ValueError(f"{name}.{key} must be a JSON object")
                normalized[key] = decoded
        return normalized

    @staticmethod
    def _extract_tool_calls(content: str) -> tuple[list[dict], str]:
        """Parse app-tool envelopes emitted by supported Ollama model families."""
        content = str(content or "")
        gemma_calls, clean_content = OllamaChatView._extract_gemma_tool_calls(content)
        xml_calls, clean_content = OllamaChatView._extract_tool_call_markup(clean_content)
        return gemma_calls + xml_calls, clean_content

    @staticmethod
    def _recover_complete_streamed_tool_call(response_view: Gtk.Widget) -> dict | None:
        """Recover one complete call when Ollama EOFs after its JSON but before wrapper closure."""
        token_label = getattr(response_view, "_tool_preparation_tokens", None)
        raw = token_label.get_text() if token_label is not None else ""
        if not raw:
            raw = str(getattr(response_view, "_tool_call_text", "") or "")
        if "<tool_call>" not in raw:
            return None

        parsed_calls, _ = OllamaChatView._extract_tool_calls(raw)
        if parsed_calls:
            return parsed_calls[0]

        payload = raw.split("<tool_call>", 1)[1].lstrip()
        if not payload.startswith("{"):
            return None
        try:
            value, _end = json.JSONDecoder().raw_decode(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        name = value.get("name")
        arguments = value.get("arguments")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or not isinstance(arguments, dict)):
            return None
        return {"index": 0, "function": {"name": name, "arguments": arguments}}

    @staticmethod
    def _contains_text_tool_envelope(content: str) -> bool:
        content = str(content or "")
        return ("<tool_call>" in content or "<|tool_call>" in content
                or bool(re.search(r"\btool[-_]call\s*=\s*\{", content, re.IGNORECASE)))

    @staticmethod
    def _extract_gemma_tool_calls(content: str) -> tuple[list[dict], str]:
        """Parse Gemma's `<|tool_call>call:name{...}<tool_call|>` text protocol."""
        calls = []
        visible = []
        cursor = 0
        opening, closing = "<|tool_call>", "<tool_call|>"
        while True:
            start = content.find(opening, cursor)
            if start < 0:
                visible.append(content[cursor:])
                break
            visible.append(content[cursor:start])
            end = content.find(closing, start + len(opening))
            if end < 0:
                visible.append("[Incomplete Gemma tool call; no action was run.]")
                break
            payload = content[start + len(opening):end].strip()
            match = re.match(r"call:([A-Za-z_][A-Za-z0-9_]*)\s*", payload)
            try:
                if not match:
                    raise ValueError("Missing function name")
                name = match.group(1)
                parser = _GemmaCallArguments(payload[match.end():])
                arguments = parser.parse()
                if parser.position != len(parser.source):
                    raise ValueError("Unexpected trailing data")
                calls.append({"index": len(calls), "function": {
                    "name": name, "arguments": arguments,
                }})
            except (ValueError, TypeError, json.JSONDecodeError):
                visible.append("[Malformed Gemma tool call; no action was run.]")
            cursor = end + len(closing)
        return calls, "".join(visible)

    @staticmethod
    def _extract_tool_call_markup(content: str) -> tuple[list[dict], str]:
        """Parse JSON envelopes and legacy Qwen-style markup; keep malformed text visible."""
        calls = []
        content = str(content or "")
        # Qwen sometimes emits the same parameter closer twice immediately
        # before closing the function (often after an empty ask_user fields
        # value). Collapse only this redundant boundary sequence so it cannot
        # break parsers that consume the legacy markup; tool arguments are
        # still validated below and empty/invalid forms remain rejected.
        content = re.sub(
            r"(?:\s*</parameter\s*>){2,}(?=\s*</function\s*>)",
            "</parameter>", content, flags=re.IGNORECASE)
        visible = []
        cursor = 0
        opening = re.compile(r"<tool_call>")
        while match := opening.search(content, cursor):
            visible.append(content[cursor:match.start()])
            payload_start = match.end()
            while payload_start < len(content) and content[payload_start].isspace():
                payload_start += 1
            if payload_start < len(content) and content[payload_start] == "{":
                wrapper_end = re.search(r"</tool_call\s*>", content[payload_start:])
                if wrapper_end is None:
                    visible.append("[Incomplete <tool_call>; no action was run.]")
                    cursor = len(content)
                    break
                payload_end = payload_start + wrapper_end.start()
                try:
                    payload = json.loads(content[payload_start:payload_end].strip())
                    name = payload.get("name") if isinstance(payload, dict) else None
                    arguments = payload.get("arguments") if isinstance(payload, dict) else None
                    if (not isinstance(name, str) or not re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]*", name)
                            or not isinstance(arguments, dict)):
                        raise ValueError("tool call must contain a name and arguments object")
                    calls.append({"index": len(calls), "function": {
                        "name": name, "arguments": arguments,
                    }})
                except (json.JSONDecodeError, ValueError, TypeError):
                    visible.append("[Malformed <tool_call>; no action was run.]")
                cursor = payload_start + wrapper_end.end()
                continue
            # Some local models duplicate the angle bracket at the start of
            # the function marker (``<<function=...>``). Accept that harmless
            # prefix typo here; the closing boundary and every argument are
            # still validated below before dispatch.
            function_match = re.search(
                r"<+function=([A-Za-z_][A-Za-z0-9_]*)>", content[match.end():], re.DOTALL)
            if not function_match:
                visible.append("[Malformed <tool_call>; no action was run.]")
                cursor = match.end()
                continue
            name = function_match.group(1)
            arguments_start = match.end() + function_match.end()
            # Qwen occasionally closes the outer envelope without emitting
            # </function>, or places a parameter closer after it. Treat the
            # tool_call boundary as the function boundary when necessary.
            function_end = re.search(
                r"</function\s*>|(?=</tool_call\s*>)",
                content[arguments_start:], re.DOTALL)
            if not function_end:
                visible.append("[Incomplete <tool_call>; no action was run.]")
                cursor = len(content)
                break
            arguments_end = arguments_start + function_end.start()
            arguments = {}
            body = content[arguments_start:arguments_end]
            body = OllamaChatView._repair_missing_parameter_separator(body)
            # Some models emit a parameter name as element text and then wrap
            # its value in a second closing tag, e.g.
            # ``<parameter>project</parameter> my-project </parameter>``.
            # Normalize the name marker to the regular named-parameter form;
            # the existing value decoder still validates JSON and scalar data.
            body = re.sub(
                r"<parameter>\s*([A-Za-z_][A-Za-z0-9_]*)\s*</parameter>",
                r"<parameter=\1>", body,
            )
            # Some local models pluralize every parameter closer as
            # ``</parameters>``. Normalize that one known typo before reading
            # values, otherwise the closer becomes part of the JSON payload
            # (for example ``false</parameters>``) and makes the whole call
            # unparsable. The argument names and values remain schema-checked
            # by the normal parser and tool bridge.
            body = re.sub(r"</parameters\s*>", "</parameter>", body)
            # A recurring model error is an orphan parameter close immediately
            # after the function name. Strip only leading orphan closers; later
            # closers remain subject to the normal argument parser.
            body = re.sub(r"^(?:\s*</parameters?>)+\s*", "", body)
            # Local models occasionally drop the `=` after `parameter` and
            # emit `<parameter>categories>...`. Accept that narrowly malformed
            # spelling as well as the documented and inline forms, then still
            # require the value to decode as JSON before executing anything.
            parameter_open = re.compile(
                r"\s*<parameter=([A-Za-z_][A-Za-z0-9_]*)(>|=)|"
                r"\s*<parameter>([A-Za-z_][A-Za-z0-9_]*)>")
            body_cursor = 0
            malformed = False
            while body_cursor < len(body):
                # Ignore whitespace before checking for the one known malformed
                # pattern: an extra parameter closer emitted between arguments.
                while body_cursor < len(body) and body[body_cursor].isspace():
                    body_cursor += 1
                # Small local models sometimes duplicate a parameter close
                # between arguments or before </function>. Ignore this one
                # redundant delimiter; argument values are still JSON-decoded
                # and the named function must still close correctly.
                stray_closer = re.match(r"</parameters?>", body[body_cursor:])
                if stray_closer:
                    # Recover the common pluralized orphan closer emitted by
                    # local models before the first argument. It carries no
                    # value and cannot make an otherwise invalid call valid.
                    body_cursor += stray_closer.end()
                    continue
                item = parameter_open.match(body, body_cursor)
                if item is None:
                    if body[body_cursor:].strip():
                        malformed = True
                    break
                key = item.group(1) or item.group(3)
                separator = item.group(2) or ">"
                value_start = item.end()
                closing = body.find("</parameter>", value_start)
                next_parameter = re.search(
                    r"<parameter(?:=[A-Za-z_][A-Za-z0-9_]*(?:>|=)|"
                    r">[A-Za-z_][A-Za-z0-9_]*>)", body[value_start:])
                next_start = (value_start + next_parameter.start()
                              if next_parameter else len(body))
                if separator == "=":
                    # Also accept the model's common inline form:
                    # <parameter=title="..." and <parameter=fields=[...]
                    # This is particularly important for ask_user schemas.
                    raw_value = body[value_start:min(next_start, closing)
                                      if closing >= 0 else next_start].strip()
                    if raw_value.endswith("</parameter>"):
                        raw_value = raw_value[:-len("</parameter>")].rstrip()
                    try:
                        arguments[key] = json.loads(raw_value)
                    except json.JSONDecodeError:
                        malformed = True
                        break
                    body_cursor = next_start
                    if closing >= 0 and closing < next_start:
                        body_cursor = closing + len("</parameter>")
                else:
                    # The model occasionally omits </parameter>. Since this
                    # parser runs only after </function> has arrived, the next
                    # parameter or function boundary can safely terminate the
                    # value. JSON validation below still rejects incomplete or
                    # ambiguous arguments before any tool is invoked.
                    value_end = closing if closing >= 0 and closing < next_start else next_start
                    raw_value = body[value_start:value_end]
                    if key in arguments:
                        malformed = True
                        break
                    try:
                        arguments[key] = json.loads(raw_value.strip())
                    except json.JSONDecodeError:
                        # Some local models omit JSON quotes around simple
                        # string arguments (for example
                        # <parameter=query>build</parameter>). Recover only a
                        # plain scalar here; objects, arrays, quoted strings,
                        # and tag-like text must still be valid JSON.
                        plain_value = raw_value.strip()
                        if (plain_value and plain_value not in {"true", "false", "null"}
                                and not re.search(r'[<>"\\\[\]{}\n\r]', plain_value)):
                            arguments[key] = plain_value
                        else:
                            malformed = True
                            break
                    body_cursor = (closing + len("</parameter>")
                                   if closing >= 0 and closing < next_start else next_start)
                if key in arguments:
                    # Inline decoding inserted the key above; regular syntax
                    # inserted it in its branch. Duplicate names are invalid.
                    occurrences = re.findall(
                        rf"<parameter(?:={re.escape(key)}(?:>|=)|"
                        rf">{re.escape(key)}>)", body)
                    if len(occurrences) > 1:
                        malformed = True
                        break
            if name == "ask_user" and (malformed or not arguments.get("fields")):
                # Some local models switch to child-tag output inside this
                # pseudo-XML protocol (for example <fields>[...]</fields>).
                # Recover only the known ask_user keys; never send this text to
                # an XML parser and never treat a partial JSON field list as an
                # executable call.
                compatible = OllamaChatView._parse_ask_user_child_tags(body)
                if compatible.get("fields"):
                    arguments.update(compatible)
                    malformed = False
            if arguments and not malformed:
                calls.append({"index": len(calls), "function": {
                    "name": name, "arguments": arguments,
                }})
            else:
                visible.append("[Malformed <tool_call>; no action was run.]")
            cursor = arguments_start + function_end.end()
            wrapper_end = re.match(
                r"(?:\s*</parameter>)*\s*</tool_call\s*>", content[cursor:])
            if wrapper_end:
                cursor += wrapper_end.end()
        visible.append(content[cursor:])
        return calls, "".join(visible)

    def _stream_tool_call_progress(self, event: dict, turn_container: Gtk.Box,
                                   activities: dict[int, Gtk.Expander],
                                   pending_form_activity: list, previews: dict[int, dict],
                                   tools: dict):
        """Render only tool-call text fragments Ollama actually exposes in its stream."""
        message = event.get("message") or {}
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            return
        if calls:
            response = getattr(turn_container, "_latest_response", None)
            if response is not None:
                self._call_on_ui_thread(
                    self._set_thinking_status, response, "Preparing tool call…")
        for position, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            index = self._streamed_tool_index(call, position)
            function = call.get("function") or {}
            previous = previews.setdefault(index, {"name": "", "arguments": {}})
            name_fragment = str(function.get("name") or "")
            name = self._merge_stream_text(previous["name"], name_fragment)
            previous["name"] = name
            arguments = function.get("arguments")
            if arguments is not None:
                previous["arguments"] = self._merge_stream_value(previous["arguments"], arguments)
            display_arguments = previous["arguments"]
            try:
                display_arguments = self._normalize_tool_arguments(
                    name.strip(), display_arguments, tools.get(name.strip()))
            except ValueError:
                pass  # Keep incomplete streamed fragments visible until they form valid JSON.
            if response is not None and position == 0:
                self._call_on_ui_thread(self._update_tool_preparation, response,
                                        name.strip(), display_arguments)
            # Ollama's API provides parsed tool-call fields, not raw model tokens.
            # Show any string argument fragments verbatim when a model/server does
            # stream them. Do not stringify the parsed call envelope: that creates
            # JSON the model never emitted and makes a completed call look like a
            # token-by-token transcript.
            raw_arguments = function.get("arguments")
            if (response is not None and isinstance(raw_arguments, str) and raw_arguments
                    and not getattr(response, "_has_generated_token_stream", False)):
                old_raw = previous.get("_raw_arguments", "")
                new_raw = self._merge_stream_text(old_raw, raw_arguments)
                previous["_raw_arguments"] = new_raw
                fragment = new_raw[len(old_raw):] if new_raw.startswith(old_raw) else raw_arguments
                if fragment:
                    self._call_on_ui_thread(self._append_tool_call_text, response, fragment)
            name = name.strip()
            if not name:
                continue
            if index in activities:
                continue
            # Creating the row synchronously from the chat worker keeps the widget
            # reference available so the final tool result can update this same row.
            if name == "ask_user" and pending_form_activity[0] is not None:
                activity = pending_form_activity[0]
                pending_form_activity[0] = None
                self._call_on_ui_thread(
                    self._set_activity_status, activity, "Preparing your form…")
            else:
                activity = self._call_on_ui_thread(
                    self._create_activity_view, turn_container,
                    self._tool_activity_status(name, {}))
            activities[index] = activity
        if calls and event.get("done"):
            response = getattr(turn_container, "_latest_response", None)
            if response is not None:
                first_call = calls[0] if calls and isinstance(calls[0], dict) else {}
                function = first_call.get("function") or {}
                self._call_on_ui_thread(self._update_tool_preparation, response,
                                        str(function.get("name") or ""),
                                        function.get("arguments") or {})
                self._call_on_ui_thread(self._collapse_turn_thinking, response)

    @staticmethod
    def _merge_stream_text(previous: str, fragment: str) -> str:
        if not fragment or previous == fragment or previous.startswith(fragment):
            return previous
        if fragment.startswith(previous):
            return fragment
        return previous + fragment

    @classmethod
    def _merge_stream_value(cls, previous, fragment):
        """Accept both cumulative JSON fragments and token-by-token string fragments."""
        if isinstance(previous, dict) and isinstance(fragment, dict):
            merged = dict(previous)
            for key, value in fragment.items():
                merged[key] = cls._merge_stream_value(merged.get(key), value)
            return merged
        if isinstance(previous, list) and isinstance(fragment, list):
            overlap = min(len(previous), len(fragment))
            merged = [cls._merge_stream_value(previous[index], fragment[index])
                      for index in range(overlap)]
            if len(fragment) > overlap:
                merged.extend(fragment[overlap:])
            elif len(previous) > overlap:
                merged.extend(previous[overlap:])
            return merged
        if isinstance(previous, str) and isinstance(fragment, str):
            return cls._merge_stream_text(previous, fragment)
        return fragment

    def _run_tool_call(self, call: dict, tools: dict, turn_container: Gtk.Box,
                       activity_view: Gtk.Expander | None = None):
        if self._turn_cancel_event is not None and self._turn_cancel_event.is_set():
            raise OllamaCancelled("Stopped by user")
        function = call.get("function") or {}
        name = function.get("name", "")
        arguments = function.get("arguments") or {}
        self._record_debug_event(
            "TOOL CALL", text=f"Tool: {name}\nArguments:\n" + self._debug_format_value(
                self._debug_redact(arguments)))
        tool = tools.get(name)
        normalization_error = None
        try:
            arguments = self._normalize_tool_arguments(name, arguments, tool)
        except ValueError as error:
            normalization_error = error
            arguments = {}
        if activity_view is None:
            activity_view = self._call_on_ui_thread(
                self._create_activity_view, turn_container,
                self._tool_activity_status(name, arguments))
        else:
            self._call_on_ui_thread(
                self._set_activity_status, activity_view,
                self._tool_activity_status(name, arguments))
        if name == "ask_user":
            self._active_form_activity = activity_view
        # Let GTK paint the visible in-progress row before invoking a tool. Some
        # app actions synchronously marshal work to the GTK thread; without this
        # frame boundary, the first repaint can be delayed until that work ends.
        self._wait_for_activity_paint(activity_view)
        if normalization_error is not None:
            output = f"Tool failed: {normalization_error}"
        elif tool is None:
            output = f"Unknown tool: {name}"
        else:
            try:
                if name in ("pull_ollama_model", "change_library_location"):
                    self.bridge.tool_progress_callback = lambda model, event: self._call_on_ui_thread(
                        self._update_model_pull_activity, activity_view, model, event)
                output = tool.handler(arguments)
            except OllamaCancelled:
                raise
            except Exception as error:
                output = f"Tool failed: {error}"
            finally:
                self.bridge.tool_progress_callback = None
        self._call_on_ui_thread(self._append_tool_action, activity_view, name, arguments, output)
        if name == "ask_user":
            self._active_form_activity = None
        model_output = output
        failure = self._tool_failure_message(output)
        if failure:
            self._record_debug_event("TOOL FAILURE", text=f"Tool: {name}\n{failure}")
            if tool is not None:
                definition = json.dumps({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }, ensure_ascii=False, separators=(",", ":"))[:12000]
            else:
                definition = "Tool definition unavailable. Find it with list_tools, then read_tool_schema."
            model_output += (
                "\n\nRETRY GUIDANCE: The app-tool call failed before it returned a successful result. "
                "Use this exact tool contract when correcting the call: " + definition +
                "\nExact error: " + failure[:1600] +
                "\nCorrect the arguments and retry this operation once if the error shows it did not run. "
                "If the error suggests partial side effects, inspect current state before retrying. "
                "Do not repeat the same invalid arguments or claim success without a successful result."
            )
        self._record_debug_event(
            "TOOL RESULT", text=f"Tool: {name}\nStatus: {'failed' if failure else 'returned'}\n"
            f"{self._debug_format_value(self._debug_redact(output))}")
        self.messages.append({"role": "tool", "tool_name": name, "content": model_output})
        return output

    @staticmethod
    def _debug_redact(value, key=""):
        """Hide common credentials in tool arguments and returned data."""
        sensitive = re.compile(r"password|passwd|secret|token|credential|private.?key|api.?key", re.I)
        if sensitive.search(str(key)):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(item_key): OllamaChatView._debug_redact(item, str(item_key))
                    for item_key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [OllamaChatView._debug_redact(item, key) for item in value]
        return value

    @staticmethod
    def _debug_format_value(value):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _record_debug_event(self, kind, text="", details="", channel=None, merge=False):
        if not self.debug_conversation_logging:
            return
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self._debug_events_lock:
            if merge and self._debug_events:
                previous = self._debug_events[-1]
                if previous.get("kind") == kind and previous.get("channel") == channel:
                    previous["text"] = previous.get("text", "") + str(text)
                    return
            self._debug_events.append({
                "kind": str(kind), "channel": channel, "time": timestamp,
                "text": str(text), "details": str(details),
            })

    @staticmethod
    def _set_activity_status(activity_view: Gtk.Expander, status: str):
        activity_view.set_label(status)
        activity_view.set_visible(True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _finish_streamed_tool_activity(activity_view: Gtk.Expander | None, status: str):
        if activity_view is None:
            return GLib.SOURCE_REMOVE
        activity_view.set_label(status)
        activity_view.set_expanded(False)
        OllamaChatView._collapse_tool_preparation_frames(
            getattr(activity_view, "_turn_container", None))
        for state in ("succeeded", "failed", "in-progress", "queued", "canceled"):
            activity_view.remove_css_class(f"chat-activity-{state}")
        activity_view.add_css_class("chat-activity-canceled")
        activity_view.set_visible(True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _update_model_pull_activity(activity_view: Gtk.Expander, model: str, event: dict):
        status = str(event.get("status", "Downloading"))
        total, completed = event.get("total"), event.get("completed", 0)
        if isinstance(total, (int, float)) and total > 0 and isinstance(completed, (int, float)):
            status += f" · {min(100, max(0, int(completed * 100 / total)))}%"
        activity_view.set_label(f"pull_ollama_model · In progress · {model}: {status}")
        activity_view.set_visible(True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _finish_pending_form_activity(activity_view: Gtk.Expander, status: str,
                                      succeeded: bool = True):
        activity_view.set_label(status)
        activity_view.set_expanded(False)
        if succeeded:
            activity_view.add_css_class("chat-activity-succeeded")
        activity_view.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _wait_for_activity_paint(self, activity_view: Gtk.Widget):
        painted = threading.Event()

        def arm_frame_wait():
            try:
                frame_clock = activity_view.get_frame_clock()
                if frame_clock is None or not activity_view.get_mapped():
                    painted.set()
                    return GLib.SOURCE_REMOVE

                handler_id = None

                def after_paint(_clock):
                    if handler_id is not None:
                        frame_clock.disconnect(handler_id)
                    painted.set()

                handler_id = frame_clock.connect("after-paint", after_paint)
                frame_clock.request_phase(Gdk.FrameClockPhase.PAINT)
                activity_view.queue_draw()
            except Exception:
                painted.set()
            return GLib.SOURCE_REMOVE

        def wait_for_layout_and_scroll():
            # _scroll_to_bottom waits briefly for GTK to allocate the newly
            # appended activity row. Arm the frame wait after that callback so
            # the row is both in view and painted before tool execution starts.
            GLib.timeout_add(50, arm_frame_wait)
            return GLib.SOURCE_REMOVE

        self._call_on_ui_thread(wait_for_layout_and_scroll)
        painted.wait(2)

    def _create_activity_view(self, turn_container: Gtk.Box, status: str) -> Gtk.Expander:
        expander = Gtk.Expander(label=status)
        expander.add_css_class("chat-activity")
        expander.set_expanded(True)
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        expander.set_child(actions)
        expander._actions = actions
        expander._turn_container = turn_container
        turn_container.append(expander)
        self._scroll_to_bottom()
        return expander

    def _append_tool_action(self, activity_view: Gtk.Expander, tool_name: str,
                            arguments: dict, output: str):
        description = self._tool_activity_description(tool_name, arguments, output)
        summary = description
        if tool_name == "ask_user":
            try:
                result = json.loads(output)
                state = result.get("status") if isinstance(result, dict) else None
            except (TypeError, ValueError):
                state = None
            fields = [str(field.get("label", "")) for field in arguments.get("fields", [])
                      if isinstance(field, dict) and field.get("label")]
            requested = (", ".join(fields) or str(arguments.get("title", "details")))[:80]
            summary = (f"Asked: {requested}" if state == "submitted" else
                       "Details form canceled" if state == "cancelled" else "Details form unavailable")
        outcome = self._tool_outcome(output)
        activity_view.set_label(f"{tool_name} · {summary} · {outcome}")
        activity_view.set_expanded(False)
        self._collapse_tool_preparation_frames(activity_view._turn_container)
        for state in ("succeeded", "failed", "in-progress", "queued", "canceled", "submitted", "no-change"):
            activity_view.remove_css_class(f"chat-activity-{state}")
        activity_view.add_css_class(f"chat-activity-{outcome.lower().replace(' ', '-')}")
        activity_view.set_visible(True)
        detail = Gtk.Label(label=description, xalign=0)
        detail.add_css_class("dimmed")
        detail.set_wrap(True)
        activity_view._actions.append(detail)
        result_heading = Gtk.Label(label="Result", xalign=0)
        result_heading.add_css_class("heading")
        activity_view._actions.append(result_heading)
        result_text = str(output or "").strip()
        try:
            result_text = json.dumps(json.loads(result_text), ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            pass
        if tool_name != "create_toolset" and len(result_text) > 1600:
            result_text = result_text[:1600] + "… (truncated; full result remains in assistant context)"
        result_label = Gtk.Label(label=result_text or "No result text.", xalign=0, yalign=0,
                                 wrap=True, selectable=True)
        result_label.add_css_class("monospace")
        if outcome == "Failed":
            result_label.add_css_class("error")
        if tool_name == "create_toolset":
            raw_output = Gtk.Expander(label="Raw output")
            raw_output.add_css_class("dim-label")
            raw_output.set_expanded(False)
            raw_output.set_child(result_label)
            activity_view._actions.append(raw_output)
        else:
            activity_view._actions.append(result_label)
        self._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _call_on_ui_thread(callback, *args):
        """Run a GTK operation on the main loop and return its result to a worker."""
        completed = threading.Event()
        outcome = {}

        def invoke():
            try:
                outcome["result"] = callback(*args)
            except Exception as error:
                outcome["error"] = error
            finally:
                completed.set()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(invoke)
        if not completed.wait(60):
            raise OllamaError("Timed out while updating the chat view")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result")

    @staticmethod
    def _tool_activity_description(tool_name: str, arguments: dict | None = None,
                                   output: str = "") -> str:
        if tool_name == "ask_user":
            arguments = arguments or {}
            title = str(arguments.get("title", "Details"))
            labels = [str(field.get("label", "")) for field in arguments.get("fields", [])
                      if isinstance(field, dict) and field.get("label")]
            try:
                result = json.loads(output)
            except (TypeError, ValueError):
                result = {}
            state = result.get("status") if isinstance(result, dict) else None
            outcome = "answers submitted" if state == "submitted" else "canceled" if state == "cancelled" else "could not be displayed"
            fields = ", ".join(labels)
            field_text = f" Fields: {fields}." if fields else ""
            return f"Displayed “{title}” form; {outcome}.{field_text}"
        return {
            "list_tools": "Found app tools",
            "read_tool_schema": "Read app tool schema",
            "list_documentation": "Read available documentation",
            "read_documentation": "Read documentation",
            "list_ollama_models": "Listed Ollama models",
            "pull_ollama_model": "Downloaded an Ollama model",
            "list_objects": "Read Library objects",
            "list_options": "Loaded available options",
            "ask_user": "Asked you for details",
            "list_sections": "Read available sections",
            "open_section": "Opened an app section",
            "open_project": "Opened a project",
            "open_object": "Opened object details",
            "get_git_status": "Read Git repository status",
            "update_git_directory": "Started Git repository update",
            "commit_git_changes": "Started saving Git changes",
            "discard_git_changes": "Started discarding Git changes",
            "rename_git_directory": "Renamed a Git directory",
            "delete_git_directory": "Deleted a Git directory",
            "create_git_directory": "Started Git directory creation",
            "create_vm_runner": "Started VM runner creation",
            "open_wizard": "Opened an app workflow",
            "list_operations": "Read background operations",
            "read_operation_log": "Read live operation output",
            "read_vm_output": "Read live VM output",
            "change_library_location": "Changed Library location",
            "stop_operation": "Stopped an operation",
            "control_vm": "Sent a control to the VM",
            "resume_build_queue": "Resumed the build queue",
            "update_settings": "Updated an app preference",
            "get_settings": "Read app preferences",
            "get_root_access_status": "Checked root access",
            "set_root_access": "Changing root access",
            "cancel_root_action": "Requesting root action cancellation",
            "open_root_action_output": "Opening root action output",
            "list_bugs": "Read failed package builds",
            "open_bugzilla": "Opened Gentoo Bugzilla",
            "set_bug_status": "Updated a failure status",
            "remove_bug": "Removed a failure from Bugs",
            "list_bug_notes": "Read issue notes",
            "add_bug_note": "Added an issue note",
            "edit_bug_note": "Edited an issue note",
            "delete_bug_note": "Deleted an issue note",
            "delete_project": "Deleted a project",
            "delete_snapshot": "Deleted an unused snapshot",
            "delete_packages_cache": "Deleted a binary package cache",
            "delete_runner": "Started VM runner deletion",
            "get_runner_status": "Read execution runner status",
            "set_runner_state": "Changed VM runner state",
            "update_runner": "Updated VM runner settings",
            "get_toolset_status": "Read toolset status",
            "set_toolset_mount": "Changed toolset mount state",
            "update_toolset": "Started toolset update",
            "configure_toolset": "Configured toolset applications",
            "rename_toolset": "Renamed a toolset",
            "delete_toolset": "Deleted a toolset",
            "create_project": "Creating a project",
            "rename_project": "Renamed a project",
            "update_project": "Updated project settings",
            "create_stage": "Started stage creation",
            "update_stage": "Updated a stage",
            "set_stage_parent": "Changed stage relationship",
            "read_stage_script": "Read stage script",
            "write_stage_script": "Wrote stage script",
            "set_stage_setting": "Updated a stage setting",
            "reset_stage_setting": "Reset a stage setting",
            "delete_stage": "Deleted a stage",
            "update_stage_kernel": "Updated stage kernel settings",
            "delete_stage_kernel": "Deleted a stage kernel",
            "read_object_file": "Read an app file",
            "list_object_files": "Listed object files",
            "write_object_file": "Wrote an object file",
            "list_builds": "Read build records",
            "delete_build_record": "Deleted a saved build record",
            "list_deployable_builds": "Read deployable builds",
            "create_live_iso": "Started live ISO creation",
            "create_stage_image": "Started stage image creation",
            "deploy_stage_to_target": "Started stage deployment",
            "create_packages_directory": "Created a binary-packages folder",
            "read_build_file": "Read a build file",
            "start_build": "Started or queued a project build",
            "start_vm": "Started a build in a virtual machine",
            "stop_build": "Requested build cancellation",
            "create_snapshot": "Started snapshot generation",
            "import_snapshot": "Started importing a snapshot",
            "create_toolset": "Started toolset installation",
            "get_stage_settings": "Read stage settings",
            "list_stage_kernels": "Read stage kernels",
            "list_repository_packages": "Browsed repository packages",
            "package_cache_stats": "Counted binary packages",
            "list_library_storage": "Read Library storage details",
            "delete_library_storage_item": "Deleted a Library Cache or Temporary item",
        }.get(tool_name, "Used app information")

    @staticmethod
    def _tool_outcome(output: str) -> str:
        """Return a short truthful state for the per-call activity row."""
        if str(output).startswith(("Tool failed:", "Unknown tool:")):
            return "Failed"
        try:
            result = json.loads(output)
        except (TypeError, ValueError):
            result = None
        if isinstance(result, dict):
            status = str(result.get("status") or result.get("result") or "").lower()
            if status in {"failed", "error"} or result.get("error"):
                return "Failed"
            if status == "partial":
                return "Partial"
            if status in {"queued", "pending"}:
                return "Queued"
            if status in {"started", "start_requested", "authorization_requested", "cancellation_requested", "running", "in_progress", "deletion_started"}:
                return "In progress"
            if status in {"cancelled", "canceled"}:
                return "Canceled"
            if status in {"submitted", "confirmed"}:
                return "Submitted"
            if status:
                return "Succeeded"
        plain_output = str(output).strip().lower()
        if plain_output.startswith(("error:", "failed:", "tool failed:", "unknown tool:")):
            return "Failed"
        if plain_output.startswith(("cancellation requested", "stop requested")):
            return "In progress"
        if plain_output.startswith("no build is currently running"):
            return "No change"
        if result is None and not str(output).strip():
            return "No result"
        return "Succeeded"

    @staticmethod
    def _tool_activity_status(tool_name: str, arguments: dict) -> str:
        if tool_name == "list_options":
            categories = arguments.get("categories") or []
            if "template_variables" in categories:
                return "Finding options for this template…"
            if "project_templates" in categories:
                return "Finding available project templates…"
            if "project_sources" in categories:
                return "Finding project source options…"
            if "git_directory_sources" in categories:
                return "Finding directory source options…"
            if "build_snapshots" in categories or "build_toolsets" in categories or "build_runners" in categories:
                return "Finding options for this build…"
            if "live_iso_options" in categories:
                return "Finding options for this live ISO…"
            if "stage_settings" in categories or "stage_setting_options" in categories or "stage_targets" in categories:
                return "Finding build stage options…"
            return "Finding available options…"
        status = {
            "list_documentation": "Finding relevant documentation…",
            "read_documentation": "Reading documentation…",
            "list_ollama_models": "Checking installed Ollama models…",
            "pull_ollama_model": "Starting model download…",
            "list_objects": "Checking your Library…",
            "ask_user": "Preparing your form…",
            "list_sections": "Checking available app sections…",
            "open_section": "Opening the requested section…",
            "open_project": "Opening the project…",
            "open_object": "Opening object details…",
            "get_git_status": "Reading Git status…",
            "update_git_directory": "Fetching and rebasing Git changes…",
            "commit_git_changes": "Saving Git changes…",
            "discard_git_changes": "Discarding Git changes…",
            "rename_git_directory": "Renaming a Git directory…",
            "delete_git_directory": "Deleting a Git directory…",
            "create_git_directory": "Creating a Git directory…",
            "create_vm_runner": "Creating a VM runner…",
            "open_wizard": "Opening the requested workflow…",
            "list_operations": "Checking background operations…",
            "list_tools": "Searching available app tools…",
            "read_tool_schema": "Reading app tool schema…",
            "read_operation_log": "Reading live operation output…",
            "read_vm_output": "Reading live virtual machine output…",
            "stop_operation": "Stopping the operation…",
            "control_vm": "Controlling the virtual machine…",
            "resume_build_queue": "Resuming queued builds…",
            "update_settings": "Updating the app preference…",
            "get_settings": "Reading app preferences…",
            "list_bugs": "Finding failed package builds…",
            "open_bugzilla": "Opening Gentoo Bugzilla…",
            "set_bug_status": "Updating the failure status…",
            "remove_bug": "Removing a failure…",
            "list_bug_notes": "Reading issue notes…",
            "add_bug_note": "Adding an issue note…",
            "edit_bug_note": "Editing an issue note…",
            "delete_bug_note": "Deleting an issue note…",
            "delete_project": "Deleting the project…",
            "delete_snapshot": "Deleting the snapshot…",
            "delete_packages_cache": "Deleting the package cache…",
            "delete_runner": "Deleting the VM runner…",
            "get_runner_status": "Checking execution runner status…",
            "set_runner_state": "Changing VM runner state…",
            "update_runner": "Updating VM runner settings…",
            "get_toolset_status": "Checking toolset status…",
            "set_toolset_mount": "Changing toolset mount state…",
            "update_toolset": "Starting the toolset update…",
            "configure_toolset": "Applying toolset application settings…",
            "rename_toolset": "Renaming a toolset…",
            "delete_toolset": "Deleting a toolset…",
            "create_project": "Starting project creation…",
            "rename_project": "Renaming the project…",
            "update_project": "Updating project settings…",
            "create_stage": "Adding the stage…",
            "update_stage": "Updating the stage…",
            "set_stage_setting": "Updating a stage setting…",
            "reset_stage_setting": "Resetting a stage setting…",
            "delete_stage": "Deleting the stage…",
            "update_stage_kernel": "Updating stage kernel settings…",
            "delete_stage_kernel": "Deleting the stage kernel…",
            "read_object_file": "Reading a project or Library file…",
            "list_object_files": "Finding files in the selected project or repository…",
            "write_object_file": "Preparing a confirmed file change…",
            "list_builds": "Finding recent build records…",
            "delete_build_record": "Deleting the saved build record…",
            "list_deployable_builds": "Finding completed builds that can be deployed…",
            "create_live_iso": "Preparing the live ISO file…",
            "create_stage_image": "Preparing the stage disk image…",
            "deploy_stage_to_target": "Preparing a validated disk deployment…",
            "create_packages_directory": "Creating a binary-packages folder…",
            "read_build_file": "Reading the build log…",
            "start_build": "Preparing and starting the build…",
            "start_vm": "Preparing the virtual machine…",
            "stop_build": "Stopping the build…",
            "create_snapshot": "Starting snapshot generation…",
            "import_snapshot": "Preparing to import the snapshot…",
            "create_toolset": "Preparing toolset installation…",
            "get_stage_settings": "Reading stage settings…",
            "list_stage_kernels": "Reading stage kernels…",
            "list_repository_packages": "Reading snapshot or overlay packages…",
            "package_cache_stats": "Counting binary packages…",
            "list_library_storage": "Scanning Library storage…",
            "delete_library_storage_item": "Deleting the Library item…",
            "change_library_location": "Changing the Library location…",
            "set_stage_parent": "Changing the stage relationship…",
            "read_stage_script": "Reading the stage script…",
            "write_stage_script": "Writing the stage script…",
        }.get(tool_name, "Working with Catalyst Lab…")
        return f"{tool_name} · In progress · {status}"

    @staticmethod
    def _stream_chunk(response_label: Gtk.Label, event: dict, streamed_text: list[str],
                      suppress_content: bool = False, tool_call_text: list[str] | None = None):
        chat_view = getattr(response_label, "_chat_view", None)

        def update_ui(callback, *args):
            if chat_view is not None:
                chat_view._call_on_ui_thread(callback, *args)
            else:
                GLib.idle_add(callback, *args)

        message = event.get("message") or {}
        logprobs = event.get("logprobs") or []
        calls = message.get("tool_calls") or []
        content = message.get("content", "")
        thinking = message.get("thinking") or event.get("thinking", "")
        has_logprobs = isinstance(logprobs, list) and bool(logprobs)
        logprob_tokens = [item["token"] for item in logprobs
                          if isinstance(item, dict) and isinstance(item.get("token"), str)]
        if has_logprobs:
            response_label._raw_streamed_text = True
            update_ui(chat_view._append_raw_output if chat_view is not None else
                      OllamaChatView._noop, "".join(logprob_tokens))
        elif thinking or content:
            response_label._raw_streamed_text = True
            if chat_view is not None:
                update_ui(chat_view._append_raw_output,
                          str(thinking or "") + str(content or ""))

        # Ollama may stream sampled tokens before it fills either channel field,
        # and some model templates put all generated text (including channel
        # markers) in this token stream. Use it as the live source regardless
        # of the model's advertised thinking support; parse markers to place
        # text, then use the channel fields only when they contain text that
        # was not already represented by sampled tokens.
        sampled_text = "".join(logprob_tokens)
        if sampled_text:
            response_label._used_logprob_preview = True
            response_label._logprob_preview_text = (
                getattr(response_label, "_logprob_preview_text", "") + sampled_text)
            if chat_view is not None:
                chat_view._record_debug_event("ASSISTANT TOKENS", sampled_text,
                                              channel="sampled tokens", merge=True)
            thinking_text = re.sub(
                r"</?think>|<\|channel>thought|<\|channel>|<channel\|>", "",
                str(thinking or ""))
            sampled_plain = re.sub(
                r"</?think>|<\|channel>thought|<\|channel>|<channel\|>", "",
                sampled_text)
            explicit_thinking_delta = bool(
                thinking_text and "<think>" not in sampled_text
                and "<|channel>" not in sampled_text
                and sampled_plain.strip()
                and thinking_text.strip().startswith(sampled_plain.strip()))
            if explicit_thinking_delta:
                thinking_piece = update_ui(OllamaChatView._route_model_tool_content,
                                           response_label, sampled_text)
                if thinking_piece:
                    update_ui(OllamaChatView._set_thinking_status,
                              response_label, "Thinking…")
                    update_ui(OllamaChatView._append_thinking_text,
                              response_label, thinking_piece)
            else:
                update_ui(OllamaChatView._append_logprob_answer,
                          response_label, sampled_text, suppress_content)

        def already_sampled(text):
            if not sampled_text or not text:
                return False
            # Ollama can report the same generated text both as token logprobs
            # and in a separated channel field. Ignore the duplicate field
            # delta, while retaining channel fields that are absent from the
            # sampled-token stream.
            preview = getattr(response_label, "_logprob_preview_text", "")
            preview = re.sub(r"</?think>|<\|channel>thought|<\|channel>|<channel\|>",
                             "", preview)
            normalized = re.sub(r"</?think>|<\|channel>thought|<\|channel>|<channel\|>",
                                "", str(text))
            return bool(normalized and normalized in preview)

        # Separate channel fields are authoritative when sampled tokens did not
        # already carry the same text. This handles servers/models that expose
        # channel text without token logprobs.
        if thinking and not already_sampled(str(thinking)):
            if chat_view is not None:
                chat_view._record_debug_event("ASSISTANT CHANNEL", str(thinking),
                                              channel="thinking", merge=True)
            update_ui(OllamaChatView._set_thinking_status, response_label, "Thinking…")
            thinking = update_ui(OllamaChatView._route_model_tool_content,
                                 response_label, str(thinking))
            if thinking:
                update_ui(OllamaChatView._append_thinking_text, response_label, thinking)

        if content and not already_sampled(str(content)):
            if chat_view is not None:
                chat_view._record_debug_event("ASSISTANT CHANNEL", str(content),
                                              channel="content", merge=True)
            content = update_ui(OllamaChatView._route_model_tool_content,
                                response_label, str(content))
            if content and getattr(response_label, "_used_logprob_preview", False):
                preview = (getattr(response_label, "_visible_streamed_answer", "")
                           or getattr(response_label, "_logprob_preview_text", ""))
                if preview:
                    # The final event can repeat text that was already shown
                    # from token metadata. Trim the shared prefix/tail so the
                    # canonical channel does not appear twice.
                    if content.startswith(preview):
                        content = content[len(preview):]
                    elif preview.startswith(content) or content in preview:
                        content = ""
                    else:
                        overlap = min(len(preview), len(content))
                        while overlap and not preview.endswith(content[:overlap]):
                            overlap -= 1
                        if overlap:
                            content = content[overlap:]
            if content:
                if tool_call_text is not None and calls:
                    tool_call_text[0] += content
                has_inline_thinking = any(marker in content for marker in
                                          ("<think>", "</think>", "<|channel>"))
                if has_inline_thinking:
                    update_ui(OllamaChatView._begin_generated_token_stream, response_label)
                if has_inline_thinking or suppress_content:
                    # Inline channel markers need the marker-aware parser;
                    # suppressed provisional text must stay out of the answer.
                    update_ui(OllamaChatView._append_generated_token, response_label,
                              content, False, suppress_content, True)
                else:
                    # Ollama's `message.content` is already the user-visible
                    # channel. Render ordinary chunks directly so model
                    # thinking capability cannot delay them until turn end.
                    update_ui(OllamaChatView._append_response_text,
                              response_label, content)
                if not calls and not suppress_content:
                    clean_content = content.replace("</think>", "").replace("<think>", "")
                    if clean_content:
                        streamed_text.append(clean_content)
        if calls:
            update_ui(OllamaChatView._set_thinking_status, response_label,
                      "Preparing tool call…")

    @staticmethod
    def _set_thinking_status(response_view: Gtk.Label, status: str):
        expander = getattr(response_view, "_thinking_expander", None)
        if expander is not None:
            expander.set_label(status)
            expander.set_visible(True)
            if status.startswith("Thinking") or status.startswith("Preparing tool call"):
                expander.set_expanded(True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _noop(*_args):
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _append_thinking_text(response_view: Gtk.Label, content: str):
        label = getattr(response_view, "_thinking_text", None)
        if label is None:
            return GLib.SOURCE_REMOVE
        response_view._thinking_tokens = getattr(response_view, "_thinking_tokens", "") + content
        OllamaChatView._render_thinking_text(response_view)
        expander = getattr(response_view, "_thinking_expander", None)
        if expander is not None:
            expander.set_visible(True)
            expander.set_expanded(True)
        chat_view = getattr(response_view, "_chat_view", None)
        if chat_view is not None:
            chat_view._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _append_tool_call_text(response_view: Gtk.Label, content: str):
        frame = getattr(response_view, "_tool_preparation_frame", None)
        if frame is not None:
            frame.set_visible(True)
            frame.set_expanded(True)
            label = response_view._tool_preparation_tokens
            label.set_text(label.get_text() + content)
            raw = label.get_text()
            gemma = "<|tool_call>" in raw
            json_name = None
            if gemma:
                payload = raw.split("<|tool_call>", 1)[1]
                function_match = re.match(r"\s*call:([A-Za-z_][A-Za-z0-9_]*)", payload)
            else:
                payload = raw.split("<tool_call>", 1)[1] if "<tool_call>" in raw else ""
                json_name = re.search(r'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"', payload)
                function_match = (json_name or re.search(
                    r"<function=([A-Za-z_][A-Za-z0-9_]*)>", payload))
            if function_match:
                name = function_match.group(1)
                response_view._tool_preparation_name.set_text(name)
                if gemma:
                    arguments = {}
                elif json_name:
                    arguments = OllamaChatView._parse_partial_json_tool_arguments(payload)
                else:
                    arguments = OllamaChatView._parse_partial_tool_markup_arguments(
                        payload[function_match.end():])
                if name == "ask_user" and not json_name:
                    arguments.update(OllamaChatView._parse_ask_user_child_tags(
                        payload[function_match.end():], partial=True))
                if arguments and arguments != getattr(response_view, "_tool_markup_last_arguments", None):
                    response_view._tool_markup_last_arguments = arguments
                    OllamaChatView._update_tool_preparation(response_view, name, arguments)
            if not gemma and re.match(r"\s*\{\s*\"name\"\s*:", payload):
                response_view._tool_markup_kind = "json"
            chat_view = getattr(response_view, "_chat_view", None)
            if chat_view is not None:
                chat_view._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _parse_partial_json_tool_arguments(source: str) -> dict:
        """Return the arguments object once its JSON value is complete."""
        source = str(source)
        match = re.search(r'"arguments"\s*:\s*', source)
        if match is None:
            return {}
        start = match.end()
        while start < len(source) and source[start].isspace():
            start += 1
        if start >= len(source) or source[start] != "{":
            return {}
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(source)):
            char = source[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(source[start:index + 1])
                    except json.JSONDecodeError:
                        return {}
                    return value if isinstance(value, dict) else {}
        return {}

    @staticmethod
    def _parse_partial_tool_markup_arguments(source: str) -> dict:
        """Decode complete values from a still-open streamed tool-call envelope.

        Models sometimes emit the documented ``<parameter=name>value`` form,
        but also produce ``<parameter=name=\"value\"``. Accept both while
        streaming; this preview parser never authorizes execution.
        """
        arguments = {}
        source = OllamaChatView._repair_missing_parameter_separator(str(source))
        parameter = re.compile(
            r"\s*<parameter=([A-Za-z_][A-Za-z0-9_]*)(>|=)|"
            r"\s*<parameter>([A-Za-z_][A-Za-z0-9_]*)>")
        cursor = 0
        while match := parameter.match(source, cursor):
            key = match.group(1) or match.group(3)
            inline_value = match.group(2) == "="
            value_start = match.end()
            if inline_value:
                if source[value_start:value_start + 1] == '"':
                    # Inline quoted scalar.
                    end = re.search(r'(?<!\\)"', source[value_start + 1:])
                    if not end:
                        break
                    raw = source[value_start:value_start + 1 + end.start() + 1]
                    try:
                        arguments[key] = json.loads(raw)
                    except json.JSONDecodeError:
                        break
                    cursor = value_start + len(raw)
                    close_tag = re.match(r"\s*</parameter>", source[cursor:])
                    if close_tag:
                        cursor += close_tag.end()
                    continue
                # Inline JSON object/array. Parse its complete prefix members
                # now, even when the container's closing delimiter has not
                # arrived yet.
                rest = source[value_start:]
                next_parameter = re.search(
                    r"<parameter(?:=[A-Za-z_][A-Za-z0-9_]*(?:>|=)|"
                    r">[A-Za-z_][A-Za-z0-9_]*>)", rest)
                raw_value = rest[:next_parameter.start()] if next_parameter else rest
                raw_value = raw_value.split("</parameter>", 1)[0].strip()
                partial = OllamaChatView._decode_json_prefix(raw_value)
                if key == "fields":
                    partial = OllamaChatView._merge_partial_fields(raw_value, partial)
                if partial is not None:
                    arguments[key] = partial
                break

            closing = source.find("</parameter>", value_start)
            next_parameter = re.search(
                r"<parameter(?:=[A-Za-z_][A-Za-z0-9_]*(?:>|=)|"
                r">[A-Za-z_][A-Za-z0-9_]*>)", source[value_start:])
            boundary = value_start + next_parameter.start() if next_parameter else len(source)
            raw_value = source[value_start:closing if closing >= 0 and closing < boundary
                               else boundary].strip()
            if closing >= 0 and closing < boundary:
                try:
                    arguments[key] = json.loads(raw_value)
                except json.JSONDecodeError:
                    arguments[key] = raw_value
                cursor = closing + len("</parameter>")
                continue

            if next_parameter and raw_value:
                # A complete parameter followed by the next parameter is
                # enough to preview it even if its closing tag was omitted.
                try:
                    arguments[key] = json.loads(raw_value)
                except json.JSONDecodeError:
                    arguments[key] = raw_value
                cursor = boundary
                continue

            # For JSON arrays/objects, expose each fully generated member/item
            # as soon as it can be decoded, without waiting for the final ]/}.
            partial = OllamaChatView._decode_json_prefix(raw_value)
            if key == "fields":
                partial = OllamaChatView._merge_partial_fields(raw_value, partial)
            if partial is not None:
                arguments[key] = partial
            elif raw_value:
                arguments[key] = raw_value
            break
        return arguments

    @staticmethod
    def _repair_missing_parameter_separator(source: str) -> str:
        """Repair a missing `>` before an unambiguous parameter value."""
        source = re.sub(
            r'<parameter=([A-Za-z_][A-Za-z0-9_]*)(?=(?:true|false|null)(?=\s|</parameter>|$)|-?\d|[\{\[\"])',
            r'<parameter=\1>',
            source,
        )
        # Local models sometimes omit the separator when a plain string value
        # starts on the next line, e.g. ``<parameter=runner\nlocal``. JSON
        # values are covered above; this narrow line-boundary case repairs the
        # tag without changing valid parameters or guessing at same-line text.
        return re.sub(
            r'(<parameter=([A-Za-z_][A-Za-z0-9_]*))(?=\s*\n\s*[^<\s])',
            r'\1>',
            source,
        )

    @staticmethod
    def _parse_ask_user_child_tags(source: str, partial: bool = False) -> dict:
        """Recover ask_user fields emitted as <title>/<message>/<fields> tags."""
        result = {}
        for key in ("title", "message"):
            match = re.search(
                rf"<(?:parameter={key}|{key})>(.*?)</{key}>", source, re.DOTALL)
            if match:
                raw = match.group(1).strip()
                try:
                    result[key] = json.loads(raw) if raw.startswith('"') else raw
                except json.JSONDecodeError:
                    result[key] = raw
        match = re.search(r"<(?:fields|parameter=fields)>", source)
        if match:
            remainder = source[match.end():]
            closing = re.search(r"</(?:fields|parameter)>", remainder)
            raw = remainder[:closing.start()] if closing else remainder
            fields = OllamaChatView._decode_json_prefix(raw)
            fields = OllamaChatView._merge_partial_fields(raw, fields)
            if isinstance(fields, list) and (partial or closing):
                result["fields"] = fields
        return result

    @staticmethod
    def _merge_partial_fields(source: str, complete_fields):
        """Include the currently streamed ask_user field and its completed options."""
        fields = list(complete_fields) if isinstance(complete_fields, list) else []
        # The last object can be incomplete while its label/type/options are
        # already useful. Complete option objects are individually valid JSON.
        starts = []
        in_string = escaped = False
        depth = 0
        for index, char in enumerate(source):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                # Square brackets are deliberately not counted here: field
                # objects are the outermost brace-level values in this array.
                if depth == 0:
                    starts.append(index)
                depth += 1
            elif char == "}" and depth:
                depth -= 1

        # Locate the final incomplete field object, if one exists.
        if not depth or not starts:
            return fields or None
        field_start = starts[-1]
        fragment = source[field_start:]
        current = {}
        for key in ("id", "label", "type", "required", "default"):
            match = re.search(rf'"{key}"\s*:\s*("(?:\\.|[^"\\])*"|true|false|null)', fragment)
            if match:
                try:
                    current[key] = json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
        options_pos = fragment.find('"options"')
        if options_pos >= 0:
            array_start = fragment.find("[", options_pos)
            if array_start >= 0:
                option_prefix = fragment[array_start:]
                parsed = OllamaChatView._decode_json_prefix(option_prefix)
                if isinstance(parsed, list):
                    options = list(parsed)
                    partial_option = OllamaChatView._partial_json_object(
                        option_prefix)
                    if partial_option:
                        if (options and isinstance(options[-1], dict)
                                and options[-1].get("value") == partial_option.get("value")):
                            options[-1] = {**options[-1], **partial_option}
                        else:
                            options.append(partial_option)
                    current["options"] = options
        if current:
            if fields and isinstance(fields[-1], dict) and fields[-1].get("id") == current.get("id"):
                fields[-1] = {**fields[-1], **current}
            else:
                fields.append(current)
        return fields or None

    @staticmethod
    def _partial_json_object(source: str):
        """Read completed scalar properties from the last unfinished JSON object."""
        starts = []
        in_string = escaped = False
        depth = 0
        for index, char in enumerate(source):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                if depth == 0:
                    starts.append(index)
                depth += 1
            elif char == "}" and depth:
                depth -= 1
        if not depth or not starts:
            return {}
        fragment = source[starts[-1]:]
        result = {}
        for key in ("id", "label", "value", "type", "required", "default"):
            match = re.search(
                rf'"{key}"\s*:\s*("(?:\\.|[^"\\])*"|true|false|null|-?\d+(?:\.\d+)?)',
                fragment)
            if match:
                try:
                    result[key] = json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
        return result

    @staticmethod
    def _decode_json_prefix(source: str):
        """Return the complete portion of a JSON object/array prefix."""
        decoder = json.JSONDecoder()
        source = source.lstrip()
        if not source or source[0] not in "[{":
            try:
                return decoder.raw_decode(source)[0]
            except (json.JSONDecodeError, TypeError):
                return None

        root_open = source[0]
        result = {} if root_open == "{" else []
        pos = 1
        while pos < len(source):
            while pos < len(source) and (source[pos].isspace() or source[pos] == ","):
                pos += 1
            if pos >= len(source) or source[pos] in "}]":
                break
            try:
                if root_open == "{":
                    key, key_end = decoder.raw_decode(source, pos)
                    if not isinstance(key, str):
                        break
                    colon = key_end
                    while colon < len(source) and source[colon].isspace():
                        colon += 1
                    if colon >= len(source) or source[colon] != ":":
                        break
                    value_start = colon + 1
                    while value_start < len(source) and source[value_start].isspace():
                        value_start += 1
                    value, value_end = decoder.raw_decode(source, value_start)
                    result[key] = value
                    pos = value_end
                else:
                    value, pos = decoder.raw_decode(source, pos)
                    result.append(value)
            except (json.JSONDecodeError, TypeError):
                break
        return result if result else None

    @staticmethod
    def _update_tool_preparation(response_view: Gtk.Label, name: str, arguments):
        frame = getattr(response_view, "_tool_preparation_frame", None)
        if frame is None:
            return GLib.SOURCE_REMOVE
        frame.set_visible(True)
        frame.set_expanded(True)
        if name:
            frame.set_label(f"Tool call · {name}")
        response_view._tool_preparation_name.set_text(name or "Waiting for tool name…")
        grid = response_view._tool_preparation_parameters
        while child := grid.get_first_child():
            grid.remove(child)
        if name == "ask_user" and isinstance(arguments, dict):
            row = 0
            for key in ("title", "message"):
                value = arguments.get(key)
                if value:
                    label = Gtk.Label(label=str(value), xalign=0, wrap=True, selectable=True)
                    label.add_css_class("dim-label")
                    grid.attach(label, 0, row, 2, 1)
                    row += 1
            fields = arguments.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    label_text = str(field.get("label") or field.get("id") or "Field")
                    field_label = Gtk.Label(label=label_text, xalign=0, valign=Gtk.Align.START)
                    field_label.add_css_class("dim-label")
                    details = [str(field.get("type") or "")]
                    options = field.get("options")
                    if isinstance(options, list):
                        details.extend(str(option.get("label", option.get("value", "")))
                                       for option in options if isinstance(option, dict))
                    elif field.get("type") in ("select", "multiselect"):
                        details.append("Loading options…")
                    preview = Gtk.Label(label=" · ".join(filter(None, details)), xalign=0,
                                        yalign=0, wrap=True, selectable=True)
                    grid.attach(field_label, 0, row, 1, 1)
                    grid.attach(preview, 1, row, 1, 1)
                    row += 1
            if row == 0:
                hint = Gtk.Label(label="Receiving form fields…", xalign=0)
                hint.add_css_class("dim-label")
                grid.attach(hint, 0, row, 2, 1)
        elif isinstance(arguments, dict):
            for row, (key, value) in enumerate(arguments.items()):
                key_label = Gtk.Label(label=str(key), xalign=0, valign=Gtk.Align.START)
                key_label.add_css_class("dim-label")
                value_label = Gtk.Label(
                    label=json.dumps(value, ensure_ascii=False, default=str),
                    xalign=0, yalign=0, wrap=True, selectable=True)
                grid.attach(key_label, 0, row, 1, 1)
                grid.attach(value_label, 1, row, 1, 1)
        chat_view = getattr(response_view, "_chat_view", None)
        if chat_view is not None:
            chat_view._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _promote_tool_preparation(response_view: Gtk.Label, call: dict):
        function = call.get("function") or {}
        OllamaChatView._update_tool_preparation(
            response_view, str(function.get("name") or ""),
            function.get("arguments") or {})
        parser = getattr(response_view, "_markdown_parser", None)
        if parser is not None and parser.raw:
            label = response_view._tool_preparation_tokens
            label.set_text(label.get_text() + parser.raw)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _remove_tool_preparation_frames(turn_container):
        if turn_container is None:
            return GLib.SOURCE_REMOVE
        for frame in getattr(turn_container, "_tool_preparation_frames", []):
            if frame.get_visible():
                frame.set_label(frame.get_label().replace("Preparing tool call", "Tool call"))
                frame.set_expanded(True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _collapse_tool_preparation_frames(turn_container):
        if turn_container is None:
            return GLib.SOURCE_REMOVE
        for frame in getattr(turn_container, "_tool_preparation_frames", []):
            if frame.get_visible():
                frame.set_expanded(False)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _begin_generated_token_stream(response_view: Gtk.Label):
        response_view._has_generated_token_stream = True
        expander = getattr(response_view, "_thinking_expander", None)
        if expander is not None and not getattr(response_view, "_generation_response_started", False):
            expander.set_label("Thinking…")
            expander.set_expanded(True)
        OllamaChatView._render_thinking_text(response_view)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _append_generated_token(response_view: Gtk.Label, token: str,
                                tool_call: bool = False, suppress_content: bool = False,
                                thinking_supported: bool = True):
        """Render sampled tokens live: thinking/tool syntax gray, answer below it."""
        token = str(token)
        token = OllamaChatView._route_model_tool_content(response_view, token)
        if not token:
            return GLib.SOURCE_REMOVE
        has_thinking_marker = any(marker in token for marker in
                                  ("<think>", "</think>", "<|channel>", "<channel|>"))
        if (not thinking_supported and not suppress_content and not tool_call
                and not has_thinking_marker
                and not getattr(response_view, "_generation_has_think_marker", False)):
            # If Ollama rejected `think`, these sampled tokens are the model's
            # ordinary response. Models without a thinking channel never emit
            # </think>, so keep their answer out of the Thinking expander.
            response_view._generation_response_started = True
            response_view._visible_streamed_answer = (
                getattr(response_view, "_visible_streamed_answer", "") + token)
            OllamaChatView._append_response_text(response_view, token)
            return GLib.SOURCE_REMOVE
        close_markers = ("</think>", "<channel|>")
        gemma_thinking_open = "<|channel>thought"
        if "<think>" in token or gemma_thinking_open in token:
            response_view._generation_has_think_marker = True
        in_tool_call = tool_call or bool(getattr(response_view, "_tool_call_generation", False))
        current = getattr(response_view, "_generation_marker_buffer", "") + token
        response_view._generation_marker_buffer = ""
        answer_piece = []
        tool_piece = []
        markers = ("<think>", gemma_thinking_open, *close_markers)
        while current:
            if current.startswith("<think>"):
                response_view._generation_has_think_marker = True
                response_view._generation_response_started = False
                current = current[len("<think>"):]
                continue
            if current.startswith(gemma_thinking_open):
                response_view._generation_has_think_marker = True
                response_view._generation_response_started = False
                expander = getattr(response_view, "_thinking_expander", None)
                if expander is not None:
                    expander.set_label("Thinking…")
                    expander.set_visible(True)
                    expander.set_expanded(True)
                response_view._generation_gemma_skip_newline = True
                current = current[len(gemma_thinking_open):]
                continue
            closing_marker = next((marker for marker in close_markers
                                   if current.startswith(marker)), None)
            if closing_marker:
                response_view._generation_response_started = True
                expander = getattr(response_view, "_thinking_expander", None)
                if expander is not None:
                    expander.set_label("Thinking")
                    # The marker ends the thinking channel, but the model may
                    # still be generating answer or tool-call tokens in this
                    # same response. Keep the block open until that response
                    # completes; completion handlers collapse it afterwards.
                current = current[len(closing_marker):]
                continue
            if getattr(response_view, "_generation_gemma_skip_newline", False):
                while current.startswith(("\r", "\n")):
                    current = current[1:]
                if not current:
                    break
                response_view._generation_gemma_skip_newline = False
            # Hold a suffix that might be a marker split across token chunks.
            partial = next((marker for marker in markers if marker.startswith(current)), None)
            if partial:
                response_view._generation_marker_buffer = current
                break
            if (in_tool_call and (getattr(response_view, "_generation_response_started", False)
                                  or not getattr(response_view, "_generation_has_think_marker", False))):
                response_view._tool_call_generation = True
                response_view._tool_call_token_text = getattr(response_view, "_tool_call_token_text", "") + current[0]
                tool_piece.append(current[0])
            elif getattr(response_view, "_generation_response_started", False):
                if suppress_content:
                    response_view._generated_token_text = getattr(
                        response_view, "_generated_token_text", "") + current[0]
                else:
                    answer_piece.append(current[0])
            else:
                response_view._generated_token_text = getattr(response_view, "_generated_token_text", "") + current[0]
            current = current[1:]
        if answer_piece:
            answer_text = "".join(answer_piece)
            response_view._visible_streamed_answer = (
                getattr(response_view, "_visible_streamed_answer", "") + answer_text)
            OllamaChatView._append_response_text(response_view, answer_text)
        if tool_piece:
            OllamaChatView._append_tool_call_text(response_view, "".join(tool_piece))
        OllamaChatView._render_thinking_text(response_view)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _append_logprob_answer(response_view: Gtk.Label, token: str,
                               suppress_content: bool = False):
        token = OllamaChatView._route_model_tool_content(response_view, token)
        if not token:
            return GLib.SOURCE_REMOVE
        markers = ("<think>", "</think>", "<|channel>thought", "<|channel>", "<channel|>")
        current = getattr(response_view, "_logprob_marker_buffer", "") + token
        response_view._logprob_marker_buffer = ""
        if any(marker in current for marker in markers):
            OllamaChatView._append_logprob_channel_tokens(response_view, current)
            return GLib.SOURCE_REMOVE
        pending = next((current[-size:] for size in range(1, min(len(current), max(map(len, markers))) + 1)
                        if any(marker.startswith(current[-size:]) for marker in markers)), "")
        answer = current[:-len(pending)] if pending else current
        response_view._logprob_marker_buffer = pending
        if answer and not suppress_content:
            response_view._visible_streamed_answer = (
                getattr(response_view, "_visible_streamed_answer", "") + answer)
            OllamaChatView._append_response_text(response_view, answer)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _append_logprob_channel_tokens(response_view: Gtk.Label, token: str,
                                       suppress_content: bool = False):
        OllamaChatView._begin_generated_token_stream(response_view)
        # Before an explicit marker appears, sampled text belongs to the
        # answer channel. The marker parser switches into/out of thinking.
        response_view._generation_response_started = True
        OllamaChatView._append_generated_token(
            response_view, token, False, suppress_content, True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _route_model_tool_content(response_view: Gtk.Label, token: str) -> str:
        """Route supported tool envelopes into their live card."""
        return OllamaChatView._route_tool_call_markup(response_view, token)

    @staticmethod
    def _route_tool_call_markup(response_view: Gtk.Label, token: str) -> str:
        openings = ("<tool_call>", "<|tool_call>")
        current = getattr(response_view, "_tool_markup_buffer", "") + str(token)
        response_view._tool_markup_buffer = ""
        ordinary, captured = [], []
        active = bool(getattr(response_view, "_tool_markup_active", False))
        while current:
            if active:
                kind = getattr(response_view, "_tool_markup_kind", "xml")
                terminators = (["<tool_call|>"] if kind == "gemma" else
                               ["</tool_call>"] if kind == "json" else
                               ["</tool_call>", "</function>"])
            else:
                terminators = [*openings, "</tool_call>"]
            found = [(current.find(marker), marker) for marker in terminators
                     if current.find(marker) >= 0]
            if found:
                index, marker = min(found, key=lambda item: item[0])
                if marker in openings:
                    ordinary.append(current[:index])
                    captured.append(marker)
                    active = True
                    response_view._tool_markup_kind = (
                        "gemma" if marker == "<|tool_call>" else "xml")
                    response_view._thinking_tool_markup_seen = True
                elif marker == "</tool_call>" and not active:
                    # Qwen sometimes omits </tool_call> after closing the named function.
                    # Consume a delayed wrapper terminator without leaking it into the answer.
                    pass
                else:
                    captured.append(current[:index + len(marker)])
                    active = False
                current = current[index + len(marker):]
                continue
            partial = next((candidate for candidate in terminators
                            if candidate.startswith(current)), None)
            if partial:
                response_view._tool_markup_buffer = current
                frame = getattr(response_view, "_tool_preparation_frame", None)
                if frame is not None:
                    frame.set_visible(True)
                    frame.set_expanded(True)
                    chat_view = getattr(response_view, "_chat_view", None)
                    if chat_view is not None:
                        chat_view._scroll_to_bottom()
                break
            # Preserve only a suffix that could become a delimiter in the next token.
            suffix_length = max((length for length in range(
                1, min(len(current), max(map(len, terminators)) - 1) + 1)
                if any(marker.startswith(current[-length:]) for marker in terminators)), default=0)
            body = current[:-suffix_length] if suffix_length else current
            (captured if active else ordinary).append(body)
            if suffix_length:
                response_view._tool_markup_buffer = current[-suffix_length:]
            current = ""
        response_view._tool_markup_active = active
        if captured:
            OllamaChatView._append_tool_call_text(response_view, "".join(captured))
        return "".join(ordinary)

    @staticmethod
    def _clear_tool_call_preview(response_view: Gtk.Label):
        response_view._tool_call_text = ""
        response_view._tool_call_token_text = ""
        response_view._tool_call_generation = False
        response_view._tool_markup_buffer = ""
        response_view._tool_markup_active = False
        response_view._tool_markup_last_arguments = None
        OllamaChatView._render_thinking_text(response_view)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _clear_pending_tool_preview(response_view: Gtk.Label):
        """Discard ambiguous post-think tokens once Ollama identifies them as answer text."""
        response_view._tool_call_token_text = ""
        response_view._tool_call_generation = False
        OllamaChatView._render_thinking_text(response_view)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _clear_answer_token_preview(response_view: Gtk.Label):
        """For models without a think channel, leave the streamed answer in its normal bubble only."""
        response_view._generated_token_text = ""
        response_view._has_generated_token_stream = False
        response_view._logprob_marker_buffer = ""
        response_view._logprob_preview_text = ""
        response_view._used_logprob_preview = False
        response_view._response_edge_buffer = ""
        response_view._response_has_visible_text = False
        response_view._visible_streamed_answer = ""
        OllamaChatView._render_thinking_text(response_view)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _render_thinking_text(response_view: Gtk.Label):
        label = getattr(response_view, "_thinking_text", None)
        if label is None:
            return GLib.SOURCE_REMOVE
        thinking = getattr(response_view, "_thinking_tokens", "")
        tool_text = getattr(response_view, "_tool_call_text", "")
        generated = getattr(response_view, "_generated_token_text", "")
        sections = [OllamaChatView._strip_empty_edge_lines(part)
                    for part in (thinking, tool_text, generated) if part]
        sections = [part for part in sections if part]
        label.set_text("\n".join(sections))
        expander = getattr(response_view, "_thinking_expander", None)
        if expander is not None:
            expander.set_visible(True)
        chat_view = getattr(response_view, "_chat_view", None)
        if chat_view is not None:
            chat_view._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _clear_thinking(response_view: Gtk.Label):
        label = getattr(response_view, "_thinking_text", None)
        expander = getattr(response_view, "_thinking_expander", None)
        response_view._thinking_tokens = ""
        response_view._tool_call_text = ""
        response_view._generated_token_text = ""
        response_view._tool_call_token_text = ""
        response_view._tool_call_generation = False
        response_view._generation_response_started = False
        response_view._generation_has_think_marker = False
        response_view._generation_gemma_thinking_active = False
        response_view._generation_gemma_skip_newline = False
        response_view._generation_marker_buffer = ""
        response_view._tool_markup_buffer = ""
        response_view._tool_markup_active = False
        response_view._tool_markup_last_arguments = None
        response_view._has_generated_token_stream = False
        response_view._logprob_marker_buffer = ""
        response_view._logprob_preview_text = ""
        response_view._used_logprob_preview = False
        response_view._response_edge_buffer = ""
        response_view._response_has_visible_text = False
        if label is not None:
            label.set_text("")
        if expander is not None:
            expander.set_label("Thinking…")
            expander.set_visible(False)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _flush_incomplete_stream_text(response_view: Gtk.Label):
        """Expose delimiter fragments held back when a stream ends early."""
        generated_marker = getattr(response_view, "_generation_marker_buffer", "")
        response_view._generation_marker_buffer = ""
        if generated_marker:
            OllamaChatView._append_response_text(response_view, generated_marker)

        tool_marker = getattr(response_view, "_tool_markup_buffer", "")
        response_view._tool_markup_buffer = ""
        if tool_marker:
            if getattr(response_view, "_tool_markup_active", False):
                OllamaChatView._append_tool_call_text(response_view, tool_marker)
            else:
                OllamaChatView._append_response_text(response_view, tool_marker)

        logprob_marker = getattr(response_view, "_logprob_marker_buffer", "")
        response_view._logprob_marker_buffer = ""
        if logprob_marker:
            OllamaChatView._append_response_text(response_view, logprob_marker)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _collapse_turn_thinking(response_view: Gtk.Label):
        turn_container = getattr(response_view, "_turn_container", None)
        for expander in getattr(turn_container, "_thinking_expanders", []):
            if expander.get_visible() and expander._thinking_text.get_text().strip():
                expander.set_label("Thinking")
                expander.set_expanded(False)
            else:
                expander.set_visible(False)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _append_response_text(response_view: Gtk.Label, content: str):
        parser = getattr(response_view, "_markdown_parser", None)
        if parser is None:
            parser = MarkdownStreamRenderer(response_view)
            response_view._markdown_parser = parser
        # Hold trailing blank lines until the next chunk confirms whether they
        # are internal paragraph spacing or just generation padding. Leading
        # blank lines are discarded as soon as visible text begins.
        incoming = getattr(response_view, "_response_edge_buffer", "") + str(content)
        response_view._response_edge_buffer = ""
        lines = incoming.splitlines(keepends=True)
        if not getattr(response_view, "_response_has_visible_text", False):
            while lines and not lines[0].strip():
                lines.pop(0)
        if not lines:
            return GLib.SOURCE_REMOVE
        while lines and not lines[-1].strip():
            response_view._response_edge_buffer = lines.pop() + response_view._response_edge_buffer
        visible = "".join(lines)
        trailing_breaks = re.search(r"(?:\r\n|\n|\r)+$", visible)
        if trailing_breaks:
            response_view._response_edge_buffer = (
                trailing_breaks.group() + response_view._response_edge_buffer)
            visible = visible[:trailing_breaks.start()]
        if visible:
            parser.append(visible)
            if visible.strip():
                response_view._response_has_visible_text = True
        chat_view = getattr(response_view, "_chat_view", None)
        if chat_view is not None:
            chat_view._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _strip_empty_edge_lines(text: str) -> str:
        lines = str(text or "").splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    @staticmethod
    def _finalize_response(response_view: Gtk.Label):
        OllamaChatView._clear_pending_tool_preview(response_view)
        OllamaChatView._collapse_turn_thinking(response_view)
        parser = getattr(response_view, "_markdown_parser", None)
        if parser:
            parser.append("", final=True)
        return GLib.SOURCE_REMOVE

    def _add_message(self, speaker: str, text: str, formatted: bool = False, with_activity: bool = False):
        is_user = speaker == "You"
        if is_user:
            self._record_debug_event("USER MESSAGE", text=text)
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        halign=Gtk.Align.END if is_user else Gtk.Align.FILL)
        block.add_css_class("chat-message")
        block.add_css_class("chat-message-user" if is_user else "chat-message-assistant")
        heading = Gtk.Label(label=speaker, xalign=0)
        heading.add_css_class("heading")
        block.append(heading)
        turn_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        block.append(turn_container)
        if with_activity:
            self._active_turn_container = turn_container
        body = self._append_response_segment(turn_container, formatted=formatted, text=text,
                                             raw_header=False)
        body._turn_container = turn_container
        self.transcript.append(block)
        self._append_raw_output(f"{speaker}:\n{text}\n\n")
        self._scroll_to_bottom()
        return body

    def _append_response_segment(self, turn_container: Gtk.Box, formatted: bool = True,
                                 text: str = "", raw_header: bool = True) -> Gtk.Label:
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.add_css_class("chat-message-bubble")
        if formatted:
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            body.add_css_class("chat-markdown")
            body._markdown_parser = MarkdownStreamRenderer(body)
            if text:
                body._markdown_parser.append(text)
        else:
            body = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
            body.set_max_width_chars(100)
        if getattr(self, "_active_turn_container", None) is turn_container:
            thinking_text = Gtk.Label(xalign=0, wrap=True, selectable=True)
            thinking_text.add_css_class("dim-label")
            thinking = Gtk.Expander(label="Thinking…")
            thinking.add_css_class("chat-thinking")
            thinking.set_child(thinking_text)
            thinking.set_expanded(True)
            thinking.set_visible(False)
            bubble.append(thinking)
            body._thinking_text = thinking_text
            body._thinking_expander = thinking
            body._chat_view = self
            thinking._thinking_text = thinking_text
            turn_container._thinking_expanders = getattr(turn_container, "_thinking_expanders", []) + [thinking]
            preparation = Gtk.Expander(label="Preparing tool call")
            preparation.add_css_class("chat-tool-preparation")
            preparation.connect("notify::expanded", self._sync_tool_preparation_style)
            preparation_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                                      margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)
            tool_name = Gtk.Label(xalign=0, wrap=True)
            tool_name.add_css_class("dim-label")
            preparation_box.append(tool_name)
            parameters = Gtk.Grid(column_spacing=12, row_spacing=4)
            preparation_box.append(parameters)
            generated_label = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
            generated_label.add_css_class("dim-label")
            generated_label.add_css_class("monospace")
            raw_tokens = Gtk.Expander(label="Raw tool tokens", expanded=False)
            raw_tokens.add_css_class("chat-tool-raw-output")
            raw_tokens.set_child(generated_label)
            preparation_box.append(raw_tokens)
            preparation.set_child(preparation_box)
            preparation.set_visible(False)
            bubble.append(preparation)
            body._tool_preparation_frame = preparation
            body._tool_preparation_name = tool_name
            body._tool_preparation_parameters = parameters
            body._tool_preparation_tokens = generated_label
            body._tool_preparation_raw_expander = raw_tokens
            turn_container._tool_preparation_frames = getattr(
                turn_container, "_tool_preparation_frames", []) + [preparation]
        bubble.append(body)
        turn_container.append(bubble)
        body._chat_view = self
        body._turn_container = turn_container
        body._raw_streamed_text = False
        turn_container._latest_response = body
        if raw_header:
            self._append_raw_output("\nOllama:\n")
        self._scroll_to_bottom()
        return body

    @staticmethod
    def _sync_tool_preparation_style(expander, _property):
        if expander.get_expanded():
            expander.add_css_class("chat-tool-preparation-expanded")
        else:
            expander.remove_css_class("chat-tool-preparation-expanded")

    def _append_raw_output(self, text: str):
        """Append unparsed conversation text to the plain transcript tab."""
        if not text:
            return GLib.SOURCE_REMOVE
        end = self.raw_output_buffer.get_end_iter()
        self.raw_output_buffer.insert(end, str(text))
        adjustment = self.raw_output_scroll.get_vadjustment()
        GLib.idle_add(lambda: (adjustment.set_value(
            max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())),
            GLib.SOURCE_REMOVE)[1])
        return GLib.SOURCE_REMOVE

    def save_debug_conversation(self, force: bool = False):
        """Persist the raw text tab after this chat closes in a debug run."""
        if not self.debug_conversation_logging or self._debug_conversation_saved:
            return None
        self._debug_conversation_save_requested = True
        if self._turn_active and not force:
            return None
        start = self.raw_output_buffer.get_start_iter()
        end = self.raw_output_buffer.get_end_iter()
        raw_text = self.raw_output_buffer.get_text(start, end, True)
        try:
            with self._debug_events_lock:
                events = [dict(event) for event in self._debug_events]
            path = save_conversation(self.model, raw_text, events)
        except OSError as error:
            print(f"Failed to save debug conversation: {error}")
            return None
        self._debug_conversation_saved = path is not None
        if path is not None:
            print(f"Saved debug conversation: {path}")
        return path

    def _finish_reply(self, label: Gtk.Label, reply: str, error: str | None):
        if error:
            self._record_debug_event("TURN ERROR", text=error)
        elif getattr(label, "_turn_stopped", False):
            self._record_debug_event("TURN STOPPED", text="Generation stopped by the user.")
        else:
            parser = getattr(label, "_markdown_parser", None)
            rendered = getattr(parser, "raw", "")
            self._record_debug_event(
                "TURN COMPLETE", text="Assistant turn finished.",
                details=(f"Final response characters: {len(reply)}; "
                         f"stream-rendered characters: {len(rendered)}"))
        self._collapse_turn_thinking(label)
        turn_container = getattr(label, "_turn_container", None)
        self._remove_tool_preparation_frames(turn_container)
        self._collapse_tool_preparation_frames(turn_container)
        if error:
            self._set_response_error(label, error)
        elif getattr(label, "_turn_stopped", False):
            parser = getattr(label, "_markdown_parser", None)
            if parser is not None:
                parser.append("\n\nStopped.", final=True)
            self.messages.append({"role": "assistant", "content": "Stopped."})
        else:
            # Keep streamed text live, but recover the canonical response if
            # the stream renderer received only empty/whitespace fragments.
            parser = getattr(label, "_markdown_parser", None)
            rendered = getattr(parser, "raw", "")
            display_reply = self._strip_empty_edge_lines(reply)
            label._response_edge_buffer = ""
            if display_reply and parser and not rendered.strip():
                parser.raw = ""
                parser.append(display_reply, final=True)
            elif display_reply and parser and display_reply.startswith(rendered) and len(display_reply) > len(rendered):
                self._append_response_text(label, display_reply[len(rendered):])
            if reply and not getattr(label, "_raw_streamed_text", False):
                self._append_raw_output(reply)
                label._raw_streamed_text = True
            self.messages.append({"role": "assistant", "content": reply})
        self.entry.set_sensitive(True)
        self._turn_active = False
        self._active_client = None
        self._turn_cancel_event = None
        self.bridge.cancel_event = None
        self._set_composer_button()
        self.entry.grab_focus()
        self._active_turn_container = None
        self._scroll_to_bottom()
        if self._debug_conversation_save_requested:
            self.save_debug_conversation()
        if not error and getattr(self.bridge, "restart_after_turn", False):
            self.bridge.restart_after_turn = False
            GLib.timeout_add(1500, self._restart_application)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _restart_application():
        application = Gtk.Application.get_default()
        if application is not None:
            application.restart()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _set_response_error(response_view: Gtk.Widget, error: str):
        if isinstance(response_view, Gtk.Label):
            previous = response_view.get_text()
            response_view.set_text(f"{previous}\n\n{error}" if previous else error)
            response_view.add_css_class("error")
            return
        label = Gtk.Label(label=error, xalign=0, wrap=True, selectable=True)
        label.add_css_class("error")
        response_view.append(label)

    def _scroll_to_bottom(self):
        if self._scroll_source_id:
            return

        def scroll_after_layout():
            self._scroll_source_id = 0
            adjustment = self.scroll.get_vadjustment()
            adjustment.set_value(max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()))
            return GLib.SOURCE_REMOVE

        # Wait for GTK to allocate the updated transcript before reading the adjustment bounds.
        self._scroll_source_id = GLib.timeout_add(40, scroll_after_layout)


class MarkdownStreamRenderer:
    """Render a safe Markdown subset, including responsive tables, as GTK widgets."""
    INLINE_MARKUP = re.compile(r"(<br\s*/?>|\*\*[^*]+\*\*|__[^_]+__|(?<!\*)\*[^*\n]+\*(?!\*)|(?<!_)_[^_\n]+_(?!_)|`[^`\n]+`)", re.IGNORECASE)

    def __init__(self, container: Gtk.Box):
        self.container = container
        self.raw = ""
        self.in_fence = False

    def append(self, text: str, final: bool = False):
        self.raw += text
        self._render()

    def _render(self):
        child = self.container.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.container.remove(child)
            child = next_child

        lines = self.raw.splitlines()
        plain_lines = []
        in_fence = False
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
                plain_lines.append(line)
                index += 1
                continue
            if not in_fence and index + 1 < len(lines):
                headers = self._split_table_row(line)
                separators = self._split_table_row(lines[index + 1])
                if headers and self._is_table_separator(separators):
                    self._append_plain_block(plain_lines)
                    plain_lines = []
                    rows = [headers]
                    index += 2
                    while index < len(lines):
                        row = self._split_table_row(lines[index])
                        if not row:
                            break
                        rows.append(row)
                        index += 1
                    self.container.append(self._build_table(rows, separators))
                    continue
            plain_lines.append(line)
            index += 1
        self._append_plain_block(plain_lines)

    def _append_plain_block(self, lines: list[str]):
        if not lines:
            return
        label = Gtk.Label(xalign=0, wrap=True, selectable=True, use_markup=True)
        label.set_markup(self._to_pango("\n".join(lines)))
        self.container.append(label)

    @staticmethod
    def _split_table_row(line: str) -> list[str] | None:
        if "|" not in line:
            return None
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    @staticmethod
    def _is_table_separator(cells: list[str] | None) -> bool:
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)

    @staticmethod
    def _table_alignment(separator: str) -> float:
        separator = separator.replace(" ", "")
        if separator.startswith(":") and separator.endswith(":"):
            return 0.5
        if separator.endswith(":"):
            return 1.0
        return 0.0

    @classmethod
    def _build_table(cls, rows: list[list[str]], separators: list[str]) -> Gtk.Widget:
        column_count = max(len(rows[0]), len(separators))
        # Keep the table at its natural height. A scrollable container can otherwise
        # inherit spare vertical space from the chat transcript and stretch empty rows.
        grid = Gtk.Grid(column_spacing=0, row_spacing=0, hexpand=True,
                        vexpand=False, valign=Gtk.Align.START)
        for row_index, row in enumerate(rows):
            for column in range(column_count):
                text = row[column] if column < len(row) else ""
                alignment = cls._table_alignment(separators[column]) if column < len(separators) else 0.0
                label = Gtk.Label(xalign=alignment, wrap=True, selectable=True,
                                  use_markup=True, hexpand=True,
                                  vexpand=False, valign=Gtk.Align.START)
                label.set_markup(f"<b>{cls._inline(text)}</b>" if row_index == 0 else cls._inline(text))
                label.add_css_class("chat-table-header" if row_index == 0 else "chat-table-cell")
                grid.attach(label, column, row_index * 2, 1, 1)
            if row_index < len(rows) - 1:
                divider = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL,
                                        vexpand=False, valign=Gtk.Align.START)
                grid.attach(divider, 0, row_index * 2 + 1, column_count, 1)

        frame = Gtk.Frame(hexpand=True, vexpand=False, valign=Gtk.Align.START)
        frame.set_valign(Gtk.Align.START)
        frame.add_css_class("chat-table-frame")
        frame.set_child(grid)
        return frame

    @classmethod
    def _to_pango(cls, markdown: str) -> str:
        from gi.repository import GLib
        rendered = []
        in_fence = False
        for line in markdown.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                rendered.append(f"<tt>{GLib.markup_escape_text(line)}</tt>")
                continue
            if match := re.match(r"^(#{1,6})\s+(.*)$", line):
                rendered.append(f"<b>{cls._inline(match.group(2))}</b>")
            elif match := re.match(r"^(\s*)([-*+]\s+|\d+[.)]\s+)(.*)$", line):
                rendered.append(f"{GLib.markup_escape_text(match.group(1))}• {cls._inline(match.group(3))}")
            else:
                rendered.append(cls._inline(line))
        if markdown.endswith("\n"):
            rendered.append("")
        return "\n".join(rendered)

    @classmethod
    def _inline(cls, text: str) -> str:
        from gi.repository import GLib
        pieces = []
        cursor = 0
        for match in cls.INLINE_MARKUP.finditer(text):
            pieces.append(GLib.markup_escape_text(text[cursor:match.start()]))
            token = match.group(0)
            if re.fullmatch(r"<br\s*/?>", token, re.IGNORECASE):
                # Some model responses embed HTML line breaks inside Markdown
                # table cells. Support only this harmless tag; all other HTML
                # remains escaped by markup_escape_text below.
                pieces.append("<br/>")
            elif token.startswith(("**", "__")):
                pieces.append(f"<b>{GLib.markup_escape_text(token[2:-2])}</b>")
            elif token.startswith("`"):
                pieces.append(f"<tt>{GLib.markup_escape_text(token[1:-1])}</tt>")
            else:
                pieces.append(f"<i>{GLib.markup_escape_text(token[1:-1])}</i>")
            cursor = match.end()
        pieces.append(GLib.markup_escape_text(text[cursor:]))
        return "".join(pieces)
