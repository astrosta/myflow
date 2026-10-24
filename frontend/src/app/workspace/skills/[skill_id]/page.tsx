import type { Metadata } from "next";

import { SkillEditorPage } from "@/components/skills/skill-editor-page";

export const metadata: Metadata = {
  title: "技能编辑器",
};

export default async function SkillEditorRoute({
  params,
}: {
  params: Promise<{ skill_id: string }>;
}) {
  const { skill_id } = await params;
  return <SkillEditorPage skillId={skill_id} />;
}
