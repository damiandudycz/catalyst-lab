def unique_name(name: str, is_available) -> str:
    """Return name, adding " - 2", " - 3", etc. until the supplied check accepts it."""
    base = name.strip()
    if not base or is_available(base):
        return base
    number = 2
    candidate = f"{base} - {number}"
    while not is_available(candidate):
        number += 1
        candidate = f"{base} - {number}"
    return candidate
