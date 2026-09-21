import os
from pathlib import Path

def project_root() -> Path:
    if env_root := os.getenv("MYFLOW_PROJECT_ROOT"):
        root = Path(env_root).resolve()
        if not root.exists():
            raise ValueError(f"MYFLOW_PROJECT_ROOT={env_root} resolved to  {root} that does not exist")
        if not root.is_dir():
            raise ValueError(f"MYFLOW_PROJECT_ROOT={env_root} resolved to  {root} that is not a directory")
        return root
    return Path.cwd().resolve()

def runtime_home() -> Path:
    if env_home := os.getenv("MYFLOW_HOME"):
        home = Path(env_home).resolve()

    return project_root() / ".myflow"

def resolve_path(value: str|os.PathLike[str], *, base: Path|None = None) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = (base or project_root()) / path
    return path.resolve()

def existing_project_file(names: tuple[str, ...]) -> Path|None:
    root = project_root()

    for name in names:
        file = root / name
        if file.is_file():
            return file
    return None