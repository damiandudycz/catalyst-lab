"""Consistent formatting for JSON files written by the application."""

import json
from typing import Any, TextIO


def dump_json(file: TextIO, value: Any) -> None:
    """Write readable UTF-8 JSON using the application's standard file format."""
    json.dump(value, file, indent=4)
    file.write("\n")
