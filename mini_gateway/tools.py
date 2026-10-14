"""Local tools: Python callables the gateway runs under a permit.

Every tool here is governed. There is no unpermitted path: a call needs a
wallet, a permit and an idempotency key before the tool body can run.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ToolFunc = Callable[..., Awaitable[Any]]


@dataclass
class LocalTool:
    service_id: str
    name: str
    description: str
    category: str
    func: ToolFunc = field(repr=False)
    credits_per_unit: Decimal
    unit_name: str
    # When set, the gateway overwrites this argument with the call's own
    # idempotency key, so the caller can not pick a different one.
    idempotency_key_arg: str | None = None

    def input_schema(self) -> dict[str, Any]:
        params = {
            name: {"type": "string"}
            for name in inspect.signature(self.func).parameters
            if name != self.idempotency_key_arg
        }
        return {"type": "object", "properties": params, "required": list(params)}

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.service_id,
            "description": self.description,
            "inputSchema": self.input_schema(),
            "annotations": {
                "title": self.name,
                "category": self.category,
                "credits_per_unit": str(self.credits_per_unit),
                "unit_name": self.unit_name,
                "requires_permit": True,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, LocalTool] = {}

    def register_local(
        self,
        *,
        service_id: str,
        name: str,
        description: str,
        func: ToolFunc,
        credits_per_unit: float | Decimal | str,
        category: str = "custom",
        unit_name: str = "call",
        idempotency_key_arg: str | None = None,
    ) -> dict[str, Any]:
        if service_id in self._tools:
            raise ValueError(f"tool {service_id!r} is already registered")
        if not inspect.iscoroutinefunction(func):
            raise TypeError("local tools must be async functions")
        price = Decimal(str(credits_per_unit))
        if not price.is_finite() or price < 0:
            raise ValueError("credits_per_unit must be a finite, non negative number")
        if idempotency_key_arg and idempotency_key_arg not in inspect.signature(func).parameters:
            raise ValueError(f"{idempotency_key_arg!r} is not a parameter of the tool")
        tool = LocalTool(
            service_id=service_id,
            name=name,
            description=description,
            category=category,
            func=func,
            credits_per_unit=price,
            unit_name=unit_name,
            idempotency_key_arg=idempotency_key_arg,
        )
        self._tools[service_id] = tool
        return tool.describe()

    def unregister_local(self, service_id: str) -> None:
        self._tools.pop(service_id, None)

    def get(self, service_id: str) -> LocalTool | None:
        return self._tools.get(service_id)

    def all(self) -> list[LocalTool]:
        return list(self._tools.values())
