
STARTUP_ONLY_PREFIX = "startup-only:"

STARTUP_ONLY_FIELDS: dict[str,str] = {
    "log_level": (
        "apply_logging_level() 只在启动时运行；重载后的AppConfig不会重新触发它"
    ),
}

def format_field_description(field_path: str, *, field_doc: str|None=None) -> str:
    reason = STARTUP_ONLY_FIELDS[field_path]
    header = f"{STARTUP_ONLY_PREFIX} {reason}"
    return header if field_doc is None else f"{header}\n{field_doc.strip()}"