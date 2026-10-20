from __future__ import annotations
from gi.repository import Gtk, Adw, GObject
from .multistage_process import MultiStageProcess, MultiStageProcessState
from .multistage_process_execution_view import MultistageProcessExecutionView

class WizardView(Adw.Bin, Gtk.Buildable):
    __gtype_name__ = "WizardView"

    __gsignals__ = {
        "is-page-ready-to-continue": (GObject.SignalFlags.RUN_FIRST, bool, (Gtk.Widget,)),
        "begin-installation": (GObject.SignalFlags.RUN_FIRST, None, ())
    }

    show_welcome_screen = GObject.Property(type=bool, default=False)
    welcome_screen_icon_name = GObject.Property(type=str, default=None)
    welcome_screen_title = GObject.Property(type=str, default=None)
    welcome_screen_description = GObject.Property(type=str, default=None)
    install_button_label = GObject.Property(type=str, default="Start installation") # Next button title on last page.
    # Setup is shown and it's not on first page: back of navigation (in main window) goes to previous page instead.
    can_go_back = GObject.Property(type=bool, default=False)

    def __init__(
        self, installation_in_progress: MultiStageProcess | None = None,
        content_navigation_view: Adw.NavigationView | None = None
    ):
        super().__init__()
        # Main objects:
        self.installation_in_progress = installation_in_progress
        # Navigation:
        self.content_navigation_view = content_navigation_view
        # Setup view box container:
        self.setup_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Install view box container:
        self.install_view = MultistageProcessExecutionView()
        # Carousel:
        self.carousel = Adw.Carousel()
        self.carousel.set_vexpand(True)
        # Pages are changed only with Back / Next buttons, scrolling and swiping could skip pages that are not ready.
        self.carousel.set_interactive(False)
        self.setup_view.append(self.carousel)
        # Bottom bar:
        self._setup_bottom_bar()
        self.setup_view.append(Gtk.Separator()) # Line above bottom bar, like in other bottom bars.
        self.setup_view.append(self.bottom_bar)
        # Welcome page:
        self.welcome_page: Gtk.Widget = None
        # State:
        self.current_page = 0
        self.pages: list[Gtk.Widget] = []
        self._all_pages: list[Gtk.Widget] = [] # Also hidden pages, in order of adding (keeps them alive while hidden).
        # Optional callback for views that need to confirm leaving a page (eg. a generated-file review page).
        # Return True when the callback handled the request asynchronously.
        self.back_interceptor = None
        # Signals:
        self.carousel.connect('page-changed', self.on_page_changed)
        self.connect("notify::parent", lambda *args: self._update_can_go_back())
        self.connect("realize", self.on_realize)

    def on_realize(self, widget):
        self.install_view.content_navigation_view = self.content_navigation_view
        self.install_view._window = self._window
        self.set_installation(self.installation_in_progress)
        self._setup_welcome_page()
        self._refresh_buttons_state()

    def _setup_welcome_page(self):
        # Realize can happen again (eg. when view pushed in main window is shown again), welcome page is added once.
        if not self.show_welcome_screen or getattr(self, "_welcome_page_set_up", False):
            return
        self._welcome_page_set_up = True
        # Root: GtkScrolledWindow
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)
        # Main vertical GtkBox
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        vbox.set_valign(Gtk.Align.CENTER)
        vbox.set_vexpand(True)
        vbox.set_hexpand(True)
        vbox.set_margin_start(24)
        vbox.set_margin_end(24)
        # Image
        if self.welcome_screen_icon_name:
            image = Gtk.Image.new_from_icon_name(self.welcome_screen_icon_name)
            image.set_pixel_size(128)
            vbox.append(image)
        # Title Label
        if self.welcome_screen_title:
            title_label = Gtk.Label(label=self.welcome_screen_title)
            title_label.set_halign(Gtk.Align.CENTER)
            title_label.set_wrap(True)
            title_label.get_style_context().add_class("title-1")
            vbox.append(title_label)
        # Subtitle Label
        if self.welcome_screen_description:
            subtitle_label = Gtk.Label(label=self.welcome_screen_description)
            subtitle_label.set_halign(Gtk.Align.CENTER)
            subtitle_label.set_wrap(True)
            subtitle_label.set_justify(Gtk.Justification.CENTER)
            vbox.append(subtitle_label)
        # Button container
        inner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner_box.set_halign(Gtk.Align.CENTER)
        # ListBox
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.get_style_context().add_class("boxed-list")
        # AdwButtonRow
        button_row = Adw.ButtonRow(title="Get started")
        button_row.set_activatable(True)
        button_row.get_style_context().add_class("suggested-action")
        button_row.connect("activated", self.on_start_row_activated)
        list_box.append(button_row)
        inner_box.append(list_box)
        vbox.append(inner_box)
        scrolled_window.set_child(vbox)
        # Add welcome page
        self.welcome_page = scrolled_window
        self.carousel.prepend(scrolled_window)
        self.pages.insert(0, scrolled_window)
        self._all_pages.insert(0, scrolled_window) # Counted in positions of pages shown with set_page_visible.
        self.carousel.scroll_to(scrolled_window, False)

    def _setup_bottom_bar(self) -> None:
        # --- Outer container for the buttons and indicator ---
        self.bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        self.bottom_bar.set_hexpand(True)
        self.bottom_bar.set_margin_start(24)
        self.bottom_bar.set_margin_end(24)
        self.bottom_bar.set_margin_top(12)
        self.bottom_bar.set_margin_bottom(12)
        # --- Back button section ---
        back_box = Gtk.Box()
        back_box.set_halign(Gtk.Align.START)
        self.back_button = Gtk.Button(label="Back")
        self.back_button.get_style_context().add_class("flat")
        self.back_button.connect("clicked", self.on_back_pressed)
        back_box.append(self.back_button)
        self.bottom_bar.append(back_box)
        # --- Carousel indicator ---
        indicator = Adw.CarouselIndicatorDots()
        indicator.set_carousel(self.carousel)
        self.bottom_bar.append(indicator)
        # --- Next button section ---
        next_box = Gtk.Box()
        next_box.set_halign(Gtk.Align.END)
        self.next_button = Gtk.Button(label="Next")
        self.next_button.get_style_context().add_class("suggested-action")
        self.next_button.connect("clicked", self.on_next_pressed)
        next_box.append(self.next_button)
        self.bottom_bar.append(next_box)

    def do_add_child(self, builder, child, type_):
        # Called ONLY for external <child> in other .ui files
        # Welcome page is added before other pages (properties of view are set then). Added when view is shown, it
        # would be placed before page carousel already shows (view pushed in main window), carousel keeps showing that
        # page then.
        self._setup_welcome_page()
        if type_ == "welcome":
            self.welcome_page = child
            self.carousel.prepend(child)
            self.pages.insert(0, child)
            self._all_pages.insert(0, child)
        else:
            self.carousel.append(child)
            self.pages.append(child)
            self._all_pages.append(child)
        self._refresh_buttons_state()

    def remove_page(self, page: Gtk.Widget):
        """Removes page that is not needed (eg. on given system)."""
        self.carousel.remove(page)
        self.pages.remove(page)
        if page in self._all_pages:
            self._all_pages.remove(page)
        self._refresh_buttons_state()

    def set_page_visible(self, page: Gtk.Widget, visible: bool):
        """Shows or hides page that is needed only for some selections on previous pages. Pages after current one
        should be changed only, hidden page keeps its position between other pages."""
        if visible == (page in self.pages) or page not in self._all_pages:
            return
        if visible:
            position = sum(1 for other in self._all_pages[:self._all_pages.index(page)] if other in self.pages)
            self.carousel.insert(page, position)
            self.pages.insert(position, page)
        else:
            self.carousel.remove(page)
            self.pages.remove(page)
        self._refresh_buttons_state()

    # --------------------------------------------------------------------------
    # Switching pages:

    def on_page_changed(self, carousel, pspec):
        self.current_page = int(carousel.get_position())
        self._refresh_buttons_state()

    def on_back_pressed(self, _):
        is_first_page = self.current_page == 0
        if not is_first_page:
            if self.back_interceptor and self.back_interceptor(self):
                return
            self.carousel.scroll_to(self.carousel.get_nth_page(self.current_page - 1), True)

    def on_next_pressed(self, _):
        is_last_page = self.current_page == len(self.pages) - 1
        if not is_last_page:
            self.carousel.scroll_to(self.carousel.get_nth_page(self.current_page + 1), True)
        else:
            self.emit("begin-installation")

    def go_back(self):
        """Previous page of setup (back of navigation)."""
        if self.can_go_back:
            self.on_back_pressed(None)

    def _update_can_go_back(self):
        # Wizard replaced by other view (eg. queued build) doesn't handle back of navigation anymore.
        can_go_back = self.get_parent() is not None and self.get_child() is self.setup_view and self.current_page > 0
        if self.can_go_back != can_go_back:
            self.can_go_back = can_go_back

    def on_start_row_activated(self, _):
        if len(self.pages) > 1:
            self.carousel.scroll_to(self.pages[1], True)

    def _refresh_buttons_state(self):
        """Should be called manually when state of displayed page changes in subclass."""
        # Subclasses may change a control while constructing their first page,
        # before adding that page to the carousel.
        if not self.pages or not 0 <= self.current_page < len(self.pages):
            return
        displayed_page = self.pages[self.current_page]
        displayed_pages = self.pages[0:self.current_page + 1]
        displayed_pages_ready = all(self.emit("is-page-ready-to-continue", page) for page in displayed_pages)
        is_welcome_page = displayed_page == self.welcome_page
        is_first_page = self.current_page == 0
        is_last_page = self.current_page == len(self.pages) - 1
        self.back_button.set_sensitive(not is_first_page)
        self.back_button.set_opacity(0.0 if is_first_page else 1.0)
        self.next_button.set_sensitive(displayed_pages_ready and not is_welcome_page)
        self.next_button.set_opacity(0.0 if is_welcome_page else 1.0)
        self.next_button.set_label(self.install_button_label if is_last_page else "Next")
        self._update_can_go_back()

    # --------------------------------------------------------------------------
    # Installation management:

    def _set_current_stage(self, stage: MultiStageProcessState):
        # Setup views visibility:
        if stage == MultiStageProcessState.SETUP:
            self.set_child(self.setup_view)
        else:
            self.set_child(self.install_view)
        self._update_can_go_back()

    def set_installation(self, installation: MultistageProcess | None):
        # Call this after installation was correctly started from this view using begin_installation overwrite.
        if self.install_view.multistage_process:
            return
        self.installation_in_progress = installation
        if installation:
            self.install_view.set_multistage_process(installation)
        self.refresh_installation_state()

    def refresh_installation_state(self):
        self._set_current_stage(self.installation_in_progress.status if self.installation_in_progress else MultiStageProcessState.SETUP)

