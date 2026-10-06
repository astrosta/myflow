import { ChatRoute } from "./chat-route";

// 聊天页的重样式**只被真正渲染聊天的路由引入**（M5 §2 的静态根边界）：
// 根布局不引它，所以落地页不会为了一个它根本不渲染的功能多加载几百 KB CSS。
import "./chat.css";

export const metadata = {
  title: "工作区",
};

/**
 * 工作区首页 = **一个还没落地的新会话**。
 *
 * 直接把对话框摆出来，进来就能打字，而不是先点一次"新建对话"。
 *
 * `threadId` 传 `null`，thread 在**第一次发送时**才由 SDK 创建：
 * 进页面就建一个空 thread 会往侧栏塞一堆没人用过的会话，
 * 而"先进来看一眼"是完全正常的用法。
 */
export default function WorkspaceHomePage() {
  return <ChatRoute threadId={null} />;
}
