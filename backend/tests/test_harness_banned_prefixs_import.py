import ast
from pathlib import Path

HARNESS_ROOT = Path(__file__).parent.parent / "packages" / "harness"/ "agentkit"
BANNED_PREFIXES = ("app.",)

def _collect_import(filepath: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            results.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            results.append((node.lineno, node.module))
    return results

def test_banned_prefixes_are_not_used():
    violations = [
        f" {py.relative_to(HARNESS_ROOT.parent[2])}:{lineno}: import {module}"
        for py in sorted(HARNESS_ROOT.rglob("*.py"))
        for lineno, module in _collect_import(py)
        if any(module == prefix.rstrip(".") or module.startswith(prefix + ".") for prefix in BANNED_PREFIXES)
    ]
    assert not violations, "Harness must not import:\n" + "\n".join(violations)