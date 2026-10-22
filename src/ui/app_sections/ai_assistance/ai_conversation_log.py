"""Private plain-text conversation logs for explicitly enabled debug runs."""
from datetime import datetime
import os
from pathlib import Path
import uuid


DEBUG_FLAG = "CATALYSTLAB_DEBUG_CONVERSATION_LOGS"
LOG_DIRECTORY = Path("/tmp/catalystlab-debug-conversations")


def save_conversation(model: str, raw_text: str, events=None) -> Path | None:
    """Save a readable event trace plus the untouched raw stream in debug runs."""
    if os.environ.get(DEBUG_FLAG) != "1":
        return None

    directory = LOG_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError(f"Unsafe debug conversation directory: {directory}")
    directory.chmod(0o700)

    created = datetime.now().astimezone()
    stamp = created.strftime("%Y%m%d-%H%M%S-%f%z")
    path = directory / f"conversation-{stamp}-{uuid.uuid4().hex[:8]}.txt"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(f"Date: {created.isoformat(timespec='seconds')}\n")
        output.write(f"Model: {model}\n\n")
        output.write("=== EVENT TRACE ===\n")
        for index, event in enumerate(events or (), 1):
            output.write(f"\n--- {index:03d} · {event.get('kind', 'EVENT')} ")
            if event.get("channel"):
                output.write(f"· {event['channel']} ")
            output.write(f"· {event.get('time', '')} ---\n")
            details = event.get("details")
            if details:
                output.write(str(details).rstrip() + "\n")
            text = event.get("text")
            if text:
                output.write(str(text))
                if not str(text).endswith("\n"):
                    output.write("\n")
        output.write("\n=== RAW OUTPUT (verbatim) ===\n")
        output.write(raw_text)
        if raw_text and not raw_text.endswith("\n"):
            output.write("\n")
    return path
