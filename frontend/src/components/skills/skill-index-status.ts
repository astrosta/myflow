/** 技能版本的向量索引状态：后端没配投影时会直接给出 disabled。 */
export function skillIndexStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case "ready":
      return "已索引";
    case "pending":
      return "索引中";
    case "failed":
      return "索引失败";
    case "disabled":
      return "索引未启用";
    case "unindexed":
      return "未索引";
    default:
      return status || "—";
  }
}
