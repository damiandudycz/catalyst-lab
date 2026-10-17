"""Normalize streamed serial output for a text console without a VT emulator."""


class SerialConsoleFilter:
    """Preserve SGR colors, discard cursor controls, and normalize CR updates."""

    def __init__(self):
        self._state = "text"
        self._escape = ""
        self._pending_cr = False

    def feed(self, text: str, final: bool = False) -> str:
        output = []

        def flush_cr():
            if self._pending_cr:
                output.append("\n")
                self._pending_cr = False

        for character in text:
            if self._state == "text":
                if character == "\x1b":
                    flush_cr()
                    self._state = "escape"
                    self._escape = character
                elif character == "\r":
                    flush_cr()
                    self._pending_cr = True
                else:
                    if self._pending_cr:
                        self._pending_cr = False
                        output.append("\n")
                        if character == "\n":
                            continue
                    if character == "\n" or character == "\t" or ord(character) >= 0x20 and ord(character) != 0x7f:
                        output.append(character)
            elif self._state == "escape":
                self._escape += character
                if character == "[":
                    self._state = "csi"
                elif character == "]":
                    self._state = "osc"
                elif character in "()*+-./":
                    self._state = "charset"
                else:
                    self._state = "text"
                    self._escape = ""
            elif self._state == "charset":
                self._state = "text"
                self._escape = ""
            elif self._state == "csi":
                self._escape += character
                code = ord(character)
                if 0x40 <= code <= 0x7e:
                    if character == "m":
                        output.append(self._escape)
                    self._state = "text"
                    self._escape = ""
            elif self._state == "osc":
                if character == "\x07":
                    self._state = "text"
                elif character == "\x1b":
                    self._state = "osc-escape"
            elif self._state == "osc-escape":
                self._state = "text" if character == "\\" else "osc"

        if final:
            flush_cr()
            self._state = "text"
            self._escape = ""
        return "".join(output)
