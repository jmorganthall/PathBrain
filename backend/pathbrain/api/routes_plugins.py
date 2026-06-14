"""Plugins endpoint: list registered benchmark plugins."""
from __future__ import annotations

from fastapi import APIRouter

from ..plugins import iter_plugins
from ..schemas import PluginInfo

router = APIRouter()


@router.get("/plugins", response_model=list[PluginInfo])
def list_plugins() -> list[PluginInfo]:
    return [PluginInfo(name=p.name, description=p.description) for p in iter_plugins()]
