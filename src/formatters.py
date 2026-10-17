def format_size(size: int, max_unit: str = "TB", units: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")) -> str:
    for index, unit in enumerate(units):
        if size < 1024 or unit == max_unit:
            return f"{size:.0f} {unit}" if index == 0 else f"{size:.1f} {unit}"
        size /= 1024
