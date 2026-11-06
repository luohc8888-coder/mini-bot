"""语言适配器协议。

扫描器负责文件发现、大小限制和路径安全；每种语言只需实现
``LanguageAdapter.parse``，把源码解析为统一的 ``ParsedFile``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from .models import ParsedFile, SourceFile


class LanguageAdapter(ABC):
    """语言解析适配器的抽象基类。

    后续增加 Java、C++、TypeScript 时，只需要新增子类并把实例传给
    ``ProjectScanner(adapters=[...])``；CodeGraph 和前端不需要理解具体语言。
    """

    language: ClassVar[str]
    extensions: ClassVar[tuple[str, ...]]

    @classmethod
    def matches(cls, path: Path) -> bool:
        """判断文件是否属于当前适配器。"""
        return path.suffix.lower() in cls.extensions

    @abstractmethod
    def parse(self, source: SourceFile) -> ParsedFile:
        """解析单个源文件。

        Args:
            source: 已读取、已计算 hash 的源码文件。

        Returns:
            统一格式的符号、import、调用和诊断信息。

        Notes:
            适配器必须只做静态分析，不得执行目标代码。无法恢复的结构应以
            diagnostics 或 lower-confidence 记录暴露，不能静默编造。
        """
        raise NotImplementedError


__all__ = ["LanguageAdapter"]
