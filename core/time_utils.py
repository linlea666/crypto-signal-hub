"""项目统一时间工具。

Docker 容器默认时区为 UTC，本模块确保所有业务时间使用北京时间。
全项目应使用 `now_beijing()` 替代 `datetime.now()`。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

# 北京时间 = UTC+8
_BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    """返回当前北京时间（带时区信息）"""
    return datetime.now(_BEIJING_TZ)


def now_beijing_str() -> str:
    """返回 ISO 格式的北京时间字符串（用于日志和存储）"""
    return now_beijing().strftime("%Y-%m-%d %H:%M:%S")
