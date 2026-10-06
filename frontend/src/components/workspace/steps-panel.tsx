"use client";

import { Collapsible } from "@/components/ui/collapsible";
import type { StepsGroup } from "@/core/messages/group";
import { cn } from "@/lib/utils";

import { StepItem } from "./step-item";

/**
 * "处理中"分组。
 *
 * 它在流式期间承载的是**还没定型的 AI 消息**：纯内容的消息也留在这里，
 * 因为 provider 可能稍后往同一条消息上追加工具调用块（§6.1），
 * 提前把它外提成终态气泡会让那段文字在结束的瞬间跳一次位置。
 *
 * 默认展开：工具卡片要能被看见（参数和结果是验收第一条），
 * 折叠只是给"这一轮跑了几十步"的长会话留一个收起手段。
 */
export interface StepsPanelProps {
  group: StepsGroup;
  /**
   * 正在做 gap 恢复（§5.2）。
   *
   * 恢复期间 stream 的 loading 是 false，光看 group.streaming 会得出
   * "这一轮已经结束"的结论，而后台的 run 其实还在跑——那正是 §5.2 点名
   * 必须避免的误导：用户会以为回答就这么短。所以这里把恢复也算作"还在跑"。
   */
  recovering?: boolean;
  className?: string;
}

export function StepsPanel({ group, recovering = false, className }: StepsPanelProps) {
  const count = group.steps.length;
  const active = group.streaming || recovering;

  return (
    <section aria-label="处理过程" className={cn("w-full", className)} data-run-id={group.runId ?? undefined}>
      <Collapsible
        contentClassName="space-y-3"
        defaultOpen
        meta={count + " 步"}
        trigger={
          <span className="inline-flex items-center gap-2">
            {active ? (
              <span aria-hidden className="stream-pulse h-1.5 w-1.5 rounded-full bg-emerald-500" />
            ) : (
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-neutral-300" />
            )}
            <span>
              {group.streaming ? "处理中…" : recovering ? "处理中…（连接恢复中）" : "已完成"}
            </span>
          </span>
        }
      >
        <ol className="space-y-4">
          {group.steps.map((step) => (
            <StepItem key={step.id} step={step} streaming={active} />
          ))}
        </ol>
      </Collapsible>
    </section>
  );
}
