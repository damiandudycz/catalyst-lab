import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GObject, Adw
import cairo
from .project_stage import stage_target_icon, stage_target_short_name
from .tree_node import TreeNode
from dataclasses import dataclass

# Constants for spacing
NODE_MARGIN_X = 16 # Spacing between compact nodes of a branch
EXPANDED_NODE_MARGIN_X = 32 # Spacing between nodes of a branch when all are expanded
SIDE_MARGIN = 32 # Space on left and right, nodes expanded while hovered are moved to fit in view
NODE_MARGIN_Y = 32 # Spacing WITHIN a branch
ROOT_BRANCH_MARGIN_Y = 48 # Larger spacing BETWEEN root branches
CONNECTOR_CORNER_RADIUS = 10 # Rounded corners of lines connecting parents with children

@dataclass(frozen=True)
class StageNodeStatus:
    """Status shown on stage entry, for example state of stage in running build."""
    title: str
    icon_name: str | None = None
    css_class: str | None = None
    in_progress: bool = False # Shows spinner instead of icon.

NODE_EXPAND_DURATION = 200 # Milliseconds

def _stage_card_text(stage, status: StageNodeStatus | None, expanded: bool) -> tuple[str, str]:
    target = getattr(stage, "target", None) or ""
    broken = getattr(stage, "is_broken", False)
    subtitle = getattr(stage, "warning_message", None) if broken else target.replace("_", "-")
    if status:
        subtitle = f"{subtitle} · {status.title}" if subtitle else status.title
    title = stage.name if expanded or broken else stage_target_short_name(target)
    return title, subtitle or ""

def _create_stage_card(stage, status: StageNodeStatus | None, expanded: bool):
    """Stage displayed as list entry (boxed list with single row). Compact card has icon and short name of target,
    expanded one also name, target and status. Also used for measuring nodes, so sizes match."""
    target = getattr(stage, "target", None) or ""
    broken = getattr(stage, "is_broken", False)
    warning = getattr(stage, "warning_message", None)
    title, subtitle = _stage_card_text(stage, status, expanded)
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=not broken)
    row.add_css_class("stage-tree-card-row")
    row.set_use_markup(False)
    # Single line labels, ellipsized while card grows. Wrapping labels would report minimal width and get squeezed.
    row.set_title_lines(1)
    row.set_subtitle_lines(1)
    prefix_icon = "danger-triangle-svgrepo-com-symbolic" if broken else stage_target_icon(target)
    prefix = Gtk.Image.new_from_icon_name(prefix_icon)
    row.add_prefix(prefix)
    if broken:
        row.add_css_class("warning")
        row.set_tooltip_text(warning or "This stage is unavailable")
        prefix.add_css_class("warning")
    status_box = Gtk.Box()
    spinner = Adw.Spinner()
    spinner.set_size_request(16, 16)
    icon = Gtk.Image()
    status_box.append(spinner)
    status_box.append(icon)
    row.add_suffix(status_box)
    _update_stage_card_status(status, spinner, icon)
    list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    list_box.add_css_class("boxed-list")
    list_box.append(row)
    return list_box, row, spinner, icon

def _update_stage_card_status(status: StageNodeStatus | None, spinner: Adw.Spinner, icon: Gtk.Image):
    spinner.set_visible(bool(status and status.in_progress))
    icon.set_visible(bool(status and not status.in_progress and status.icon_name))
    spinner.set_tooltip_text(status.title if status and status.in_progress else None)
    icon.set_from_icon_name(status.icon_name if status and status.icon_name else None)
    icon.set_tooltip_text(status.title if status and status.icon_name else None)
    if status and status.css_class:
        icon.add_css_class(status.css_class)
    for css_class in ("success", "error", "warning", "dimmed"):
        if not status or status.css_class != css_class:
            icon.remove_css_class(css_class)

def _lerp(start: float, end: float, progress: float) -> float:
    return start + (end - start) * progress

