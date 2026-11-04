"""兼容旧导入；批量精确编辑的实现统一位于 edit.py。"""

from .edit import ApplyPatchTool

__all__ = ["ApplyPatchTool"]
