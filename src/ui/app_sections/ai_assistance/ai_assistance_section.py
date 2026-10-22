import threading

from gi.repository import Gtk, Adw, GLib

from .app_section import app_section
from .repository import Repository
from .ollama_client import OllamaClient, OllamaError
from .formatters import format_size
from .ollama_chat_view import OllamaChatView


@app_section(title="OpenImmoloEye", icon="applications-science-symbolic", order=8_000)
class AIAssistanceSection(Gtk.Box):
    """Configure Ollama, download models, and start a chat."""
    __gtype_name__ = "AIAssistanceSection"

    FEATURED_MODELS = (
        ("qwen3.5:2b", "Compact general-purpose model"),
        ("qwen3.5:4b", "General-purpose model"),
        ("gemma4:e2b", "Small Gemma model"),
        ("llama3.2:1b", "Small text model"),
    )

    def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
        self.content_navigation_view = content_navigation_view
        self._request_generation = 0
        self._pull_generation = 0
        self._url_change_source = None
        self._updating_url = False

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                     hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                          margin_start=24, margin_end=24, margin_top=18, margin_bottom=24)
        scroller.set_child(content)
        self.append(scroller)

        connection_group = Adw.PreferencesGroup(title="Ollama")
        content.append(connection_group)
        self.url_row = Adw.EntryRow(title="Server URL")
        self.url_row.set_text(Repository.Settings.value.ollama_url)
        self.url_row.connect("notify::text", self._url_changed)
        connection_group.add(self.url_row)

        self.model_row = Adw.ComboRow(title="Model")
        self.models = Gtk.StringList.new(["Checking connection…"])
        self.model_row.set_model(self.models)
        self.model_row.set_selected(0)
        self.model_row.set_sensitive(False)
        self.model_row.connect("notify::selected", self._model_selected)
        connection_group.add(self.model_row)

        self.connection_row = Adw.ActionRow(title="Connection", subtitle="Checking Ollama…")
        self.connection_row.add_prefix(Gtk.Image.new_from_icon_name("network-transmit-receive-symbolic"))
        connection_group.add(self.connection_row)

        self.installed_group = Adw.PreferencesGroup(
            title="Downloaded models", description="Models installed on the configured Ollama server.")
        content.append(self.installed_group)
        self.installed_placeholder = Adw.ActionRow(title="Checking installed models…")
        self.installed_group.add(self.installed_placeholder)
        self.installed_rows = []
        self._installed_model_names = set()

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          margin_start=12, margin_end=12, margin_top=12)
        self.check_button = Gtk.Button(label="Check connection")
        self.check_button.connect("clicked", self._connect_clicked)
        actions.append(self.check_button)
        self.chat_button = Gtk.Button(label="Chat", sensitive=False)
        self.chat_button.add_css_class("suggested-action")
        self.chat_button.connect("clicked", self._open_chat)
        actions.append(self.chat_button)
        connection_group.add(actions)

        self._build_download_controls(content)
        self._connect_clicked()

    def _build_download_controls(self, content: Gtk.Box):
        self.download_expander = Gtk.Expander(label="Download models", expanded=False)
        content.append(self.download_expander)
        download_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                  margin_start=12, margin_end=12, margin_top=12)
        self.download_expander.set_child(download_content)

        download_group = Adw.PreferencesGroup(
            description="Enter a model name from the Ollama library, or choose a popular model below.")
        download_content.append(download_group)
        self.pull_name_row = Adw.EntryRow(title="Model name")
        download_group.add(self.pull_name_row)

        download_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                                   margin_start=12, margin_end=12, margin_top=12)
        self.pull_button = Gtk.Button(label="Download model", halign=Gtk.Align.START)
        self.pull_button.connect("clicked", self._pull_clicked)
        download_actions.append(self.pull_button)
        self.pull_status = Gtk.Label(label="", xalign=0, valign=Gtk.Align.CENTER,
                                     hexpand=True, wrap=True)
        self.pull_status.add_css_class("dimmed")
        download_actions.append(self.pull_status)
        download_group.add(download_actions)
        self.pull_progress = Gtk.ProgressBar(show_text=True, hexpand=True,
                                             margin_start=12, margin_end=12, margin_bottom=12)
        self.pull_progress.set_visible(False)
        download_group.add(self.pull_progress)

        catalog_group = Adw.PreferencesGroup(
            title="Popular models", description="See the full catalog at ollama.com/library.")
        download_content.append(catalog_group)
        self.featured_buttons = []
        self.featured_rows = []
        for model, description in self.FEATURED_MODELS:
            row = Adw.ActionRow(title=model, subtitle=description)
            button = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
            button.connect("clicked", self._featured_model_clicked, model)
            row.add_suffix(button)
            row.set_activatable_widget(button)
            catalog_group.add(row)
            self.featured_buttons.append(button)
            self.featured_rows.append((row, button, model, description))
        catalog_group.add(Gtk.LinkButton(uri="https://ollama.com/library", label="Browse all models",
                                         halign=Gtk.Align.START, margin_start=12))

    def _url_changed(self, *_args):
        if self._updating_url:
            return
        if self._url_change_source is not None:
            GLib.source_remove(self._url_change_source)
        self._url_change_source = GLib.timeout_add(700, self._check_changed_url)

    def _check_changed_url(self):
        self._url_change_source = None
        self._connect_clicked()
        return GLib.SOURCE_REMOVE

    def _server_url(self) -> str:
        url = self.url_row.get_text().strip().rstrip("/") or "http://localhost:11434"
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        if self.url_row.get_text() != url:
            self._updating_url = True
            self.url_row.set_text(url)
            self._updating_url = False
        return url

    def _connect_clicked(self, *_args):
        url = self._server_url()
        Repository.Settings.value.ollama_url = url
        self._request_generation += 1
        generation = self._request_generation
        self.check_button.set_sensitive(False)
        self.check_button.set_label("Checking…")
        self.chat_button.set_sensitive(False)
        self.connection_row.set_subtitle(f"Connecting to {url}…")

        def load_models():
            try:
                models = OllamaClient(url, timeout=8).list_models()
                error = None
            except (OllamaError, ValueError) as exception:
                models, error = [], str(exception)
            GLib.idle_add(self._show_models, generation, models, error)

        threading.Thread(target=load_models, name="ollama-model-discovery", daemon=True).start()

    def _show_models(self, generation: int, models: list[dict], error: str | None):
        if generation != self._request_generation:
            return GLib.SOURCE_REMOVE
        self.check_button.set_sensitive(True)
        self.check_button.set_label("Check connection")
        while self.models.get_n_items():
            self.models.remove(0)
        if error:
            self._show_installed_models([], error)
            self.models.append("Unavailable")
            self.model_row.set_selected(0)
            self.model_row.set_sensitive(False)
            self.chat_button.set_sensitive(False)
            self.connection_row.set_subtitle(error)
            self.connection_row.remove_css_class("success")
            self.connection_row.add_css_class("error")
            return GLib.SOURCE_REMOVE

        names = [item.get("name") for item in models if isinstance(item, dict) and item.get("name")]
        selected_model = Repository.Settings.value.ollama_model
        if names:
            for name in names:
                self.models.append(name)
            selected_index = names.index(selected_model) if selected_model in names else 0
            self.model_row.set_sensitive(True)
            self.model_row.set_selected(selected_index)
            Repository.Settings.value.ollama_model = names[selected_index]
            self.chat_button.set_sensitive(True)
            detail = f"Connected · {len(names)} model{'s' if len(names) != 1 else ''} available"
        else:
            self.models.append("No models installed")
            self.model_row.set_selected(0)
            self.model_row.set_sensitive(False)
            self.chat_button.set_sensitive(False)
            detail = "Connected · download a model to start chatting"
        self._show_installed_models(models, None)
        self.connection_row.set_subtitle(detail)
        self.connection_row.remove_css_class("error")
        self.connection_row.add_css_class("success")
        return GLib.SOURCE_REMOVE

    def _show_installed_models(self, models: list[dict], error: str | None):
        for row in self.installed_rows:
            self.installed_group.remove(row)
        self.installed_rows.clear()
        if self.installed_placeholder.get_parent() is self.installed_group:
            self.installed_group.remove(self.installed_placeholder)
        self._installed_model_names = {
            item.get("name") for item in models
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("name")
        }
        if error:
            self.installed_placeholder.set_title("Could not load downloaded models")
            self.installed_placeholder.set_subtitle(error)
            self.installed_group.add(self.installed_placeholder)
        elif not self._installed_model_names:
            self.installed_placeholder.set_title("No models downloaded")
            self.installed_placeholder.set_subtitle("Choose a popular model below or enter a model name to download.")
            self.installed_group.add(self.installed_placeholder)
        else:
            selected_model = Repository.Settings.value.ollama_model
            for item in models:
                if not isinstance(item, dict) or item.get("name") not in self._installed_model_names:
                    continue
                name = item["name"]
                size = item.get("size")
                detail = "Currently selected" if name == selected_model else "Installed"
                if isinstance(size, int) and size > 0:
                    detail += f" · {format_size(size)}"
                row = Adw.ActionRow(title=name, subtitle=detail)
                button = Gtk.Button(label="Using" if name == selected_model else "Use",
                                    valign=Gtk.Align.CENTER, sensitive=name != selected_model)
                button.connect("clicked", self._use_model, name)
                row.add_suffix(button)
                row.set_activatable_widget(button)
                self.installed_group.add(row)
                self.installed_rows.append(row)
        for row, button, name, description in self.featured_rows:
            installed = name in self._installed_model_names
            button.set_label("Use" if installed else "Download")
            button.set_tooltip_text("Select this installed model" if installed else f"Download {name}")
            row.set_subtitle("Downloaded model" if installed else description)
        return GLib.SOURCE_REMOVE

    def _use_model(self, _button, model: str):
        if model not in self._installed_model_names:
            self._connect_clicked()
            return
        names = [self.models.get_string(index) for index in range(self.models.get_n_items())]
        if model in names:
            self.model_row.set_selected(names.index(model))
        Repository.Settings.value.ollama_model = model
        self._show_installed_models(
            [{"name": name} for name in self._installed_model_names], None)
        self.connection_row.set_subtitle(f"Connected · Using {model}")

    def _featured_model_clicked(self, _button, model: str):
        if model in self._installed_model_names:
            self._use_model(None, model)
        else:
            self._download_featured_model(None, model)

    def _model_selected(self, row, _param):
        if row.get_sensitive():
            index = row.get_selected()
            if index != Gtk.INVALID_LIST_POSITION and index < self.models.get_n_items():
                model = self.models.get_string(index)
                if model not in ("Unavailable", "No models installed", "Checking connection…"):
                    Repository.Settings.value.ollama_model = model

    def _open_chat(self, *_args):
        settings = Repository.Settings.value
        navigation_view = self.content_navigation_view
        chat = OllamaChatView(settings.ollama_url, settings.ollama_model,
                              content_navigation_view=navigation_view)
        if chat.debug_conversation_logging:
            handler_id = None

            def chat_popped(_navigation_view, page):
                if page is chat._page:
                    navigation_view.disconnect(handler_id)
                    chat.save_debug_conversation(force=True)

            handler_id = navigation_view.connect("popped", chat_popped)
        navigation_view.push_view(chat, title="Chat")

    def _pull_clicked(self, *_args):
        model = self.pull_name_row.get_text().strip()
        if not model:
            self.pull_status.set_text("Enter a model name first.")
            self.pull_status.remove_css_class("dimmed")
            self.pull_status.add_css_class("error")
            return
        url = self._server_url()
        Repository.Settings.value.ollama_url = url
        self._pull_generation += 1
        generation = self._pull_generation
        self.pull_button.set_sensitive(False)
        for button in self.featured_buttons:
            button.set_sensitive(False)
        self.pull_name_row.set_sensitive(False)
        self.pull_progress.set_fraction(0)
        self.pull_progress.set_text("Starting…")
        self.pull_progress.set_visible(True)
        self.pull_status.set_text(f"Preparing {model}…")
        self.pull_status.remove_css_class("error")
        self.pull_status.add_css_class("dimmed")

        def report(event: dict):
            GLib.idle_add(self._update_pull_progress, generation, event)

        def download():
            try:
                OllamaClient(url, timeout=60).pull_model(model, progress=report)
                error = None
            except (OllamaError, ValueError) as exception:
                error = str(exception)
            GLib.idle_add(self._finish_pull, generation, model, error)

        threading.Thread(target=download, name="ollama-model-download", daemon=True).start()

    def _update_pull_progress(self, generation: int, event: dict):
        if generation != self._pull_generation:
            return GLib.SOURCE_REMOVE
        status = event.get("status", "Downloading")
        total, completed = event.get("total"), event.get("completed", 0)
        if isinstance(total, (int, float)) and total > 0:
            fraction = min(1.0, max(0.0, completed / total))
            self.pull_progress.set_fraction(fraction)
            self.pull_progress.set_text(f"{format_size(completed)} / {format_size(total)}")
        else:
            self.pull_progress.pulse()
            self.pull_progress.set_text(status)
        self.pull_status.set_text(status)
        return GLib.SOURCE_REMOVE

    def _download_featured_model(self, _button, model: str):
        self.pull_name_row.set_text(model)
        self._pull_clicked()

    def _finish_pull(self, generation: int, model: str, error: str | None):
        if generation != self._pull_generation:
            return GLib.SOURCE_REMOVE
        self.pull_button.set_sensitive(True)
        for button in self.featured_buttons:
            button.set_sensitive(True)
        self.pull_name_row.set_sensitive(True)
        if error:
            self.pull_status.set_text(error)
            self.pull_status.remove_css_class("dimmed")
            self.pull_status.add_css_class("error")
            self.pull_progress.set_text("Download failed")
            return GLib.SOURCE_REMOVE
        Repository.Settings.value.ollama_model = model
        self.pull_progress.set_fraction(1.0)
        self.pull_progress.set_text("Complete")
        self.pull_status.set_text(f"Downloaded {model}")
        self.pull_status.remove_css_class("error")
        self.pull_status.add_css_class("success")
        self._connect_clicked()
        return GLib.SOURCE_REMOVE
