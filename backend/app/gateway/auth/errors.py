"""M9 §2.12 / §2.13 的域异常。

放在 auth 包里的**叶子模块**：它只依赖 `error_response`，不 import gateway 的其它模块——
否则 `deps → auth → errors → services → deps` 会成环。
`backend.app.gateway.errors` 负责把它们登记进异常处理器。
"""

from __future__ import annotations

from typing import ClassVar

from backend.app.gateway.error_response import CodedError


class Unauthenticated(CodedError):
    """白名单外没有有效会话 / PAT / 内部 token。**默认拒绝**的那一侧（§2.5）。"""

    status_code: ClassVar[int] = 401
    code: ClassVar[str] = "unauthenticated"


class AuthUnavailable(CodedError):
    """**服务端**读不出认证设置（配置缺失 / 解析失败）。

    这不是"你没登录"：用户什么都没做错，会话也没失效。报 401 `unauthenticated`
    会被前端翻译成"登录状态已失效，请重新登录"——把人送去重新登录，
    而他再登一次还是同一个错。503 + 自己的 code 才是实话（而且**仍然 fail-closed**）。
    """

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "auth_unavailable"


class AuthUnavailable(CodedError):
    """**服务端**读不出认证设置（配置缺失 / 解析失败）。

    这不是"你没登录"：用户什么都没做错，会话也没失效。报 401 `unauthenticated`
    会被前端翻译成"登录状态已失效，请重新登录"——把人送去重新登录，
    而他再登一次还是同一个错。503 + 自己的 code 才是实话（而且**仍然 fail-closed**）。
    """

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "auth_unavailable"


class InvalidCredentials(CodedError):
    """**收敛形态**：邮箱不存在 / 密码错 / 账号禁用都长这样。

    只在 `auth.local.specific_errors=false` 时使用——那是"宁可让用户多点两次，
    也不让人探测某个邮箱有没有注册"的部署选择。默认（true）走下面那几个具体错误。
    """

    status_code: ClassVar[int] = 401
    code: ClassVar[str] = "invalid_credentials"


class AccountNotFound(CodedError):
    """登录标识（邮箱**或**用户名）在库里没有账号。

    告诉用户"去注册"，而不是让他反复试密码。名字里**不出现** email：
    用户敲的可能是用户名，而且"是邮箱没注册还是用户名没注册"这件事不该被回答——
    说了等于把两个命名空间都交出去探测。
    """

    status_code: ClassVar[int] = 401
    code: ClassVar[str] = "account_not_found"


class WrongPassword(CodedError):
    """邮箱对、口令错。**不告诉用户"第几位错了"**——口令本身永远不回显。"""

    status_code: ClassVar[int] = 401
    code: ClassVar[str] = "wrong_password"


class AccountDisabled(CodedError):
    """口令正确，但账号被管理员禁用（§2.13）。口令不对的人拿不到这个答案。"""

    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "account_disabled"


class AccountDeleted(CodedError):
    """口令正确，但账号处于软删的回收期（§2.13 不变量 4）。"""

    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "account_deleted"


class InvalidEmail(CodedError):
    """邮箱形状不对。detail 说清**哪一条规则**没过，并回显被拒的值。

    回显是安全的：那是调用方刚刚自己敲进来的输入，不是库里的数据。
    """

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_email"


class InvalidUsername(CodedError):
    """用户名形状不对（长度 / 字符集 / 首尾 / 保留字）。detail 说清是哪一条。"""

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_username"


class UsernameTaken(CodedError):
    """用户名被占了。和"邮箱已注册"分开报——用户要改的字段不一样。"""

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "username_taken"


class InvalidPassword(CodedError):
    """口令不满足策略，且不属于"太短/太长"两类。detail 只说规则，不回显口令。"""

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_password"


class PasswordTooShort(CodedError):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "password_too_short"


class PasswordTooLong(CodedError):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "password_too_long"


class LoginRateLimited(CodedError):
    """detail 里带"还有多少秒"，响应头带 `Retry-After`，前端不用猜。"""

    status_code: ClassVar[int] = 429
    code: ClassVar[str] = "login_rate_limited"

    def __init__(self, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class AdminAlreadyExists(CodedError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "admin_already_exists"


class RegistrationClosed(CodedError):
    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "registration_closed"


class SessionRequired(CodedError):
    """PAT 打了只允许会话的路由（例如改密）。"""

    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "session_required"


class SetupRequired(CodedError):
    """`needs_setup=true` 的用户只能改密与登出（§2.13 不变量 3）。

    **不要只在前端拦**：admin 重置密码之后，用户在拿到新密码之前不该能用旧会话做任何事。
    """

    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "setup_required"


class UnknownOwner(CodedError):
    """`X-MyFlow-Owner-User-Id` 不是存在且启用的账号（§2.9 关 2）。

    不允许"先发消息再建号"：那会写进一个没人拥有的桶。
    """

    status_code: ClassVar[int] = 401
    code: ClassVar[str] = "unknown_owner"


class ChannelNotBound(CodedError):
    """该 owner 没有绑定过这个通道（§2.9 关 3）。绑定表在 9D §5.3。"""

    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "channel_not_bound"


class LastAdmin(CodedError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "last_admin"


class CannotModifySelf(CodedError):
    """防的不是恶意，是手滑把自己锁在外面——恢复要动 CLI（§2.13 不变量 2）。"""

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "cannot_modify_self"


class UserNotFound(CodedError):
    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "user_not_found"


class UserAlreadyExists(CodedError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "user_already_exists"


class TooManyPats(CodedError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "too_many_pats"


class InvalidScope(CodedError):
    """PAT 的 scope 必须从固定菜单里选，不能自定义字符串（§2.15）。"""

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_scope"


class OriginNotAllowed(CodedError):
    """浏览器 Origin 不在允许列表里（§2.6）。login CSRF / session fixation 的门。"""

    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "origin_not_allowed"


class CsrfFailed(CodedError):
    """double-submit 对不上（§2.6）。"""

    status_code: ClassVar[int] = 403
    code: ClassVar[str] = "csrf_failed"
