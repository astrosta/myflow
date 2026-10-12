"""集中组装中间件（M6 §3；M7 §3.9 填技能槽位）。

**注册顺序 ≠ 执行顺序**（M6 §2.3）：

* wrap_model_call / wrap_tool_call 是洋葱：先注册的在外层。
* after_model 是反序：后注册的先跑。

每个顺序约束都在下面写了原因。SLOT 注释是给后续里程碑留的插入点，条件为假时不 append。
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig

from agentkit.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from agentkit.agents.middlewares.durable_context_middleware import DurableContextMiddleware
from agentkit.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
from agentkit.agents.middlewares.input_sanitization_middleware import InputSanitizationMiddleware
from agentkit.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
from agentkit.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
from agentkit.agents.middlewares.skill_tool_policy_middleware import SkillToolPolicyMiddleware
from agentkit.agents.middlewares.summarization_middleware import create_summarization_middleware
from agentkit.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
from agentkit.agents.middlewares.title_middleware import TitleMiddleware
from agentkit.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from agentkit.agents.middlewares.uploads_middleware import UploadsMiddleware
from agentkit.config import AppConfig, get_app_config
from agentkit.runtime.secret_context import new_owner_token
from agentkit.sandbox.middleware import SandboxMiddleware


def build_skill_middlewares(
    app_config: AppConfig,
    *,
    user_id: str | None = None,
    available_skills: frozenset[str] | None = None,
) -> tuple[SkillActivationMiddleware, SkillToolPolicyMiddleware]:
    """Activation 与 Policy 共用**同一个** owner token：只有这一对能读 slash 源路径（§3.6）。

    user_id 只是组装时的默认值；真正的身份在每个 hook 里从 runtime.context 现读
    （网关的 context 是权威来源，M6 §4.1）。
    """
    token = new_owner_token()
    activation = SkillActivationMiddleware(
        skills_config=app_config.skills,
        user_id=user_id,
        available_skills=available_skills,
        owner_token=token,
    )
    policy = SkillToolPolicyMiddleware(
        skills_config=app_config.skills,
        owner_token=token,
        user_id=user_id,
        available_skills=available_skills,
    )
    return activation, policy


def build_runtime_middlewares(
    app_config: AppConfig,
    *,
    include_uploads: bool,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    """基础层：错误处理、沙箱、上传。子代理（M8）与 lead 共用这一层。"""
    chain: list[AgentMiddleware] = []

    # --- wrap_model_call 外→内 ---
    # 输入清洗最外层：内层（含 LLM 重试）看到的都必须已经是清洗后的消息。
    chain.append(InputSanitizationMiddleware())
    # SLOT: ToolOutputBudgetMiddleware / ToolResultSanitizationMiddleware（W3）

    # --- before_agent ---
    chain.append(ThreadDataMiddleware(lazy_init=lazy_init))
    if include_uploads:
        chain.append(UploadsMiddleware())  # lead-only
    chain.append(SandboxMiddleware())

    # --- wrap_model_call 续 / wrap_tool_call 外→内 ---
    chain.append(DanglingToolCallMiddleware())  # 先补齐 orphan tool 对，再让错误处理看到合法历史
    chain.append(LLMErrorHandlingMiddleware(app_config=app_config))
    # SLOT: ToolReceipt / Guardrail / SandboxAudit / ReadBeforeWrite / ToolProgress（M10/W3）
    # 错误处理放最内：外层看到的是盖好 myflow_tool_meta 的结果。
    chain.append(ToolErrorHandlingMiddleware(app_config=app_config))
    return chain


def build_middlewares(
    config: RunnableConfig,
    model_name: str | None,
    *,
    app_config: AppConfig | None = None,
    custom_middlewares: list[AgentMiddleware] | None = None,
    user_id: str | None = None,
    available_skills: frozenset[str] | None = None,
) -> list[AgentMiddleware]:
    """lead 的完整链：基础层 + 上下文投影 + 技能（Activation → Policy → DurableContext）。"""
    resolved = app_config or get_app_config()
    chain = build_runtime_middlewares(resolved, include_uploads=True, lazy_init=True)
    chain.append(DynamicContextMiddleware(app_config=resolved))

    # 顺序硬约束（§3.9）：Activation 紧挨 Policy 紧挨 DurableContext。
    # 中间插别的会让共享的 owner token 或 skill_context 对不上。
    chain.extend(
        build_skill_middlewares(resolved, user_id=user_id, available_skills=available_skills)
    )
    chain.append(
        DurableContextMiddleware(
            skills_container_path=resolved.skills.container_path,
            skill_file_read_tool_names=resolved.summarization.skill_file_read_tool_names,
        )
    )
    if resolved.summarization.enabled:
        summarization = create_summarization_middleware(
            app_config=resolved, run_model_name=model_name
        )
        if summarization is not None:
            chain.append(summarization)
    # SLOT: TodoList if is_plan_mode
    chain.append(TitleMiddleware(app_config=resolved))
    # SLOT: Memory（M10）/ ViewImage / SystemMessageCoalescing / LoopDetection（W3）
    # SLOT: SubagentLimitMiddleware if subagent_enabled（M8，lead-only）
    # SLOT: TerminalResponse / SafetyFinishReason / Clarification（W3，必须最后注册）
    if custom_middlewares:
        chain.extend(custom_middlewares)
    return chain


def build_subagent_runtime_middlewares(
    app_config: AppConfig,
    *,
    model_name: str | None = None,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    """子代理（M8）复用基础层：没有上传、没有标题、没有技能绑定。"""
    del model_name
    return build_runtime_middlewares(app_config, include_uploads=False, lazy_init=lazy_init)
