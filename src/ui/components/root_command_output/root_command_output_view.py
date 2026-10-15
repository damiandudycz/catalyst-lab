import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk
from .root_helper_client import ServerCall, ServerCallEvents

@Gtk.Template(resource_path='/com/damiandudycz/CatalystLab/ui/components/root_command_output/root_command_output_view.ui')
class RootCommandOutputView(Gtk.Box):
    __gtype_name__ = 'RootCommandOutputView'

    text_view = Gtk.Template.Child()

    def __init__(self, call: ServerCall):
        super().__init__()
        self.text_buffer = self.text_view.get_buffer()
        self.text_buffer.set_text("\n".join(call.get_output()))
        end_iter = self.text_buffer.get_end_iter()
        self.text_mark_end = self.text_buffer.create_mark("", end_iter, False)
        call.event_bus.subscribe(
            ServerCallEvents.NEW_OUTPUT_LINE,
            self.append_line
        )

    def append_line(self, line: str):
        # Get the current end iterator to ensure we insert at the very end
        end_iter = self.text_buffer.get_end_iter()
        self.text_buffer.insert(end_iter, "\n" + line)
        self.text_view.scroll_to_mark(self.text_mark_end, 0, True, 0, 0)

