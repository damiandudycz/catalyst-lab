import re


def filter_package_categories(packages_by_category: dict, query: str, minimum: int = 2,
                              match_full_paths: bool = False) -> dict:
    query = query.strip().casefold()
    if len(query) < minimum:
        return packages_by_category
    filtered = {}
    for category, packages in packages_by_category.items():
        if match_full_paths and "/" in query:
            matches = {name: versions for name, versions in packages.items()
                       if query in f"{category}/{name}".casefold()}
        else:
            matches = (packages if query in category.casefold() else
                       {name: versions for name, versions in packages.items() if query in name.casefold()})
        if matches:
            filtered[category] = matches
    return filtered


def package_version_key(version: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", version)]
