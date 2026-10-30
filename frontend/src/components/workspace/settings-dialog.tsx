"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ChevronDownIcon, CloseIcon, UserIcon } from "@/components/ui/icons";
import { deleteAccount, updateUsername, type AuthUser } from "@/core/api/auth";
import { authErrorMessage } from "@/core/auth/error-messages";
import { normalizeUsername, validateUsername } from "@/core/auth/username";

/**
 * 设置面板（M9 §2）。
 *
 * 左栏是"设置"标题 + 栏目；右栏 header 是**当前账号**（头像 + 用户名，点名字就地改），
 * 主体是「账号管理」。
 *
 * 两处形制上的取舍：
 *
 * 1. **用户名编辑搬进 header**（千问网页端就是点名字改名）。原来它是一个常驻的
 *    输入框 + 保存按钮——那等于让"改名字"这个低频动作占着整个面板的视觉重心。
 * 2. **退出登录和注销账号都在主体里，且都是红的**。它们是会话/数据的终结动作，
 *    不该和"点开看看"混在一起；注销还多一道确认，因为它不可逆。
 */
export interface SettingsDialogProps {
  user: AuthUser;
  onClose: () => void;
  /** 改完用户名之后把新的用户对象交回上层 */
  onUserUpdated: (user: AuthUser) => void;
  /** 退出登录。由上层负责跳转——设置面板不该知道路由 */
  onSignOut: () => void | Promise<void>;
}

const SECTIONS = [{ id: "account", label: "账号", icon: UserIcon }] as const;

