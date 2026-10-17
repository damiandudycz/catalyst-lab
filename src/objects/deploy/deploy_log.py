def write_log_line(path, file, line: str):
    if not path:
        return file
    try:
        if file is None:
            file = open(path, "a", encoding="utf-8", buffering=1)
        file.write(line + "\n")
    except OSError as error:
        print(f"Failed to write deploy log: {error}")
    return file
