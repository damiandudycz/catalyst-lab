from __future__ import annotations
import socket, struct, threading, time
from gi.repository import Gtk, Gdk, GLib

# ------------------------------------------------------------------------------
# Screen of virtual machine: minimal VNC (RFB 3.8) client for display of QEMU on local socket. Framebuffer is updated
# with raw and copy rectangles, resized with machine, and shown scaled to fit view. Keys (GDK key values are X11
# keysyms used by VNC) and pointer (absolute, QEMU has USB tablet) are sent to machine.

_RAW, _COPY_RECT, _DESKTOP_SIZE = 0, 1, -223

class VncView(Gtk.Widget):
    __gtype_name__ = "CatalystLabVncView"

    def __init__(self):
        super().__init__(focusable=True, hexpand=True, vexpand=True)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, hexpand=True, vexpand=True)
        self.picture.set_parent(self)
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._frame = bytearray()
        self._width = self._height = 0
        self._dirty = False
        self._buttons = 0
        self._closed = False
        self.status_handler = None # Called with text of connection state.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda controller, keyval, keycode, state: self._key(keyval, True))
        keys.connect("key-released", lambda controller, keyval, keycode, state: self._key(keyval, False))
        self.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda controller, x, y: self._pointer(x, y))
        self.add_controller(motion)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        self._last_position = (0, 0)

    # Screen is scaled to space of view: it doesn't ask for size of machine screen (window would grow with it).
    def do_measure(self, orientation, for_size):
        minimum = 320 if orientation == Gtk.Orientation.HORIZONTAL else 200
        return minimum, minimum, -1, -1

    def do_size_allocate(self, width, height, baseline):
        if self.picture is not None:
            self.picture.allocate(width, height, baseline, None)

    def do_dispose(self):
        self.disconnect_vnc()
        if self.picture is not None:
            self.picture.unparent()
            self.picture = None

    # Connection

    def connect_vnc(self, path: str, alive=lambda: True):
        """Connects in background to VNC socket (created by QEMU when it starts)."""
        self._closed = False
        threading.Thread(target=self._run, args=(path, alive), daemon=True).start()

    def clear(self):
        """Removes last frame (machine stopped)."""
        if self.picture is not None:
            self.picture.set_paintable(None)

    def disconnect_vnc(self):
        self._closed = True
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def _status(self, text: str):
        if self.status_handler:
            GLib.idle_add(lambda: (self.status_handler(text), False)[1])

    def _run(self, path: str, alive):
        connection = None
        for _ in range(200):
            if self._closed or not alive():
                return
            try:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.connect(path)
                break
            except OSError:
                connection = None
                time.sleep(0.1)
        if connection is None:
            self._status("Display is not available")
            return
        self._socket = connection
        try:
            self._handshake(connection)
            self._status("")
            self._request_update(incremental=False)
            while not self._closed:
                self._read_message(connection)
        except (OSError, ConnectionError, struct.error) as e:
            if not self._closed:
                self._status(f"Display disconnected: {e}" if str(e) else "Display disconnected")

    def _receive(self, connection, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise ConnectionError("connection closed")
            data += chunk
        return bytes(data)

    def _handshake(self, connection):
        self._receive(connection, 12) # Version of server.
        connection.sendall(b"RFB 003.008\n")
        count = self._receive(connection, 1)[0]
        if count == 0:
            length = struct.unpack(">I", self._receive(connection, 4))[0]
            raise ConnectionError(self._receive(connection, length).decode(errors="replace"))
        types = self._receive(connection, count)
        if 1 not in types:
            raise ConnectionError("display requires authentication")
        connection.sendall(b"\x01")
        if struct.unpack(">I", self._receive(connection, 4))[0] != 0:
            raise ConnectionError("display refused connection")
        connection.sendall(b"\x01") # Shared.
        width, height = struct.unpack(">HH", self._receive(connection, 4))
        self._receive(connection, 16) # Pixel format of server.
        name_length = struct.unpack(">I", self._receive(connection, 4))[0]
        self._receive(connection, name_length)
        self._resize(width, height)
        # 32 bits per pixel, blue green red order in memory (little endian), padding byte.
        pixel_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self._send(b"\x00\x00\x00\x00" + pixel_format)
        encodings = [_RAW, _COPY_RECT, _DESKTOP_SIZE]
        self._send(struct.pack(">BxH", 2, len(encodings)) + b"".join(struct.pack(">i", encoding) for encoding in encodings))

    def _send(self, data: bytes):
        connection = self._socket
        if connection is None:
            return
        with self._send_lock:
            try:
                connection.sendall(data)
            except OSError:
                pass

    def _request_update(self, incremental: bool = True):
        self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self._width, self._height))

    def _resize(self, width: int, height: int):
        with self._frame_lock:
            self._width, self._height = width, height
            self._frame = bytearray(width * height * 4)

    def _read_message(self, connection):
        kind = self._receive(connection, 1)[0]
        if kind == 0: # Framebuffer update.
            count = struct.unpack(">xH", self._receive(connection, 3))[0]
            for _ in range(count):
                x, y, width, height, encoding = struct.unpack(">HHHHi", self._receive(connection, 12))
                if encoding == _RAW:
                    data = self._receive(connection, width * height * 4)
                    with self._frame_lock:
                        stride = self._width * 4
                        for row in range(height):
                            start = (y + row) * stride + x * 4
                            self._frame[start:start + width * 4] = data[row * width * 4:(row + 1) * width * 4]
                elif encoding == _COPY_RECT:
                    source_x, source_y = struct.unpack(">HH", self._receive(connection, 4))
                    with self._frame_lock:
                        stride = self._width * 4
                        rows = [bytes(self._frame[(source_y + row) * stride + source_x * 4:(source_y + row) * stride + (source_x + width) * 4])
                                for row in range(height)]
                        for row, data in enumerate(rows):
                            start = (y + row) * stride + x * 4
                            self._frame[start:start + width * 4] = data
                elif encoding == _DESKTOP_SIZE:
                    self._resize(width, height)
                else:
                    raise ConnectionError(f"unsupported encoding {encoding}")
            self._schedule_redraw()
            self._request_update()
        elif kind == 1: # Color map entries (not used with true color).
            _, count = struct.unpack(">xHH", self._receive(connection, 5))
            self._receive(connection, count * 6)
        elif kind == 2: # Bell.
            pass
        elif kind == 3: # Clipboard of machine.
            length = struct.unpack(">3xI", self._receive(connection, 7))[0]
            self._receive(connection, length)
        else:
            raise ConnectionError(f"unknown message {kind}")

    def _schedule_redraw(self):
        if not self._dirty:
            self._dirty = True
            GLib.timeout_add(16, self._redraw) # Up to 60 frames per second.

    def _redraw(self) -> bool:
        self._dirty = False
        if self.picture is None:
            return False
        with self._frame_lock:
            if not self._width or not self._height:
                return False
            data = GLib.Bytes.new(bytes(self._frame))
            width, height = self._width, self._height
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.B8G8R8X8, data, width * 4)
        self.picture.set_paintable(texture)
        return False

    # Input

    def _key(self, keyval: int, down: bool) -> bool:
        if self._socket is None:
            return False
        self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keyval))
        return True

    def _machine_position(self, x: float, y: float) -> tuple[int, int] | None:
        """Position on screen of machine (picture is scaled to fit and centered)."""
        if not self._width or not self._height:
            return None
        view_width, view_height = self.get_width(), self.get_height()
        scale = min(view_width / self._width, view_height / self._height)
        if scale <= 0:
            return None
        left = (view_width - self._width * scale) / 2
        top = (view_height - self._height * scale) / 2
        machine_x = int((x - left) / scale)
        machine_y = int((y - top) / scale)
        return max(0, min(self._width - 1, machine_x)), max(0, min(self._height - 1, machine_y))

    def _pointer(self, x: float, y: float):
        self._last_position = (x, y)
        if (position := self._machine_position(x, y)) is not None:
            self._send(struct.pack(">BBHH", 5, self._buttons, *position))

    def _button_mask(self, button: int) -> int:
        return {1: 1, 2: 2, 3: 4}.get(button, 0)

    def _on_pressed(self, gesture, count, x, y):
        self.grab_focus()
        self._buttons |= self._button_mask(gesture.get_current_button())
        self._pointer(x, y)

    def _on_released(self, gesture, count, x, y):
        self._buttons &= ~self._button_mask(gesture.get_current_button())
        self._pointer(x, y)

    def _on_scroll(self, controller, dx, dy) -> bool:
        mask = 8 if dy < 0 else 16 # Wheel up and down are buttons 4 and 5.
        if (position := self._machine_position(*self._last_position)) is not None:
            self._send(struct.pack(">BBHH", 5, self._buttons | mask, *position))
            self._send(struct.pack(">BBHH", 5, self._buttons, *position))
        return True
