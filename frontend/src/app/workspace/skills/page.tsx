import type { Metadata } from "next";

import { MySkillsPage } from "@/components/skills/my-skills-page";

export const metadata: Metadata = {
  title: "技能",
};

export default function SkillsPage() {
  return <MySkillsPage />;
}
