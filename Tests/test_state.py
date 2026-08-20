"""Unit tests for kd.state – resume / stage tracking."""

from __future__ import annotations

from pathlib import Path

import pytest

from kd.state import (
    get_kd_state,
    get_kd_state_file_path,
    save_kd_state,
    set_kd_component_stage,
)
from kd.state import test_kd_component_stage_complete as stage_complete


def test_fresh_state(tmp_path: Path):
    st = get_kd_state(tmp_path)
    assert st["SchemaVersion"] == 1
    assert "Components" in st
    assert st["Components"] == {}


def test_set_and_test_stage(tmp_path: Path):
    st = get_kd_state(tmp_path)
    st = set_kd_component_stage(tmp_path, st, "Content", "Extracted")
    assert stage_complete(st, "Content", "Extracted") is True
    assert stage_complete(st, "Content", "Configured") is False
    assert stage_complete(st, "LicenseServer", "Extracted") is False


def test_stage_persisted_to_disk(tmp_path: Path):
    st = get_kd_state(tmp_path)
    set_kd_component_stage(tmp_path, st, "Content", "Extracted")
    set_kd_component_stage(tmp_path, st, "Content", "Configured")

    # Reload from disk
    st2 = get_kd_state(tmp_path)
    assert stage_complete(st2, "Content", "Extracted")
    assert stage_complete(st2, "Content", "Configured")
    assert get_kd_state_file_path(tmp_path).is_file()


def test_duplicate_stage_not_duplicated(tmp_path: Path):
    st = get_kd_state(tmp_path)
    st = set_kd_component_stage(tmp_path, st, "Content", "Extracted")
    st = set_kd_component_stage(tmp_path, st, "Content", "Extracted")
    stages = st["Components"]["Content"]["Stages"]
    assert stages.count("Extracted") == 1


def test_corrupt_state_file_starts_fresh(tmp_path: Path):
    path = get_kd_state_file_path(tmp_path)
    path.write_text("NOT JSON {{{", encoding="utf-8")
    st = get_kd_state(tmp_path)
    assert st["Components"] == {}
    assert st["SchemaVersion"] == 1
