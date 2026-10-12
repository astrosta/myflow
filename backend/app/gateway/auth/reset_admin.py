"""`python -m backend.app.gateway.auth.reset_admin`（M9 §2.7）。

忘记密码时的**离线**救援路径：

1. 随机口令、`token_version += 1`（旧 cookie 立刻失效）、`needs_setup=true`
2. 凭据写进 `0600` 文件
3. stdout **只打路径，不打明文**

没有 admin 时不在这里自动建号——那是 `/setup` 的活（`POST /api/v1/auth/initialize`）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys
from pathlib import Path

from agentkit.config import get_app_config
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.store import make_user_store

DEFAULT_CREDENTIALS_FILE = "reset-admin-credentials.txt"


def _secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    # 0600：既是权限也是意图声明。Windows 上 chmod 只影响只读位，部署文档另有说明。
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


async def reset_admin(*, email: str | None, credentials_file: Path) -> int:
    """返回进程退出码：0 成功，2 认证关闭，3 没有 admin。"""
    app_config = get_app_config()
    if not app_config.auth.enabled:
        print("auth.enabled=false: there is no admin account to reset.", file=sys.stderr)
        return 2

    await init_engine_from_config(app_config.database)
    try:
        store = make_user_store(get_session_factory())
        service = AuthService(store)

        user = await store.get_by_email(email) if email else None
        if user is None and email is None:
            admin_id = await store.first_admin_id()
            user = await store.get(admin_id) if admin_id else None
        if user is None:
            print(
                "no admin account found; open /setup and POST /api/v1/auth/initialize instead.",
                file=sys.stderr,
            )
            return 3

        # allow_self=True：这是离线救援，不是在管理别人的账号。
        password = await service.admin_reset_password(
            actor_id=user["id"], user_id=user["id"], allow_self=True
        )
        _secure_write(
            credentials_file,
            f"email: {user['email']}\npassword: {password}\n"
            "note: needs_setup=true — you must change this password on first login.\n",
        )
        # 只打路径：stdout 可能进日志、进 CI 输出、进工单。
        print(f"credentials written to {credentials_file} (mode 0600); sign in and change the password.")
        return 0
    finally:
        await close_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset an admin password offline (M9 §2.7).")
    parser.add_argument("--email", default=None, help="target account; default: the first admin")
    parser.add_argument(
        "--credentials-file",
        default=DEFAULT_CREDENTIALS_FILE,
        help=f"where to write the new password (mode 0600); default {DEFAULT_CREDENTIALS_FILE}",
    )
    args = parser.parse_args(argv)
    return asyncio.run(reset_admin(email=args.email, credentials_file=Path(args.credentials_file)))


if __name__ == "__main__":
    raise SystemExit(main())
