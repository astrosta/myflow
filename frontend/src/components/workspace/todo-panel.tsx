"use client";

import { Collapsible } from "@/components/ui/collapsible";
import type { TodoItem } from "@/core/types";
import { cn } from "@/lib/utils";

/**
 * 任务清单。数据来自折叠后的 ThreadState（updates 帧里的 todos 字段），
 * 组件只负责展示，不自己取。
 *
 * 默认展开：这份清单本身就是"Agent 现在打算干什么"的进度条，
 * 折叠起来等于把它变成第二个没人点的按钮。
 */
const STATUS_META: Record<NonNullable<TodoItem["status"]>, { icon: string; label: string; tone: string }> = {
  pending: { icon: "○", label: "待办", tone: "text-neutral-400" },
  in_progress: { icon: "◐", label: "进行中", tone: "text-emerald-600" },
  completed: { icon: "●", label: "已完成", tone: "text-neutral-400" },
};

export interface TodoPanelProps {
  todos: readonly TodoItem[];
  className?: string;
}

export function TodoPanel({ todos, className }: TodoPanelProps) {
  if (!todos.length) return null;

  const done = todos.filter((todo) => todo.status === "completed").length;

  return (
    <section
      aria-label="任务清单"
      className={cn("rounded-lg border border-neutral-200 bg-white px-3 py-2", className)}
    >
      <Collapsible defaultOpen meta={done + "/" + todos.length} trigger={<span>任务清单</span>}>
        <ul className="space-y-1.5">
          {todos.map((todo, index) => {
            const status = todo.status ?? "pending";
            const meta = STATUS_META[status];
            return (
              <li className="flex items-start gap-2 text-xs" key={String(index) + "-" + todo.content}>
                <span aria-hidden className={cn("mt-px shrink-0", meta.tone)} title={meta.label}>
                  {meta.icon}
                </span>
                <span
                  className={cn(
                    "min-w-0 flex-1 break-words",
                    status === "completed" ? "text-neutral-400 line-through" : "text-neutral-700",
                  )}
                >
                  {todo.content}
                </span>
              </li>
            );
          })}
        </ul>
      </Collapsible>
    </section>
  );
}