class StagesTreeNode(Gtk.Widget):
    """Stage in tree. Compact card is expanded with animation while hovered. It's drawn over other nodes and lines,
    without changing their layout."""
    __gtype_name__ = "StagesTreeNode"

    def __init__(self, tree: "StagesTreeView", node: TreeNode):
        super().__init__()
        self.tree = tree
        self.node = node
        # An expanded node covers overlapping cards and connector lines below it.
        self.add_css_class("stages-tree-node")
        self.card: Gtk.ListBox | None = None
        self.card_row: Adw.ActionRow | None = None
        self.status_spinner: Adw.Spinner | None = None
        self.status_icon: Gtk.Image | None = None
        self.expanded_content = False
        self.status: StageNodeStatus | None = None
        self._content_key = None # Displayed name, target and status; card text updates in place when they change.
        self.progress = 0.0
        self.always_expanded = False # All nodes of tree shown in full, without animations.
        self.compact_rect = self.expanded_rect = (0, 0, 0, 0) # x, y, width, height in tree.
        self._size = (0, 0)
        self.animation = Adw.TimedAnimation.new(self, 0, 1, NODE_EXPAND_DURATION, Adw.CallbackAnimationTarget.new(self._on_animation_value))
        self.animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self.animation.connect("done", self._on_animation_done)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_pointer_enter)
        motion.connect("leave", self._on_pointer_leave)
        self.add_controller(motion)

    def _on_pointer_enter(self, *args):
        if not self.always_expanded:
            self._animate_to(1.0)

    def _on_pointer_leave(self, *args):
        self._animate_to(0.0)

    def set_always_expanded(self, always_expanded: bool):
        if always_expanded == self.always_expanded:
            return
        self.always_expanded = always_expanded
        if self.animation.get_state() == Adw.AnimationState.PLAYING:
            self.animation.skip()
        self.progress = 1.0 if always_expanded else 0.0
        if self.card is not None and self.expanded_content != always_expanded:
            self._set_card(expanded=always_expanded)

    @property
    def layout_size(self) -> tuple[int, int]:
        """Size of node used for layout of tree."""
        return self.expanded_size if self.always_expanded else self.compact_size

    def set_status(self, status: StageNodeStatus | None):
        """Sets status and measures compact and expanded sizes of card, returning size used for layout."""
        content_key = (self.node.value.name, getattr(self.node.value, "target", None),
                       getattr(self.node.value, "is_broken", False),
                       getattr(self.node.value, "warning_message", None), status)
        if content_key == self._content_key:
            return self.layout_size
        self._content_key = content_key
        self.status = status
        if self.card is not None:
            self._update_card(expanded=self.progress > 0)
        else:
            # Create the compact card on first layout. Deferring creation until hover leaves only
            # connectors visible on the initial draw.
            self._set_card(expanded=self.always_expanded)
        compact = _create_stage_card(self.node.value, status, expanded=False)[0].get_preferred_size()
        expanded = _create_stage_card(self.node.value, status, expanded=True)[0].get_preferred_size()
        # Expanded content is shown while card grows, so compact card is not smaller than its minimum size.
        self.compact_size = (max(compact[1].width, expanded[0].width), max(compact[1].height, expanded[0].height))
        self.expanded_size = (max(expanded[1].width, self.compact_size[0]), max(expanded[1].height, self.compact_size[1]))
        return self.layout_size

    def set_geometry(self, compact_rect: tuple, bounds: tuple[float, float]):
        """Position of compact card. Expanded card has the same center, moved to fit in bounds (width, height)."""
        self.compact_rect = compact_rect
        if self.always_expanded:
            self.expanded_rect = compact_rect # Laid out with expanded size.
            self._apply_progress()
            return
        x, y, width, height = compact_rect
        expanded_width, expanded_height = self.expanded_size
        expanded_x = min(max(x + width / 2 - expanded_width / 2, SIDE_MARGIN), bounds[0] - SIDE_MARGIN - expanded_width)
        expanded_y = min(max(y + height / 2 - expanded_height / 2, 0), bounds[1] - expanded_height)
        self.expanded_rect = (max(expanded_x, 0), max(expanded_y, 0), expanded_width, expanded_height)
        self._apply_progress()

    def _set_card(self, expanded: bool):
        self.expanded_content = expanded
        if self.card is None:
            self.card, self.card_row, self.status_spinner, self.status_icon = _create_stage_card(
                self.node.value, self.status, expanded)
            if not getattr(self.node.value, "is_broken", False):
                self.card.connect("row-activated", lambda *args: self.tree.emit("stage-selected", self.node.value))
            self.card.set_parent(self)
        else:
            self._update_card(expanded)

    def _update_card(self, expanded: bool):
        title, subtitle = _stage_card_text(self.node.value, self.status, expanded)
        self.card_row.set_title(title)
        self.card_row.set_subtitle(subtitle)
        self.card_row.set_activatable(not getattr(self.node.value, "is_broken", False))
        _update_stage_card_status(self.status, self.status_spinner, self.status_icon)
        self.expanded_content = expanded

    def _animate_to(self, value: float):
        if self.always_expanded or self.get_parent() is not self.tree:
            return
        if value > 0:
            # Drawn over other nodes.
            self.insert_before(self.get_parent(), None)
            if not self.expanded_content:
                self._set_card(expanded=True)
        self.animation.set_value_from(self.progress)
        self.animation.set_value_to(value)
        self.animation.play()

    def _on_animation_value(self, value: float):
        self.progress = value
        self._apply_progress()

    def _on_animation_done(self, animation):
        if self.progress == 0 and self.expanded_content and not self.always_expanded:
            self._set_card(expanded=False)

    def _apply_progress(self):
        x, y, width, height = (_lerp(start, end, self.progress) for start, end in zip(self.compact_rect, self.expanded_rect))
        self._size = (round(width), round(height))
        if self.get_parent() is self.tree:
            self.tree.move(self, round(x), round(y))
        self.queue_resize()

    def do_measure(self, orientation, for_size):
        size = self._size[0 if orientation == Gtk.Orientation.HORIZONTAL else 1]
        minimum = self.card.measure(orientation, -1)[0] if self.card else 0
        size = max(size, minimum)
        return size, size, -1, -1

    def do_size_allocate(self, width, height, baseline):
        if self.card:
            self.card.allocate(width, height, baseline, None)

    def do_dispose(self):
        if self.card is not None:
            self.card.unparent()
            self.card = None
        Gtk.Widget.do_dispose(self)

