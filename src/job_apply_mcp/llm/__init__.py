"""LLM layer: providers (self-registering) and the complexity-aware router."""
from . import providers  # noqa: F401  (registers providers)
from .base import ClientTask, LLMResult, Task
from .router import LLMRouter

__all__ = ["ClientTask", "LLMResult", "LLMRouter", "Task"]
