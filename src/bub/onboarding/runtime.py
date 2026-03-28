from __future__ import annotations

from pydantic import BaseModel


def resolve_runtime_model[T: BaseModel](framework: object, plugin_id: str, model: type[T]) -> T:
    service_getter = getattr(framework, "get_marketplace_service", None)
    if not callable(service_getter):
        return model()
    try:
        runtime = service_getter().load_runtime(plugin_id)
    except Exception:
        return model()
    if not isinstance(runtime, dict) or not runtime:
        return model()
    return model.model_validate(runtime)
