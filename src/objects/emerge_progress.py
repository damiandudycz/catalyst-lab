import re


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PACKAGE = re.compile(r">>> (Emerging|Completed)(?: binary)? \((\d+) of (\d+)\)")
_JOBS = re.compile(r"Jobs: (\d+) of (\d+) complete")


class EmergeProgress:
    """Parse Gentoo emerge progress, optionally ignoring the seed update of a stage build."""

    def __init__(self, *, completed_only: bool = False, ignore_seed_update: bool = False):
        self.completed_only = completed_only
        self.ignore_seed_update = ignore_seed_update
        self.total = self.completed = 0
        self.seed_update = self.seed_update_started = False

    def parse(self, line: str) -> float | None:
        progress = None
        for part in _ANSI.sub("", line).split("\r"):
            part = part.strip()
            if self.ignore_seed_update and part.startswith("Updating seed stage"):
                self.seed_update, self.seed_update_started = True, False
                continue
            if self.seed_update and part.startswith("emerge "):
                if self.seed_update_started:
                    self.seed_update = False
                else:
                    self.seed_update_started = True
                continue
            if self.seed_update:
                continue
            if self.completed_only:
                match = _PACKAGE.match(part)
                if match and match.group(1) == "Completed" and int(match.group(3)) > 1:
                    progress = int(match.group(2)) / int(match.group(3))
                continue
            if match := _JOBS.search(part):
                completed, total = map(int, match.groups())
            elif match := _PACKAGE.search(part):
                completed, total = int(match.group(2)), int(match.group(3))
                if match.group(1) == "Emerging":
                    completed -= 1
            else:
                continue
            if total != self.total:
                self.total, self.completed = total, completed
                progress = completed / total
            elif completed > self.completed:
                self.completed = completed
                progress = completed / total
        return progress
