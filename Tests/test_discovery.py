"""Unit tests for kd.discovery – ZIP selection and executable ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from kd.discovery import (
    find_kd_component_executable,
    find_kd_component_zip,
)
from kd.discovery import test_kd_windows_executable as check_windows_executable


def test_windows_executable_detects_mz(tmp_path: Path):
    pe = tmp_path / "good.exe"
    pe.write_bytes(b"MZ" + b"\x00" * 50)
    assert check_windows_executable(pe) is True

    not_pe = tmp_path / "bad.exe"
    not_pe.write_bytes(b"#!/bin/sh\n")
    assert check_windows_executable(not_pe) is False

    assert check_windows_executable(tmp_path / "missing.exe") is False


def test_find_zip_prefers_windows_marked(sample_zip_dir: Path):
    result = find_kd_component_zip(sample_zip_dir, "LicenseServer")
    assert result["Success"] is True
    assert "WINDOWS" in result["Zip"].name.upper()
    assert "LINUX" not in result["Zip"].name.upper()


def test_find_zip_blocks_linux_only(sample_zip_dir: Path):
    # Community only has a LINUX package in the fixture
    result = find_kd_component_zip(sample_zip_dir, "Community")
    assert result["Success"] is False
    assert "Linux" in result["Reason"] or "linux" in result["Reason"].lower()


def test_find_zip_accepts_unmarked_fallback(sample_zip_dir: Path):
    result = find_kd_component_zip(sample_zip_dir, "Category")
    assert result["Success"] is True
    assert "platform unmarked" in result["Reason"].lower() or result["Reason"] == "OK"


def test_find_zip_agentstore_uses_content(sample_zip_dir: Path):
    result = find_kd_component_zip(sample_zip_dir, "Agentstore")
    assert result["Success"] is True
    assert "Content" in result["Zip"].name


def test_find_zip_missing(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = find_kd_component_zip(empty, "LicenseServer")
    assert result["Success"] is False
    assert "No file matching" in result["Reason"]


def test_find_executable_picks_main_binary(sample_component_tree: Path):
    result = find_kd_component_executable(sample_component_tree, "Content")
    assert result["Success"] is True
    assert result["Executable"].name.lower() == "content.exe"
    # Helper / pipeline binaries are excluded by name/path filters, not merely ranked lower.
    names = [p.name.lower() for p in result["AllCandidates"]]
    assert "makeda.exe" not in names
    assert "extract.exe" not in names
    assert "vcredist_x64.exe" not in names
    assert "setup.exe" not in names


def test_find_executable_excludes_vcredist_and_setup(sample_component_tree: Path):
    result = find_kd_component_executable(sample_component_tree, "Content")
    assert result["Success"] is True
    picked = result["Executable"].name.lower()
    assert "vcredist" not in picked
    assert picked != "setup.exe"
    # Also ensure they are not the top pick
    for cand in result["AllCandidates"][:1]:
        assert "vcredist" not in cand.name.lower()
        assert cand.name.lower() != "setup.exe"


def test_find_executable_no_candidates(tmp_path: Path):
    empty = tmp_path / "empty_comp"
    empty.mkdir()
    result = find_kd_component_executable(empty, "Content")
    assert result["Success"] is False
    assert "No suitable .exe" in result["Reason"]


def test_find_zip_nifi(tmp_path: Path):
    zdir = tmp_path / "zips"
    zdir.mkdir()
    import zipfile
    with zipfile.ZipFile(zdir / "nifi-2.10.0-bin.zip", "w") as zf:
        zf.writestr("dummy.txt", "test")
    with zipfile.ZipFile(zdir / "other-nifi.zip", "w") as zf:
        zf.writestr("dummy.txt", "test")
    result = find_kd_component_zip(zdir, "NiFi")
    assert result["Success"] is True
    assert "nifi-2.10.0-bin.zip" in result["Zip"].name


def test_find_executable_nifi(tmp_path: Path):
    root = tmp_path / "NiFi"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nifi.cmd").write_text("@echo off\n", encoding="utf-8")
    (root / "lib").mkdir()
    result = find_kd_component_executable(root, "NiFi")
    assert result["Success"] is True
    assert result["Executable"].name.lower() == "nifi.cmd"
