"""noveletary — novel + secretary: 小説の整合性検証と文脈管理のためのKB/MCPサーバー。"""

from .engine import Fact, NarrativeKB
from .store import Store

__version__ = "0.1.0"
__all__ = ["NarrativeKB", "Fact", "Store"]
