from pathlib import Path
import tomllib

HARNESS_ROOT = Path(__file__).parent.parent / "packages" / "harness"


def test_providers_are_optional():
    pyproject = tomllib.loads((HARNESS_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = pyproject["project"]["dependencies"]
    optional = pyproject["dependency-groups"]["optional"]

    for provider in ("langchain-openai", "langchain-anthropic", "langchain-deepseek"):
        assert not any(d.startswith(provider) for d in core), (
            f"{provider} must stay optional so core installs are provider-agnostic"
        )
    assert "openai" in optional

