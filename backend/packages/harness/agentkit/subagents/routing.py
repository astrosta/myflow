"""委派措辞的唯一来源（M8 §1 / §18）。

三处必须写**同一套**"收益 / 硬否决 / 成本"，否则模型会在提示词里被鼓励并行、
在 task docstring 里被劝阻，最后按自己的偏好选：

1. lead 系统提示词的 ``{subagent_section}``（§16）
2. ``task`` 工具的 docstring（§9）
3. 每个内置子代理的 ``description``（§5.2）

真正想否决的只有两条：**输出依赖**与**可变状态重叠**。重复发现、启动开销、
综合那一轮是**成本**，要权衡，但不是"不许派"。

本模块只放字符串常量：不要在这里 import 任何东西，免得把提示词变成一张隐藏的依赖图。
"""

from __future__ import annotations

#: 三条收益（§1）：什么时候委派是真的赚。
ROUTING_BENEFITS = "并行缩短等待、专家能力、上下文隔离"

#: §16：解析后的 ``max_concurrent == 1`` 时只留这两条。提示词还在教模型并行 3 个、
#: 中间件却只放行 1 个，模型会空转——所以并行那一条要**删掉**，不是"淡化"。
ROUTING_BENEFITS_SEQUENTIAL = "专家能力、上下文隔离"

#: 两条硬否决（§1）：不是"成本"，是"不许并行派"。
ROUTING_VETOES = (
    "依赖另一个委派产出的工作，或与同级代理写同一批文件的工作"
)

#: 委派的实打实开销（§1）：简单活自己干更快。
ROUTING_COST = "启动轮次、重复探索和一轮综合"

__all__ = [
    "ROUTING_BENEFITS",
    "ROUTING_BENEFITS_SEQUENTIAL",
    "ROUTING_COST",
    "ROUTING_VETOES",
]
