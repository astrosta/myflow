"""Task 14: private skill trees are mounted per user, never process-wide."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage

from agentkit.agents.middlewares.durable_context_middleware import DurableContextMiddleware
from agentkit.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
from agentkit.agents.middlewares.skill_tool_policy_middleware import SkillToolPolicyMiddleware
from agentkit.config.app_config import AppConfig
from agentkit.config.skills_config import SkillGitConfig, SkillsConfig
from agentkit.runtime.secret_context import SLASH_SKILL_SOURCE_CONTEXT_KEY, read_secret_context
from agentkit.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from agentkit.sandbox.security import find_path_mapping, resolve_sandbox_path
from agentkit.skills.catalog import SkillSnapshot, get_skill_snapshot, set_skill_snapshot
from agentkit.skills.domain import SkillScope
from agentkit.skills.types import Skill, SkillCategory
from fakes.skill_tree import skills_config

PUBLIC_MARKER = "PUBLIC-BODY-14"
PRIVATE_MARKER = "PRIVATE-BODY-14"


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _write_skill_file(
    root: Path,
    relative: str,
    body: str,
    *,
    name: str,
    allowed: str = "read_file",
) -> Path:
    path = root / relative / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {name}\ndescription: {name} skill\n"
        f"allowed-tools: [{allowed}]\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _skill(
    *,
    name: str,
    path: Path,
    scope: SkillScope,
    owner: str | None = None,
    skill_id: str,
) -> Skill:
    return Skill(
        name=name,
        description=f"{name} skill",
        license=None,
        skill_dir=path.parent,
        skill_file=path,
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=True,
        allowed_tools=("read_file",),
        scope=scope,
        owner_user_id=owner,
        skill_id=skill_id,
        version_id=f"version-{skill_id}",
    )


def _provider(*, cache: Path) -> LocalSandboxProvider:
    return LocalSandboxProvider(cache_dir=cache)


def _mapping(sandbox, container_path: str):
    mapping = find_path_mapping(container_path, sandbox.path_mappings)
    assert mapping is not None
    return mapping


def _request(messages, context, *, tools=()) -> ModelRequest:
    return ModelRequest(
        model=object(),
        messages=list(messages),
        state={},
        tools=list(tools),
        runtime=SimpleNamespace(context=context),
    )


def test_public_mount_is_static_and_private_mount_is_per_user(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    provider = _provider(cache=cache)

    first = provider.get(provider.acquire("thread-a", user_id="user-a"))
    second = provider.get(provider.acquire("thread-b", user_id="user-b"))
    assert first is not None and second is not None

    public = _mapping(first, "/mnt/skills/public")
    assert public.local_path == str(cache / "active" / "public")
    assert public.read_only is True

    private_a = _mapping(first, "/mnt/skills/private")
    private_b = _mapping(second, "/mnt/skills/private")
    assert private_a.local_path == str(cache / "active" / "users" / "user-a")
    assert private_b.local_path == str(cache / "active" / "users" / "user-b")
    assert private_a.local_path != private_b.local_path
    assert private_b.local_path != str(cache / "active" / "users" / "user-a")


def test_no_process_wide_skills_or_active_users_mapping(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    provider = _provider(cache=cache)
    sandbox = provider.get(provider.acquire("thread-a", user_id="user-a"))
    assert sandbox is not None

    containers = {mapping.container_path for mapping in sandbox.path_mappings}
    local_paths = {Path(mapping.local_path) for mapping in sandbox.path_mappings}

    assert "/mnt/skills" not in containers
    assert cache not in local_paths
    assert cache / "active" not in local_paths
    assert cache / "active" / "users" not in local_paths


def test_another_users_private_skill_is_absent_from_the_candidate_mappings(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    owner = _provider(cache=cache)
    other = _provider(cache=cache)
    owner_sandbox = owner.get(owner.acquire("thread-a", user_id="user-a"))
    other_sandbox = other.get(other.acquire("thread-b", user_id="user-b"))
    assert owner_sandbox is not None and other_sandbox is not None

    owner_private = Path(_mapping(owner_sandbox, "/mnt/skills/private").local_path)
    skill_file = owner_private / "secret" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(PRIVATE_MARKER, encoding="utf-8")

    other_private = Path(_mapping(other_sandbox, "/mnt/skills/private").local_path)
    assert other_private != owner_private
    assert not other_private.is_relative_to(owner_private)
    other_local_paths = {Path(mapping.local_path) for mapping in other_sandbox.path_mappings}
    assert skill_file not in other_local_paths
    resolved = resolve_sandbox_path(
        "/mnt/skills/private/secret/SKILL.md", other_sandbox.path_mappings
    )
    assert resolved == other_private / "secret" / "SKILL.md"
    assert resolved != skill_file


def _private_snapshot(tmp_path: Path) -> tuple[SkillSnapshot, Path, SkillsConfig]:
    cache = tmp_path / "cache"
    private_file = _write_skill_file(
        cache / "commits" / "private", "private/secret", PRIVATE_MARKER, name="secret"
    )
    config = skills_config(tmp_path)
    private = _skill(
        name="secret",
        path=private_file,
        scope=SkillScope.PRIVATE,
        owner="user-a",
        skill_id="s-private",
    )
    return SkillSnapshot("e14", (), {"user-a": (private,)}), private_file, config


def test_slash_activation_reads_the_private_snapshot_body(tmp_path: Path) -> None:
    snapshot, private_file, config = _private_snapshot(tmp_path)
    activation = SkillActivationMiddleware(
        skills_config=config, user_id="user-a", snapshot=snapshot
    )
    policy = SkillToolPolicyMiddleware(
        skills_config=config,
        owner_token=activation.owner_token,
        user_id="user-a",
        snapshot=snapshot,
    )
    context: dict = {"user_id": "user-a"}
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    activation.wrap_model_call(
        _request([HumanMessage(content="/secret go", id="m1")], context), handler
    )

    injected = [
        message
        for message in seen["request"].messages
        if PRIVATE_MARKER in str(message.content)
    ]
    assert len(injected) == 1
    assert str(private_file) not in str(injected[0].content)
    assert read_secret_context(
        context, SLASH_SKILL_SOURCE_CONTEXT_KEY, token=activation.owner_token
    ) == "/mnt/skills/private/secret/SKILL.md"

    policy_seen: dict = {}

    def policy_handler(request: ModelRequest):
        policy_seen["tools"] = [tool.name for tool in request.tools]
        return "ok"

    policy.wrap_model_call(
        _request(
            [HumanMessage(content="/secret go", id="m1")],
            context,
            tools=[_Tool("read_file"), _Tool("bash")],
        ),
        policy_handler,
    )
    assert policy_seen["tools"] == ["read_file"]


def test_private_precedes_public_with_the_same_name_for_the_owner(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    public_file = _write_skill_file(
        cache / "commits" / "public", "public/same", PUBLIC_MARKER, name="same"
    )
    private_file = _write_skill_file(
        cache / "commits" / "private", "private/same", PRIVATE_MARKER, name="same"
    )
    config = skills_config(tmp_path)
    public = _skill(
        name="same", path=public_file, scope=SkillScope.PUBLIC, skill_id="s-public"
    )
    private = _skill(
        name="same",
        path=private_file,
        scope=SkillScope.PRIVATE,
        owner="user-a",
        skill_id="s-private",
    )
    snapshot = SkillSnapshot("e14", (public,), {"user-a": (private,)})
    activation = SkillActivationMiddleware(
        skills_config=config, user_id="user-a", snapshot=snapshot
    )
    context: dict = {"user_id": "user-a"}
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    activation.wrap_model_call(
        _request([HumanMessage(content="/same go", id="m1")], context), handler
    )

    body = "\n".join(str(message.content) for message in seen["request"].messages)
    assert PRIVATE_MARKER in body
    assert PUBLIC_MARKER not in body
    assert read_secret_context(
        context, SLASH_SKILL_SOURCE_CONTEXT_KEY, token=activation.owner_token
    ) == "/mnt/skills/private/same/SKILL.md"


def test_build_skill_middlewares_uses_the_process_snapshot(tmp_path: Path) -> None:
    from agentkit.agents.middlewares.assembly import build_skill_middlewares

    snapshot, _private_file, config = _private_snapshot(tmp_path)
    original = get_skill_snapshot()
    set_skill_snapshot(snapshot)
    try:
        activation, _policy = build_skill_middlewares(
            AppConfig(skills=config), user_id="user-a"
        )
        context: dict = {"user_id": "user-a"}
        seen: dict = {}

        def handler(request: ModelRequest):
            seen["request"] = request
            return "ok"

        activation.wrap_model_call(
            _request([HumanMessage(content="/secret go", id="m1")], context), handler
        )
    finally:
        set_skill_snapshot(original)

    assert any(PRIVATE_MARKER in str(message.content) for message in seen["request"].messages)


def test_durable_context_reads_the_private_body_from_the_snapshot(tmp_path: Path) -> None:
    snapshot, _private_file, config = _private_snapshot(tmp_path)
    middleware = DurableContextMiddleware(
        skills_container_path="/mnt/skills",
        skills_config=config,
        user_id="user-a",
        snapshot=snapshot,
    )
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    middleware.wrap_model_call(
        _request(
            [HumanMessage(content="hi", id="m1")],
            {"user_id": "user-a"},
        ).override(
            state={
                "skill_context": [
                    {
                        "name": "secret",
                        "path": "/mnt/skills/private/secret/SKILL.md",
                        "description": "secret skill",
                    }
                ]
            }
        ),
        handler,
    )

    assert any(PRIVATE_MARKER in str(message.content) for message in seen["request"].messages)


def test_provider_uses_the_configured_git_cache_dir(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "configured-cache"
    config = AppConfig(
        skills=SkillsConfig(git=SkillGitConfig(repository="origin", cache_dir=str(cache)))
    )
    monkeypatch.setattr(
        "agentkit.sandbox.local.local_sandbox_provider.get_app_config", lambda: config
    )
    provider = LocalSandboxProvider()
    sandbox = provider.get(provider.acquire("thread-a", user_id="user-a"))
    assert sandbox is not None
    assert _mapping(sandbox, "/mnt/skills/public").local_path == str(
        cache / "active" / "public"
    )
    assert _mapping(sandbox, "/mnt/skills/private").local_path == str(
        cache / "active" / "users" / "user-a"
    )


def test_snapshot_middleware_does_not_need_a_filesystem_config(tmp_path: Path) -> None:
    snapshot, _private_file, _config = _private_snapshot(tmp_path)
    activation = SkillActivationMiddleware(user_id="user-a", snapshot=snapshot)
    policy = SkillToolPolicyMiddleware(
        owner_token=activation.owner_token, user_id="user-a", snapshot=snapshot
    )
    context: dict = {"user_id": "user-a"}
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    activation.wrap_model_call(
        _request([HumanMessage(content="/secret go", id="m1")], context), handler
    )
    policy_seen: dict = {}

    def policy_handler(request: ModelRequest):
        policy_seen["tools"] = [tool.name for tool in request.tools]
        return "ok"

    policy.wrap_model_call(
        _request(
            [HumanMessage(content="/secret go", id="m1")],
            context,
            tools=[_Tool("read_file"), _Tool("bash")],
        ),
        policy_handler,
    )

    assert any(
        PRIVATE_MARKER in str(message.content) for message in seen["request"].messages
    )
    assert policy_seen["tools"] == ["read_file"]


def test_operator_mounts_cannot_reintroduce_the_whole_skill_cache(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    safe_mount = tmp_path / "safe"

    def fake_flag(name: str, default, config=None):
        if name == "mounts":
            return [
                {"container_path": "/mnt/skills", "local_path": str(cache)},
                {
                    "container_path": "/mnt/skills/private",
                    "local_path": str(cache / "active" / "users"),
                },
                {"container_path": "/mnt/safe", "local_path": str(safe_mount)},
            ]
        return default

    monkeypatch.setattr(
        "agentkit.sandbox.local.local_sandbox_provider.sandbox_flag", fake_flag
    )
    provider = LocalSandboxProvider(cache_dir=cache)
    sandbox = provider.get(provider.acquire("thread-a", user_id="user-a"))
    assert sandbox is not None

    containers = {mapping.container_path for mapping in sandbox.path_mappings}
    assert "/mnt/safe" in containers
    assert "/mnt/skills" not in containers
    assert _mapping(sandbox, "/mnt/skills/private").local_path == str(
        cache / "active" / "users" / "user-a"
    )



def test_operator_mounts_cannot_shadow_a_configured_skill_root(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    safe_mount = tmp_path / "safe"

    def fake_flag(name: str, default, config=None):
        if name == "mounts":
            return [
                {"container_path": "/opt/skills", "local_path": str(cache)},
                {
                    "container_path": "/opt/skills/private",
                    "local_path": str(cache / "active" / "users"),
                },
                {"container_path": "/mnt/safe", "local_path": str(safe_mount)},
            ]
        return default

    monkeypatch.setattr(
        "agentkit.sandbox.local.local_sandbox_provider.sandbox_flag", fake_flag
    )
    monkeypatch.setattr(
        "agentkit.sandbox.local.local_sandbox_provider._skills_container_root",
        lambda: "/opt/skills",
    )
    provider = LocalSandboxProvider(cache_dir=cache)
    sandbox = provider.get(provider.acquire("thread-a", user_id="user-a"))
    assert sandbox is not None

    containers = {mapping.container_path for mapping in sandbox.path_mappings}
    assert "/mnt/safe" in containers
    assert "/opt/skills" not in containers
    assert _mapping(sandbox, "/opt/skills/private").local_path != str(
        cache / "active" / "users"
    )
    assert _mapping(sandbox, "/opt/skills/public").local_path == str(
        cache / "active" / "public"
    )
    assert _mapping(sandbox, "/opt/skills/private").local_path == str(
        cache / "active" / "users" / "user-a"
    )
