"""命令核心：鉴权、版本、幂等、CAS、处理器注册。"""

from .bus import CommandBus

__all__ = ["CommandBus"]
