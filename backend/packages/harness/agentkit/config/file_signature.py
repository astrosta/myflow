import hashlib
from pathlib import Path

#(mtime, size, sha256) 三元组
ConfigSignature = tuple[float | None, int | None, str | None]    

def get_config_signature(config_path: Path) -> ConfigSignature:
    try:
        st = config_path.stat()
    except OSError:
        return None

    digest = hashlib.sha256()
    try:
        with config_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return (st.st_mtime, st.st_size, None)
    return (st.st_mtime, st.st_size, digest.hexdigest())