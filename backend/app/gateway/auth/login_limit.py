"""登录失败计数（M9 §2.5）。

**进程内 dict**：单 worker 精确，多 worker 近似——写进已知边界，M9 不引入 Redis 限速。

两条规则写死在这里：

- `max_login_attempts` 最小 2（配置层校验）：单次失败不得锁 IP，
  否则任何人都能用一次错口令把别人锁在门外。
- 计数键是 TCP peer IP（`client_ip()`），不是账号——按账号锁等于给攻击者一个
  "锁定任意账号"的开关。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    failures: int = 0
    locked_until: float = 0.0


class LoginRateLimiter:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def locked_for(self, key: str) -> float:
        """还剩几秒解锁。0 = 没锁。`Retry-After` 和 detail 里的秒数都取自它。"""
        bucket = self._buckets.get(key)
        if bucket is None or not bucket.locked_until:
            return 0.0
        remaining = bucket.locked_until - self._clock()
        if remaining <= 0:
            self._buckets.pop(key, None)  # 到期自动放行，不需要清理任务
            return 0.0
        return remaining

    def is_locked(self, key: str) -> bool:
        return self.locked_for(key) > 0

    def record_failure(self, key: str, *, max_attempts: int, lockout_seconds: float) -> None:
        bucket = self._buckets.setdefault(key, _Bucket())
        bucket.failures += 1
        if bucket.failures >= max(2, int(max_attempts)):
            bucket.locked_until = self._clock() + float(lockout_seconds)

    def reset(self, key: str) -> None:
        """登录成功就清零：否则一次手滑会累积到锁定。"""
        self._buckets.pop(key, None)

    def clear(self) -> None:
        self._buckets.clear()


#: 进程级单例。测试可以直接 `clear()`，不用重建 app。
login_rate_limiter = LoginRateLimiter()
