import type { Metadata } from "next";

import { MyProfilePage } from "@/components/profile/my-profile-page";

export const metadata: Metadata = {
  title: "我的画像",
};

export default function ProfilePage() {
  return <MyProfilePage />;
}
