from __future__ import annotations
import re
from dataclasses import dataclass, replace
from gi.repository import Gtk, GLib, Adw, Pango
from .multistage_process import (
    # Process
    MultiStageProcess,
    MultiStageProcessState,
    MultiStageProcessEvent,
    # Stages
    MultiStageProcessStage,
    MultiStageProcessStageState,
    MultiStageProcessStageEvent
)

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/components/multistage_process_execution_view/multistage_process_execution_view.ui')
class MultistageProcessExecutionView(Gtk.Box):
    __gtype_name__ = 'MultistageProcessExecutionView'

    process_steps_list = Gtk.Template.Child()
    cancel_button = Gtk.Template.Child()
    finish_button = Gtk.Template.Child()
    progress_bar = Gtk.Template.Child()
    progress_overlay = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.multistage_process: MultiStageProcess | None = None
        # Bottom bar is as high as progress bar, which is drawn over line.
        self.progress_overlay.set_measure_overlay(self.progress_bar, True)
        # Result of finished process is seen when this view shows it.
        self.connect("map", lambda widget: self._mark_result_seen())

    def set_multistage_process(self, multistage_process: MultiStageProcess | None = None):
        """Call when multistage_process is started"""
        if self.multistage_process is not None:
            raise RuntimeError("multistage_process already set")
        if multistage_process:
            if multistage_process.status == MultiStageProcessState.SETUP:
                raise RuntimeError("multistage_process needs to be started before connecting")
            self.multistage_process = multistage_process
            self.bind_installation_events(multistage_process)
            self.process_steps_list.set_title(title=multistage_process.title)
            self.progress_bar.set_fraction(multistage_process.progress)
            self._update_installation_steps(steps=multistage_process.stages)
            self._set_current_stage(multistage_process.status)

    def bind_installation_events(self, multistage_process: MultiStageProcess):
        multistage_process.event_bus.subscribe(
            MultiStageProcessEvent.STATE_CHANGED, self._set_current_stage
        )
        multistage_process.event_bus.subscribe(
            MultiStageProcessEvent.PROGRESS_CHANGED, self._update_progress
        )

    @Gtk.Template.Callback()
    def on_cancel_pressed(self, _):
        from .build_queue import BuildQueue
        from .project_build_process import ProjectBuild
        queue = BuildQueue.shared()
        if not isinstance(self.multistage_process, ProjectBuild) or not queue.entries:
            self.multistage_process.cancel()
            return
        # Builds are queued: next one starts when this one is cancelled, unless queue is cancelled too.
        count = len(queue.entries)
        dialog = Adw.AlertDialog(heading="Cancel build?", body=(
            f"{count} queued build{'s' if count != 1 else ''} start{'s' if count == 1 else ''} after this one. "
            "Cancel only this build, or the queue too?"))
        dialog.add_response("keep", "Keep building")
        dialog.add_response("build", "Cancel build")
        dialog.add_response("queue", "Cancel build and queue")
        dialog.set_response_appearance("build", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("queue", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep")
        dialog.set_close_response("keep")
        def on_response(dialog, response):
            if response == "queue":
                queue.clear()
            if response in ("build", "queue"):
                self.multistage_process.cancel()
        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    @Gtk.Template.Callback()
    def on_finish_pressed(self, _):
        self.multistage_process.clean_from_started_processes()
        if hasattr(self, "_window"):
            self._window.close()
        elif hasattr(self, "content_navigation_view"):
            self.content_navigation_view.pop()

    def _update_progress(self, progress):
        self.progress_bar.set_fraction(self.multistage_process.progress)

    def _update_installation_steps(self, steps: list[MultiStageProcessStage]):
        if hasattr(self, "_installation_rows"):
            for row in self._installation_rows:
                self.process_steps_list.remove(row)
        self._installation_rows = []
        tools_check_buttons_group = []
        running_stage_row = None
        for step in steps:
            row = MultiStageProcessStageRow(step=step, owner=self)
            self.process_steps_list.add(row)
            self._installation_rows.append(row)
            if step.state == MultiStageProcessStageState.IN_PROGRESS:
                running_stage_row = row
        if running_stage_row:
            GLib.idle_add(self._scroll_to_installation_step_row, running_stage_row)

    def _scroll_to_installation_step_row(self, row: MultiStageProcessStageRow):
        def _scroll(widget):
            scrolled_window = self.process_steps_list.get_ancestor(Gtk.ScrolledWindow)
            vadjustment = scrolled_window.get_vadjustment()
            _, y = row.translate_coordinates(self.process_steps_list, 0, 0)
            row_height = row.get_allocated_height()
            visible_height = vadjustment.get_page_size()
            center_y = y + row_height / 2 - visible_height / 2
            max_value = vadjustment.get_upper() - vadjustment.get_page_size()
            scroll_to = max(0, min(center_y, max_value))
            vadjustment.set_value(scroll_to)
        GLib.idle_add(_scroll, row)

    def _scroll_to_installation_steps_bottom(self):
        def _scroll():
            scrolled_window = self.process_steps_list.get_ancestor(Gtk.ScrolledWindow)
            vadjustment = scrolled_window.get_vadjustment()
            bottom = vadjustment.get_upper() - vadjustment.get_page_size()
            vadjustment.set_value(bottom)
        GLib.timeout_add(100, _scroll)

    def _mark_result_seen(self):
        process = self.multistage_process
        if process is not None and self.get_mapped() and process.status in (MultiStageProcessState.COMPLETED, MultiStageProcessState.FAILED):
            process.mark_result_seen()

    def _set_current_stage(self, stage: MultiStageProcessState):
        self._mark_result_seen()
        self.cancel_button.set_visible(stage == MultiStageProcessState.IN_PROGRESS)
        self.finish_button.set_visible(stage != MultiStageProcessState.IN_PROGRESS)
        # Add label with summary for completion states:
        def display_status(text: str, style: str | None):
            label = Gtk.Label(label=text)
            label.set_margin_top(12)
            label.set_margin_bottom(12)
            label.set_margin_start(24)
            label.set_margin_end(24)
            label.add_css_class("heading")
            if style:
                label.add_css_class(style)
            self.process_steps_list.add(label)
            self._scroll_to_installation_steps_bottom()
        match stage:
            case MultiStageProcessState.COMPLETED:
                display_status(text="Completed successfully.", style="success")
            case MultiStageProcessState.FAILED:
                display_status(text="Failed. Open failed steps to see their output.", style="error")

class MultiStageProcessStageRow(Adw.ActionRow):
    """Displays stage state. Can be activated to open output of commands executed by stage."""

    def __init__(self, step: MultiStageProcessStage, owner: MultistageProcessExecutionView):
        super().__init__(title=step.name, subtitle=step.description, subtitle_lines=1)
        self.step = step
        self.owner = owner
        self._last_line: str | None = None # Last output line, shown as subtitle while step runs.
        self._subtitle_update_id = None
        self.progress_label = Gtk.Label()
        self.progress_label.add_css_class("dim-label")
        self.progress_label.add_css_class("caption")
        self.add_suffix(self.progress_label)
        self.output_arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        self.add_suffix(self.output_arrow)
        self.connect("activated", self._on_activated)
        self._update_output_available()
        step.event_bus.subscribe(
            MultiStageProcessStageEvent.STATE_CHANGED,
            self._step_state_changed
        )
        step.event_bus.subscribe(
            MultiStageProcessStageEvent.PROGRESS_CHANGED,
            self._step_progress_changed
        )
        step.event_bus.subscribe(
            MultiStageProcessStageEvent.OUTPUT_LINE_ADDED,
            self._step_output_line_added
        )
        # A worker can finish while this row is being built. Read its current state
        # after subscribing so completion cannot fall between the snapshot and subscription.
        self.set_sensitive(step.state != MultiStageProcessStageState.SCHEDULED)
        self._set_status_icon(state=step.state)
        self._update_status_label()
        # View opened while step runs shows its last output line right away.
        if step.state == MultiStageProcessStageState.IN_PROGRESS:
            self._last_line = next((text for line in reversed(step.output_lines) if (text := _plain_text(line))), None)
            self._update_subtitle()

    def _update_output_available(self):
        """Allow opening output only when there is some output to show."""
        has_output = bool(self.step.output_lines)
        self.set_activatable(has_output)
        self.output_arrow.set_visible(has_output)

    def _on_activated(self, row):
        navigation_view = getattr(self.owner, "content_navigation_view", None) or self.get_ancestor(Adw.NavigationView)
        if navigation_view is None:
            print("Warning: No navigation view to show step output in.")
            return
        navigation_view.push_view(MultiStageProcessStageOutputView(step=self.step), title=self.step.name)

    def _step_output_line_added(self, line: str):
        if not self.get_activatable():
            self._update_output_available()
        if self.step.state == MultiStageProcessStageState.IN_PROGRESS:
            if text := _plain_text(line):
                self._last_line = text
                # Output can be fast, subtitle is updated a few times per second.
                if self._subtitle_update_id is None:
                    self._subtitle_update_id = GLib.timeout_add(250, self._update_subtitle)

    def _update_subtitle(self):
        self._subtitle_update_id = None
        running = self.step.state == MultiStageProcessStageState.IN_PROGRESS
        self.set_subtitle(GLib.markup_escape_text(self._last_line if running and self._last_line else self.step.description))
        return False

    def _step_progress_changed(self, progress: float | None):
        self._update_status_label()

    def _step_state_changed(self, state: MultiStageProcessStageState):
        self.set_sensitive(state != MultiStageProcessStageState.SCHEDULED)
        self._set_status_icon(state=state)
        self.owner._scroll_to_installation_step_row(self)
        self._update_status_label()
        if state != MultiStageProcessStageState.IN_PROGRESS:
            self._update_subtitle() # Description again.

    def _update_status_label(self):
        self.progress_label.set_label(
            "" if self.step.state == MultiStageProcessStageState.SCHEDULED else ("..." if self.step.progress is None else f"{int(self.step.progress * 100)}%")
        )

    def _set_status_icon(self, state: MultiStageProcessStageState):
        if not hasattr(self, "status_icon"):
            self.status_icon = Gtk.Image()
            self.status_icon.set_pixel_size(24)
            self.add_prefix(self.status_icon)
        icon_name = {
            MultiStageProcessStageState.SCHEDULED: "square-alt-arrow-right-svgrepo-com-symbolic",
            MultiStageProcessStageState.IN_PROGRESS: "menu-dots-square-svgrepo-com-symbolic",
            MultiStageProcessStageState.FAILED: "error-box-svgrepo-com-symbolic",
            MultiStageProcessStageState.COMPLETED: "check-square-svgrepo-com-symbolic"
        }.get(state)
        styles = {
            MultiStageProcessStageState.SCHEDULED: "dimmed",
            MultiStageProcessStageState.IN_PROGRESS: "",
            MultiStageProcessStageState.FAILED: "error",
            MultiStageProcessStageState.COMPLETED: "success"
        }
        style = styles.get(state)
        self.status_icon.set_from_icon_name(icon_name)
        for css_class in styles.values():
            if css_class:
                self.status_icon.remove_css_class(css_class)
        if style:
            self.status_icon.add_css_class(style)

class MultiStageProcessStageOutputView(Gtk.Box):
    """Displays output of commands executed by stage. Pushed when stage row is activated."""

    def __init__(self, step: MultiStageProcessStage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.step = step
        self.output_view = Gtk.TextView()
        self.output_view.set_editable(False)
        self.output_view.set_cursor_visible(False)
        self.output_view.set_monospace(True)
        self.output_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.output_view.set_top_margin(12)
        self.output_view.set_bottom_margin(12)
        self.output_view.set_left_margin(12)
        self.output_view.set_right_margin(12)
        self.output_view.add_css_class("transparent-bg")
        self.output_buffer = self.output_view.get_buffer()
        self._ansi_style = AnsiStyle()
        for line in self.step.output_lines:
            self._append_output_line(line)
        self.output_scrolled_window = Gtk.ScrolledWindow()
        self.output_scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.output_scrolled_window.set_hexpand(True)
        self.output_scrolled_window.set_vexpand(True)
        self.output_scrolled_window.set_child(self.output_view)
        self.append(self.output_scrolled_window)
        # Keep output scrolled to the bottom while new lines arrive. Stop following when user
        # scrolls up, resume when user scrolls back to the bottom.
        # Only user input changes following, as GtkTextView also moves scroll position by
        # itself while measuring lines.
        self._follow_output = True
        self._user_scroll_time = 0
        adjustment = self.output_scrolled_window.get_vadjustment()
        adjustment.connect("value-changed", self._on_output_scrolled)
        adjustment.connect("changed", self._on_output_size_changed)
        scroll_controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.KINETIC
        )
        scroll_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll_controller.connect("scroll", lambda *args: self._mark_user_scroll() or False)
        scroll_controller.connect("decelerate", lambda *args: self._mark_user_scroll())
        self.output_scrolled_window.add_controller(scroll_controller)
        scrollbar_drag = Gtk.GestureDrag()
        scrollbar_drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scrollbar_drag.connect("drag-begin", lambda *args: self._mark_user_scroll())
        scrollbar_drag.connect("drag-update", lambda *args: self._mark_user_scroll())
        self.output_scrolled_window.get_vscrollbar().add_controller(scrollbar_drag)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", lambda *args: self._mark_user_scroll() or False)
        self.output_view.add_controller(key_controller)
        # Show the latest output when view is shown. Text is laid out after it's mapped, so scroll again once it's measured.
        self.output_view.connect("map", lambda *args: GLib.idle_add(lambda: self._scroll_output_to_end() and False))
        step.event_bus.subscribe(
            MultiStageProcessStageEvent.OUTPUT_LINE_ADDED,
            self._step_output_line_added
        )

    def _step_output_line_added(self, line: str):
        self._append_output_line(line)

    def _append_output_line(self, line: str):
        """Appends line, with colors and styles from its ANSI escape sequences."""
        self._ansi_style = append_ansi_line(self.output_buffer, line, self._ansi_style)

    def _is_output_at_bottom(self) -> bool:
        adjustment = self.output_scrolled_window.get_vadjustment()
        return adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 1

    def _mark_user_scroll(self):
        self._user_scroll_time = GLib.get_monotonic_time()

    def _on_output_scrolled(self, adjustment: Gtk.Adjustment):
        # Scroll position changes within a second after user input are made by user (including kinetic scrolling).
        if GLib.get_monotonic_time() - self._user_scroll_time < 1_000_000:
            self._follow_output = self._is_output_at_bottom()
        elif self._follow_output and not self._is_output_at_bottom():
            self._scroll_output_to_end()

    def _on_output_size_changed(self, adjustment: Gtk.Adjustment):
        # Content grew (new lines, or view was laid out).
        if self._follow_output and not self._is_output_at_bottom():
            self._scroll_output_to_end()

    def _scroll_output_to_end(self):
        # GtkTextView (GTK 4.20) crashes when scrolled before it's realized.
        # Once it's shown, size change of adjustment scrolls it to the end.
        if not self.output_view.get_realized():
            return
        adjustment = self.output_scrolled_window.get_vadjustment()
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

# ------------------------------------------------------------------------------
# ANSI escape sequences in command output:
# ------------------------------------------------------------------------------

def _plain_text(line: str) -> str:
    """Line without ANSI sequences."""
    segments, _ = parse_ansi_line(line, AnsiStyle())
    return "".join(text for text, _ in segments).strip()

@dataclass(frozen=True)
class AnsiStyle:
    foreground: str | None = None
    background: str | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    inverse: bool = False

# Colors readable on both light and dark background. Black and white are shown as grey, as one of them is
# always invisible. Bright variants use the same colors.
_ANSI_COLORS = ["#77767b", "#e01b24", "#26a269", "#c88800", "#3584e4", "#c061cb", "#2aa1b3", "#9a9996"]
_ANSI_SEQUENCE = re.compile(r"\x1b(?:\[([0-9;:?]*)([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|[@-Z\\-_])")

def parse_ansi_line(line: str, style: AnsiStyle) -> tuple[list[tuple[str, AnsiStyle]], AnsiStyle]:
    """Splits line into text segments with their styles. Returns segments and style at the end of line,
    which continues in next line."""
    # Carriage return rewrites the line (progress bars), only its last version is shown.
    if "\r" in line:
        line = next((part for part in reversed(line.split("\r")) if part), "")
    segments = []
    position = 0
    for match in _ANSI_SEQUENCE.finditer(line):
        if match.start() > position:
            segments.append((line[position:match.start()], style))
        position = match.end()
        if match.group(2) == "m": # Other sequences (cursor movement, titles, charsets) are dropped.
            style = _apply_sgr(style, match.group(1))
    if position < len(line):
        segments.append((line[position:], style))
    return segments, style

def _apply_sgr(style: AnsiStyle, parameters: str) -> AnsiStyle:
    codes = [int(code) if code.isdigit() else 0 for code in re.split("[;:]", parameters)] if parameters else [0]
    index = 0
    while index < len(codes):
        code = codes[index]
        match code:
            case 0: style = AnsiStyle()
            case 1: style = replace(style, bold=True)
            case 2: style = replace(style, dim=True)
            case 3: style = replace(style, italic=True)
            case 4: style = replace(style, underline=True)
            case 7: style = replace(style, inverse=True)
            case 22: style = replace(style, bold=False, dim=False)
            case 23: style = replace(style, italic=False)
            case 24: style = replace(style, underline=False)
            case 27: style = replace(style, inverse=False)
            case 39: style = replace(style, foreground=None)
            case 49: style = replace(style, background=None)
            case _ if 30 <= code <= 37: style = replace(style, foreground=_ANSI_COLORS[code - 30])
            case _ if 90 <= code <= 97: style = replace(style, foreground=_ANSI_COLORS[code - 90])
            case _ if 40 <= code <= 47: style = replace(style, background=_ANSI_COLORS[code - 40])
            case _ if 100 <= code <= 107: style = replace(style, background=_ANSI_COLORS[code - 100])
            case 38 | 48:
                color, used = _extended_color(codes[index + 1:])
                index += used
                if color:
                    style = replace(style, **{"foreground" if code == 38 else "background": color})
        index += 1
    return style

def _extended_color(codes: list[int]) -> tuple[str | None, int]:
    """Color from 256 colors (5;n) or RGB (2;r;g;b) parameters, and number of parameters used."""
    if len(codes) >= 2 and codes[0] == 5:
        number = codes[1]
        if number < 16:
            return _ANSI_COLORS[number % 8], 2
        if number < 232:
            number -= 16
            levels = [0, 95, 135, 175, 215, 255]
            return "#{:02x}{:02x}{:02x}".format(levels[number // 36], levels[number // 6 % 6], levels[number % 6]), 2
        grey = 8 + (number - 232) * 10
        return "#{0:02x}{0:02x}{0:02x}".format(grey), 2
    if len(codes) >= 4 and codes[0] == 2:
        return "#{:02x}{:02x}{:02x}".format(*(min(max(value, 0), 255) for value in codes[1:4])), 4
    return None, len(codes)

def _dimmed(color: str | None):
    from gi.repository import Gdk
    rgba = Gdk.RGBA()
    rgba.parse(color or "#808080")
    rgba.alpha = 0.6
    return rgba

def append_ansi_line(buffer: Gtk.TextBuffer, line: str, style: AnsiStyle) -> AnsiStyle:
    """Appends line to buffer with colors and styles of its ANSI escape sequences. Returns style at end of line
    (continues in next line)."""
    if buffer.get_char_count() > 0:
        buffer.insert(buffer.get_end_iter(), "\n")
    segments, style = parse_ansi_line(line, style)
    for text, segment_style in segments:
        tag = _tag_for_style(buffer, segment_style)
        if tag:
            buffer.insert_with_tags(buffer.get_end_iter(), text, tag)
        else:
            buffer.insert(buffer.get_end_iter(), text)
    return style

def _tag_for_style(buffer: Gtk.TextBuffer, style: AnsiStyle) -> Gtk.TextTag | None:
    if style == AnsiStyle():
        return None
    name = f"ansi-{style}"
    tag_table = buffer.get_tag_table()
    if tag := tag_table.lookup(name):
        return tag
    foreground, background = style.foreground, style.background
    if style.inverse: # Default colors are unknown here, inverse of them uses grey background.
        foreground, background = background or "#ffffff", foreground or _ANSI_COLORS[0]
    tag = Gtk.TextTag(name=name)
    if foreground:
        tag.set_property("foreground", foreground)
    if background:
        tag.set_property("background", background)
    if style.bold:
        tag.set_property("weight", Pango.Weight.BOLD)
    if style.dim:
        tag.set_property("foreground-rgba", _dimmed(foreground))
    if style.italic:
        tag.set_property("style", Pango.Style.ITALIC)
    if style.underline:
        tag.set_property("underline", Pango.Underline.SINGLE)
    tag_table.add(tag)
    return tag