class StagesTreeView(Gtk.Fixed):
    __gtype_name__ = 'StagesTreeView'

    __gsignals__ = {
        "stage-selected": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,))
    }

    def __init__(self, centered=False):
        super().__init__()
        # 1. NEW: A property to control the layout style
        self.centered = centered

        self.root_nodes = []
        self.node_positions = {}
        self.node_sizes = {}
        self.buttons = {}
        self.separators = []
        self.statuses: dict = {} # Stage id -> StageNodeStatus.
        self.expand_all = StagesTreeView.expand_all_default

        # Space for shadows of cards in top and bottom rows.
        self.set_margin_top(12)
        self.set_margin_bottom(12)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self.draw_func)
        self.put(self.drawing_area, 0, 0)

    def set_root_nodes(self, root_nodes: list[TreeNode]):
        self.root_nodes = root_nodes
        self._layout_tree()
        self.drawing_area.queue_draw()

    expand_all_default = False # Last choice, used by new trees while app runs.

    def set_expand_all(self, expand_all: bool):
        """Shows all nodes in full instead of compact nodes expanded while hovered."""
        StagesTreeView.expand_all_default = expand_all
        if expand_all == self.expand_all:
            return
        self.expand_all = expand_all
        for widget in self.buttons.values():
            widget.set_always_expanded(expand_all)
        self._layout_tree()
        self.drawing_area.queue_draw()

    def set_statuses(self, statuses: dict):
        """Statuses of stages by their id, displayed on stage entries."""
        if statuses == self.statuses:
            return
        self.statuses = statuses
        self._layout_tree()
        self.drawing_area.queue_draw()

    def draw_func(self, area, context, width, height):
        # This drawing logic is robust enough to handle both layout styles
        context.set_line_width(2)
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        context.set_line_join(cairo.LINE_JOIN_ROUND)
        context.set_source_rgb(0.5, 0.5, 0.5)

        for parent, (px, py) in self.node_positions.items():
            parent_w, parent_h = self.node_sizes[parent]
            for child in parent.children:
                if child in self.node_positions and child in self.node_sizes:
                    child_x, child_y = self.node_positions[child]
                    child_w, child_h = self.node_sizes[child]

                    parent_visible_start_x = px
                    parent_visible_end_x = px + parent_w
                    child_cx = child_x + child_w / 2

                    if child_cx >= parent_visible_start_x and child_cx <= parent_visible_end_x:
                        parent_connector_y = py + parent_h
                        child_connector_y = child_y
                        context.move_to(child_cx, parent_connector_y)
                        context.line_to(child_cx, child_connector_y)
                    else:
                        parent_cx = px + parent_w / 2
                        parent_connector_y = py + parent_h / 2
                        if child_cx > parent_cx:
                            parent_connector_x = parent_visible_end_x
                        else:
                            parent_connector_x = parent_visible_start_x
                        child_connector_y = child_y
                        # Horizontal line from parent, turning down to child with rounded corner.
                        direction = 1 if child_cx > parent_connector_x else -1
                        radius = min(CONNECTOR_CORNER_RADIUS, abs(child_cx - parent_connector_x), abs(child_connector_y - parent_connector_y))
                        context.move_to(parent_connector_x, parent_connector_y)
                        context.line_to(child_cx - direction * radius, parent_connector_y)
                        context.curve_to(child_cx, parent_connector_y, child_cx, parent_connector_y, child_cx, parent_connector_y + radius)
                        context.line_to(child_cx, child_connector_y)
                    context.stroke()

    def _get_all_nodes(self):
        all_nodes = []
        nodes_to_visit = list(self.root_nodes)
        visited = set()
        while nodes_to_visit:
            node = nodes_to_visit.pop(0)
            if node in visited: continue
            visited.add(node)
            all_nodes.append(node)
            nodes_to_visit.extend(node.children)
        return all_nodes

    def _measure_nodes(self):
        """Creates node widgets for new stages (existing ones are reused, so hovered node stays expanded when statuses
        change) and measures their compact sizes."""
        nodes = self._get_all_nodes()
        widgets = {}
        for node in nodes:
            stage_id = getattr(node.value, "id", None)
            widget = self.buttons.pop(stage_id, None)
            if widget is None or widget.node.value is not node.value:
                if widget is not None:
                    self.remove(widget)
                widget = StagesTreeNode(self, node)
                widget.set_always_expanded(self.expand_all)
                self.put(widget, 0, 0)
            widget.node = node
            widgets[stage_id] = widget
        for widget in self.buttons.values():
            self.remove(widget)
        self.buttons = widgets
        self.node_sizes = {node: self.buttons[getattr(node.value, "id", None)].set_status(self.statuses.get(getattr(node.value, "id", None)))
                           for node in nodes}

    def _layout_tree(self):
        # --- COMMON SETUP ---
        if not self.root_nodes:
            for button in self.buttons.values(): self.remove(button)
            for sep in self.separators: self.remove(sep)
            self.buttons.clear()
            self.separators.clear()
            self.drawing_area.queue_draw()
            return

        self._measure_nodes()
        self.node_positions.clear()
        for sep in self.separators: self.remove(sep)
        self.separators.clear()

        node_margin_x = EXPANDED_NODE_MARGIN_X if self.expand_all else NODE_MARGIN_X
        all_positions = {}
        current_y = 0
        total_max_x = 0
        separator_y_positions = []

        # 2. CONDITIONAL LAYOUT LOGIC
        if self.centered:
            # --- Centered Layout Logic ---
            def layout_node_centered(node, x, y, branch_positions):
                node_width, node_height = self.node_sizes[node]
                if not node.children:
                    branch_positions[node] = (x, y)
                    return x, x + node_width
                child_y = y + node_height + NODE_MARGIN_Y
                children_ranges, current_x = [], x
                for child in node.children:
                    min_x, max_x = layout_node_centered(child, current_x, child_y, branch_positions)
                    children_ranges.append((min_x, max_x))
                    current_x = max_x + node_margin_x
                children_min_x, children_max_x = children_ranges[0][0], children_ranges[-1][1]
                children_center_x = (children_min_x + children_max_x) / 2
                node_x = children_center_x - node_width / 2
                branch_positions[node] = (node_x, y)
                return min(node_x, children_min_x), max(node_x + node_width, children_max_x)

            for i, root in enumerate(self.root_nodes):
                branch_positions, max_y_in_branch = {}, 0
                min_x, _ = layout_node_centered(root, 0, 0, branch_positions)
                horizontal_shift = -min_x + SIDE_MARGIN
                for node, (x, y) in branch_positions.items():
                    final_x, final_y = x + horizontal_shift, y + current_y
                    all_positions[node] = (final_x, final_y)
                    node_w, node_h = self.node_sizes[node]
                    total_max_x = max(total_max_x, final_x + node_w)
                    max_y_in_branch = max(max_y_in_branch, final_y + node_h)
                if i < len(self.root_nodes) - 1:
                    separator_y_positions.append(max_y_in_branch + ROOT_BRANCH_MARGIN_Y / 2)
                current_y = max_y_in_branch + ROOT_BRANCH_MARGIN_Y

        else:
            # --- Left-Aligned Layout Logic ---
            def layout_node_left(node, x, y):
                node_width, node_height = self.node_sizes[node]
                all_positions[node] = (x, y)
                if not node.children: return (x, x + node_width, y + node_height)
                child_y = y + node_height + NODE_MARGIN_Y
                current_child_x, child_max_extents, child_max_y_positions = x, [], []
                for child in node.children:
                    _min, max_x_child, max_y_child = layout_node_left(child, current_child_x, child_y)
                    child_max_extents.append(max_x_child)
                    child_max_y_positions.append(max_y_child)
                    current_child_x = max_x_child + node_margin_x
                subtree_max_x = max(x + node_width, max(child_max_extents)) if child_max_extents else x + node_width
                subtree_max_y = max(y + node_height, max(child_max_y_positions)) if child_max_y_positions else y + node_height
                return (x, subtree_max_x, subtree_max_y)

            for i, root in enumerate(self.root_nodes):
                _min_x, max_x, max_y = layout_node_left(root, SIDE_MARGIN, current_y)
                total_max_x = max(total_max_x, max_x)
                if i < len(self.root_nodes) - 1:
                    separator_y_positions.append(max_y + ROOT_BRANCH_MARGIN_Y / 2)
                current_y = max_y + ROOT_BRANCH_MARGIN_Y

        # --- COMMON TEARDOWN ---
        self.node_positions = all_positions
        # Wide enough for every expanded node.
        widest_expanded = max(widget.expanded_size[0] for widget in self.buttons.values())
        total_width = max(total_max_x + SIDE_MARGIN, widest_expanded + SIDE_MARGIN * 2)
        total_height = current_y
        self.set_size_request(total_width, total_height)
        self.drawing_area.set_size_request(total_width, total_height)

        for node, (x, y) in self.node_positions.items():
            self.buttons[getattr(node.value, "id", None)].set_geometry((x, y, *self.node_sizes[node]), (total_width, total_height))

        for y_pos in separator_y_positions:
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            separator.set_size_request(total_width - SIDE_MARGIN * 2, -1)
            self.put(separator, SIDE_MARGIN, y_pos)
            # Below nodes, expanded nodes are drawn over it.
            separator.insert_after(self, self.drawing_area)
            self.separators.append(separator)


