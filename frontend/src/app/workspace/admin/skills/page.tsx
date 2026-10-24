import type { Metadata } from "next";

import { ReviewQueuePage } from "@/components/skills/review-queue-page";

/**
 * 管理端技能审核台（/workspace/admin/skills）。
 *
 * **这一层的路由权限由后端 admin scope 决定**：页面会照常渲染并发出请求，
 * 非管理员拿到的是 403/401，页面把 detail 如实显示出来。侧栏隐藏「审核」入口
 * 只是别让普通用户点一个必然失败的链接，不是权限闸门。
 */
export const metadata: Metadata = {
  title: "技能审核",
};

export default function AdminSkillsPage() {
  return <ReviewQueuePage />;
}
