"""Name-keyed registries. Components self-register on import; config selects which are enabled."""
from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str):
        self.kind = kind
        self._factories: dict[str, Callable[..., T]] = {}

    def register(self, name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
        def deco(factory: Callable[..., T]) -> Callable[..., T]:
            if name in self._factories:
                raise ValueError(f"duplicate {self.kind} '{name}'")
            self._factories[name] = factory
            return factory

        return deco

    def create(self, name: str, **kwargs) -> T:
        try:
            return self._factories[name](**kwargs)
        except KeyError:
            raise KeyError(f"unknown {self.kind} '{name}'; known: {sorted(self._factories)}") from None

    def names(self) -> list[str]:
        return sorted(self._factories)


platforms: Registry = Registry("platform")
llm_providers: Registry = Registry("llm provider")
browser_drivers: Registry = Registry("browser driver")
