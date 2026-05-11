import hashlib
from pathlib import Path


def yaml_id(yaml_path: Path) -> str:
    resolved = yaml_path.resolve()
    digest = hashlib.md5(str(resolved).encode()).hexdigest()[:12]
    return digest
