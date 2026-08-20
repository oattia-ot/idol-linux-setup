"""
Per-component install progress tracking for resume support.
State is persisted as JSON under BasePath/kd-install-state.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def get_kd_state_file_path(base_path: str | Path) -> Path:
    return Path(base_path) / "kd-install-state.json"


def get_kd_state(base_path: str | Path) -> Dict[str, Any]:
    path = get_kd_state_file_path(base_path)
    if not path.is_file():
        return {
            "SchemaVersion": 1,
            "StartedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Components": {},
        }
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "SchemaVersion": 1,
            "StartedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Components": {},
        }


def save_kd_state(base_path: str | Path, state: Dict[str, Any]) -> bool:
    path = get_kd_state_file_path(base_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        return True
    except Exception:
        return False


def set_kd_component_stage(
    base_path: str | Path,
    state: Dict[str, Any],
    component: str,
    stage: str,
) -> Dict[str, Any]:
    if "Components" not in state:
        state["Components"] = {}
    if component not in state["Components"]:
        state["Components"][component] = {"Stages": []}
    stages: List[str] = list(state["Components"][component].get("Stages", []))
    if stage not in stages:
        stages.append(stage)
    state["Components"][component]["Stages"] = stages
    save_kd_state(base_path, state)
    return state


def test_kd_component_stage_complete(state: Dict[str, Any], component: str, stage: str) -> bool:
    comps = state.get("Components", {})
    if component not in comps:
        return False
    return stage in comps[component].get("Stages", [])
