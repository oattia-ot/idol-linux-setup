"""Unit tests for kd.config – pure logic, no Windows dependency."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kd.config import get_kd_config, save_kd_config
from kd.config import test_kd_config_schema as check_config_schema


def test_load_valid_config(tmp_config_file: Path):
    cfg = get_kd_config(tmp_config_file)
    assert cfg["LicenseHost"] == "licenseserver.example"
    assert "Content" in cfg["Components"]
    assert cfg["Ports"]["Content"] == "9100"


def test_missing_config_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        get_kd_config(tmp_path / "does-not-exist.json")


def test_invalid_json_raises(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="Failed to parse"):
        get_kd_config(bad)


def test_env_override(tmp_config_file: Path, monkeypatch):
    monkeypatch.setenv("KD_LICENSEHOST", "from-env.example")
    monkeypatch.setenv("KD_BASEPATH", r"D:\Override\Path")
    cfg = get_kd_config(tmp_config_file)
    assert cfg["LicenseHost"] == "from-env.example"
    assert cfg["BasePath"] == r"D:\Override\Path"


def test_explicit_overrides_beat_env(tmp_config_file: Path, monkeypatch):
    monkeypatch.setenv("KD_LICENSEHOST", "from-env")
    cfg = get_kd_config(tmp_config_file, overrides={"LicenseHost": "from-cli"})
    assert cfg["LicenseHost"] == "from-cli"


def test_strip_surrounding_quotes(tmp_config_file: Path, monkeypatch):
    # Simulate Explorer "Copy as path"
    monkeypatch.setenv("KD_BASEPATH", r'"C:\Quoted\Path"')
    cfg = get_kd_config(tmp_config_file)
    assert cfg["BasePath"] == r"C:\Quoted\Path"
    assert not cfg["BasePath"].startswith('"')


def test_schema_valid(tmp_config_file: Path):
    cfg = get_kd_config(tmp_config_file)
    result = check_config_schema(cfg)
    assert result["IsValid"] is True
    assert result["MissingKeys"] == []


def test_schema_missing_keys(tmp_path: Path):
    incomplete = {"BasePath": "C:\\KD"}
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    cfg = get_kd_config(path)
    result = check_config_schema(cfg)
    assert result["IsValid"] is False
    assert "ZipPath" in result["MissingKeys"]
    assert "Components" in result["MissingKeys"]


def test_save_and_reload(tmp_path: Path, tmp_config_file: Path):
    cfg = get_kd_config(tmp_config_file)
    cfg["LicenseHost"] = "saved.example"
    out = tmp_path / "saved.json"
    assert save_kd_config(cfg, out) is True
    reloaded = get_kd_config(out)
    assert reloaded["LicenseHost"] == "saved.example"
