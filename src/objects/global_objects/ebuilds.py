import re
from collections import defaultdict


def group_ebuild_paths(paths):
    """Group relative category/package/package-version.ebuild paths by package."""
    packages = defaultdict(lambda: defaultdict(list))
    for path in paths:
        parts = path.split("/")
        if len(parts) != 3 or not parts[2].endswith(".ebuild"):
            continue
        category, package, filename = parts
        match = re.fullmatch(rf"{re.escape(package)}-(.+)\.ebuild", filename)
        if match:
            packages[category][package].append(match.group(1))

    version_key = lambda version: (tuple(int(value) for value in re.findall(r"\d+", version)), version)
    return {
        category: {
            package: sorted(versions, key=version_key)
            for package, versions in sorted(items.items())
        }
        for category, items in sorted(packages.items())
    }