export function SettingsDialog({ user, onClose, onUserUpdated, onSignOut }: SettingsDialogProps) {
  const [section, setSection] = useState<(typeof SECTIONS)[number]["id"]>("account");

  // Esc 关闭：模态框的标配，缺了会让键盘用户被困住
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        aria-label="设置"
        aria-modal="true"
        className="flex max-h-[80vh] w-full max-w-2xl overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <nav
          aria-label="设置栏目"
          className="flex w-44 shrink-0 flex-col border-r border-neutral-200 bg-neutral-50"
        >
          <h2 className="px-4 pb-2 pt-4 text-sm font-semibold text-neutral-900">设置</h2>
          <div className="flex-1 space-y-0.5 p-2 pt-0">
            {SECTIONS.map((item) => {
              const Icon = item.icon;
              const active = section === item.id;
              return (
                <button
                  aria-current={active ? "true" : undefined}
                  className={
                    "flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors " +
                    (active
                      ? "bg-white font-medium text-neutral-900 shadow-sm"
                      : "text-neutral-600 hover:bg-white/70 hover:text-neutral-900")
                  }
                  key={item.id}
                  onClick={() => setSection(item.id)}
                  type="button"
                >
                  <Icon className="shrink-0" size={16} />
                  {item.label}
                </button>
              );
            })}
          </div>
        </nav>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex shrink-0 items-center gap-2 border-b border-neutral-200 px-5 py-3">
            <UsernameHeader onUserUpdated={onUserUpdated} user={user} />
            <button
              aria-label="关闭设置"
              className="ml-auto shrink-0 rounded p-1 text-neutral-500 transition-colors hover:bg-neutral-100 hover:text-neutral-800"
              onClick={onClose}
              type="button"
            >
              <CloseIcon size={16} />
            </button>
          </header>

          <div className="min-h-0 flex-1 overflow-auto px-5 py-5">
            {section === "account" ? (
              <AccountSection onSignOut={onSignOut} user={user} />
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}

const INPUT_CLASS =
  "h-9 min-w-0 flex-1 rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50";

/**
 * header 里的用户名：平时是一行"头像 + 名字"，点一下变成输入框。
 *
 * 保存成功之后**不关闭编辑器**——留在原地能看到名字确实变了；
 * 出错也留在原地，用户不用重新点一遍再改。
 */
function UsernameHeader({
  user,
  onUserUpdated,
}: {
  user: AuthUser;
  onUserUpdated: (user: AuthUser) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(user.username);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const normalized = normalizeUsername(value);
  const shapeError = validateUsername(value);
  const changed = normalized !== user.username;

  const save = async () => {
    if (busy || !changed || shapeError) return;
    setBusy(true);
    setError(null);
    try {
      onUserUpdated(await updateUsername(normalized));
      setEditing(false);
    } catch (cause) {
      setError(authErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <button
        className="flex min-w-0 items-center gap-2 rounded-md px-2 py-1 text-left transition-colors hover:bg-neutral-100"
        onClick={() => {
          setValue(user.username);
          setError(null);
          setEditing(true);
        }}
        title="点击修改用户名"
        type="button"
      >
        <UserIcon className="shrink-0 text-neutral-500" size={16} />
        <span className="truncate text-sm font-semibold text-neutral-900">{user.username}</span>
      </button>
    );
  }

  return (
    <form
      className="flex min-w-0 flex-1 flex-col gap-1"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div className="flex min-w-0 items-center gap-2">
        <UserIcon className="shrink-0 text-neutral-500" size={16} />
        <input
          aria-label="用户名"
          autoFocus
          className={INPUT_CLASS}
          disabled={busy}
          onChange={(event) => setValue(event.target.value)}
          value={value}
        />
        <Button disabled={busy || !changed || Boolean(shapeError)} size="sm" type="submit" variant="primary">
          {busy ? "保存中…" : "保存"}
        </Button>
        <Button disabled={busy} onClick={() => setEditing(false)} size="sm" type="button">
          取消
        </Button>
      </div>
      <p className={shapeError || error ? "pl-6 text-xs text-red-600" : "pl-6 text-xs text-neutral-400"}>
        {error ?? shapeError ?? "3–32 位，字母、数字、下划线、点或连字符"}
      </p>
    </form>
  );
}

function AccountSection({ user, onSignOut }: { user: AuthUser; onSignOut: () => void | Promise<void> }) {
  const [manageOpen, setManageOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const doDelete = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAccount();
      await onSignOut();
    } catch (cause) {
      setError(authErrorMessage(cause));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <section>
        <button
          aria-expanded={manageOpen}
          className="flex w-full items-center justify-between rounded-md px-3 py-2.5 text-left transition-colors hover:bg-neutral-100"
          onClick={() => setManageOpen((open) => !open)}
          type="button"
        >
          <span className="text-sm font-medium text-neutral-800">账号管理</span>
          <ChevronDownIcon
            className={"shrink-0 text-neutral-400 transition-transform " + (manageOpen ? "rotate-180" : "")}
            size={16}
          />
        </button>

        {manageOpen ? (
          <div className="mt-2 space-y-4 rounded-lg border border-neutral-200 bg-neutral-50/60 p-4">
            {user.email ? (
              <div className="space-y-1">
                <span className="block text-xs font-medium text-neutral-500">邮箱</span>
                {/* 旧账号可能保留邮箱；新注册账号没有邮箱，也不再以邮箱登录 */}
                <p className="text-sm text-neutral-800">{user.email}</p>
              </div>
            ) : null}

            <div className="space-y-2 border-t border-neutral-200 pt-4">
              {confirming ? (
                <div className="flex flex-wrap items-center gap-3">
                  <span className="text-sm text-neutral-700">注销后这个账号将无法登录，确定吗？</span>
                  <Button disabled={busy} onClick={() => void doDelete()} size="sm" variant="danger">
                    {busy ? "注销中…" : "确认注销"}
                  </Button>
                  <Button disabled={busy} onClick={() => setConfirming(false)} size="sm" type="button">
                    再想想
                  </Button>
                </div>
              ) : (
                <>
                  {/*
                    注销用**实心红**，而退出登录用描边红：两者后果不对等——
                    注销不可逆且数据进回收期，退出登录再登一次就回来了。
                    同一种红会让它们看起来一样重，那是层级错误。
                  */}
                  <Button
                    className="border-red-600 bg-red-600 text-white hover:bg-red-700 hover:border-red-700"
                    onClick={() => setConfirming(true)}
                    size="sm"
                    variant="danger"
                  >
                    注销账号
                  </Button>
                  <p className="text-xs text-neutral-400">注销后无法登录，数据进入回收期。</p>
                </>
              )}
              {error ? (
                <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">
                  {error}
                </p>
              ) : null}
            </div>
          </div>
        ) : null}
      </section>

      <div className="border-t border-neutral-200 pt-5">
        <Button onClick={() => void onSignOut()} variant="danger">
          退出登录
        </Button>
      </div>
    </div>
  );
}
