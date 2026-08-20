"""Unit tests for kd.validation helpers that are OS-agnostic or easily mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from kd.validation import (
    test_kd_disk_space as check_disk_space,
    test_kd_port_available as check_port_available,
    test_kd_powershell_version as check_python_version,
    test_kd_zip_source_reachable as check_zip_source,
)


def test_python_version_check():
    result = check_python_version()  # actually checks Python >= 3.8
    assert result["Pass"] is True
    assert "Current:" in result["Detail"]


def test_zip_source_reachable(sample_zip_dir: Path):
    result = check_zip_source(str(sample_zip_dir))
    assert result["Pass"] is True
    assert "Found packages" in result["Detail"]


def test_zip_source_missing(tmp_path: Path):
    result = check_zip_source(str(tmp_path / "nope"))
    assert result["Pass"] is False
    assert "not found" in result["Detail"].lower()


def test_zip_source_empty_dir(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = check_zip_source(str(empty))
    assert result["Pass"] is False
    assert "No .zip" in result["Detail"]


def test_port_available_high_port():
    # High ephemeral-style port is almost certainly free
    result = check_port_available(54321)
    # We don't assert Pass=True because the port might be in use on the CI host;
    # we only check the shape of the result.
    assert "Name" in result
    assert "Pass" in result
    assert "Detail" in result
    assert "54321" in result["Name"]


def test_disk_space_reports_something(tmp_path: Path):
    result = check_disk_space(str(tmp_path), minimum_gb=1)
    assert "Free:" in result["Detail"] or "Could not" in result["Detail"]
    assert "Pass" in result
