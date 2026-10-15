from gi.repository import Gtk, Adw

USAGE_HISTORY_SECONDS = 60
USAGE_INTERVAL_SECONDS = 1


class UsageGraph(Gtk.DrawingArea):
    """Usage (0-1) during last minute, newest on the right. Drawn with accent color, orange and red when usage is
    high (like level bars)."""

    def __init__(self):
        super().__init__(content_width=200, content_height=36, valign=Gtk.Align.CENTER)
        self.values: list[float] = []
        self.set_draw_func(self._draw)

    def add(self, value: float):
        self.values = (self.values + [value])[-(USAGE_HISTORY_SECONDS // USAGE_INTERVAL_SECONDS + 1):]
        self.queue_draw()

    def clear(self):
        self.values = []
        self.queue_draw()

    def _color(self) -> tuple[float, float, float]:
        latest = self.values[-1] if self.values else 0
        if latest >= 0.9:
            return (0xe0 / 255, 0x1b / 255, 0x24 / 255)
        if latest >= 0.75:
            return (0xff / 255, 0x78 / 255, 0x00 / 255)
        try:
            accent = Adw.StyleManager.get_default().get_accent_color_rgba()
            return (accent.red, accent.green, accent.blue)
        except AttributeError: # Older libadwaita.
            return (0x35 / 255, 0x84 / 255, 0xe4 / 255)

    @staticmethod
    def _rounded_rectangle(context, x: float, y: float, width: float, height: float, radius: float):
        import math
        context.new_sub_path()
        context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
        context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
        context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
        context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
        context.close_path()

    def _draw(self, area, context, width, height):
        foreground = self.get_color()
        self._rounded_rectangle(context, 0.5, 0.5, width - 1, height - 1, 6)
        context.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.08)
        context.fill_preserve()
        context.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.15)
        context.set_line_width(1)
        context.stroke_preserve()
        context.clip()
        if not self.values:
            return
        samples = USAGE_HISTORY_SECONDS // USAGE_INTERVAL_SECONDS
        # Keep every sample inside the clipped drawing area; placing the newest at
        # exactly `width` clipped the live edge of the graph, especially when it
        # had only just started collecting samples.
        inset = 1.5
        step = max(0, (width - inset * 2) / samples)
        points = [(width - inset - (len(self.values) - 1 - index) * step,
                   height - value * (height - 2) - 1)
                  for index, value in enumerate(self.values)]
        red, green, blue = self._color()
        if len(points) == 1:
            context.arc(points[0][0], points[0][1], 2, 0, 6.283185307179586)
            context.set_source_rgba(red, green, blue, 1)
            context.fill()
            return
        context.move_to(points[0][0], height)
        for x, y in points:
            context.line_to(x, y)
        context.line_to(points[-1][0], height)
        context.close_path()
        context.set_source_rgba(red, green, blue, 0.25)
        context.fill()
        context.move_to(*points[0])
        for x, y in points[1:]:
            context.line_to(x, y)
        context.set_source_rgba(red, green, blue, 1)
        context.set_line_width(1.5)
        context.stroke()
