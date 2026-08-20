"""Unit tests for kd.ini_config – safe .cfg editing, backup, atomic write."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kd.ini_config import backup_kd_file, restore_kd_file_from_backup, update_kd_ini_file


def _write_cfg(path: Path, content: str) -> Path:
    path.write_text(content.replace("\n", "\r\n"), encoding="utf-8")
    return path


def test_update_existing_key(tmp_path: Path):
    cfg = _write_cfg(
        tmp_path / "content.cfg",
        "[Service]\nHost=oldhost\nPort=9000\n[License]\nLicenseServerHost=old\n",
    )
    assert update_kd_ini_file(cfg, "Service", "Host", "newhost") is True
    text = cfg.read_text(encoding="utf-8")
    assert "Host=newhost" in text
    assert "Host=oldhost" not in text
    assert "Port=9000" in text  # untouched


def test_add_key_to_existing_section(tmp_path: Path):
    cfg = _write_cfg(tmp_path / "c.cfg", "[Service]\nPort=9000\n")
    assert update_kd_ini_file(cfg, "Service", "Host", "localhost") is True
    text = cfg.read_text(encoding="utf-8")
    assert "[Service]" in text
    assert "Host=localhost" in text
    assert "Port=9000" in text


def test_create_missing_section(tmp_path: Path):
    cfg = _write_cfg(tmp_path / "c.cfg", "[Service]\nPort=9000\n")
    assert update_kd_ini_file(cfg, "License", "LicenseServerHost", "lic1") is True
    text = cfg.read_text(encoding="utf-8")
    assert "[License]" in text
    assert "LicenseServerHost=lic1" in text


def test_backup_created(tmp_path: Path):
    cfg = _write_cfg(tmp_path / "c.cfg", "[Service]\nPort=1\n")
    assert update_kd_ini_file(cfg, "Service", "Port", "2") is True
    backups = list(tmp_path.glob("c.cfg.bak-*"))
    assert len(backups) == 1
    assert "Port=1" in backups[0].read_text(encoding="utf-8")


def test_no_backup_flag(tmp_path: Path):
    cfg = _write_cfg(tmp_path / "c.cfg", "[Service]\nPort=1\n")
    assert update_kd_ini_file(cfg, "Service", "Port", "2", no_backup=True) is True
    assert list(tmp_path.glob("c.cfg.bak-*")) == []


def test_missing_file_returns_false(tmp_path: Path):
    assert update_kd_ini_file(tmp_path / "missing.cfg", "S", "K", "V") is False


def test_regex_metacharacters_in_key_do_not_corrupt(tmp_path: Path):
    """Original PS bug: unescaped Section/Key broke on metacharacters."""
    cfg = _write_cfg(
        tmp_path / "c.cfg",
        "[Service]\nHost.name=old\nPort=9000\n",
    )
    # Key contains a dot – must still match and replace correctly
    assert update_kd_ini_file(cfg, "Service", "Host.name", "new") is True
    text = cfg.read_text(encoding="utf-8")
    assert "Host.name=new" in text
    assert "Host.name=old" not in text


def test_restore_from_backup(tmp_path: Path):
    cfg = _write_cfg(tmp_path / "c.cfg", "[Service]\nPort=1\n")
    backup = backup_kd_file(cfg)
    assert backup is not None
    cfg.write_text("[Service]\nPort=999\n", encoding="utf-8")
    assert restore_kd_file_from_backup(cfg, backup) is True
    assert "Port=1" in cfg.read_text(encoding="utf-8")


def test_license_section_not_glued_to_value(tmp_path: Path):
    """Regression: never produce LicenseServerHost=localhost[License]."""
    cfg = _write_cfg(tmp_path / "c.cfg", "[Service]\nPort=9000\n")
    assert update_kd_ini_file(cfg, "License", "LicenseServerHost", "localhost") is True
    assert update_kd_ini_file(cfg, "License", "Clients", "*.*.*.*") is True
    text = cfg.read_text(encoding="utf-8")
    assert "localhost[License]" not in text
    assert "localhost[Service]" not in text
    assert "*.*.*.*[License]" not in text
    assert re.search(r"(?m)^LicenseServerHost=localhost\s*$", text)
    assert re.search(r"(?m)^Clients=\*\.\*\.\*\.\*\s*$", text)
    assert text.count("[License]") == 1


def test_key_update_is_section_scoped(tmp_path: Path):
    """Same key name in another section must not be overwritten."""
    cfg = _write_cfg(
        tmp_path / "c.cfg",
        "[Server]\nLicenseServerHost=keep-me\n[Service]\nPort=1\n",
    )
    assert update_kd_ini_file(cfg, "License", "LicenseServerHost", "localhost") is True
    text = cfg.read_text(encoding="utf-8")
    assert "LicenseServerHost=keep-me" in text
    assert "LicenseServerHost=localhost" in text
    # keep-me stays under [Server]; localhost under [License]
    server_block = text.split("[Service]")[0]
    assert "LicenseServerHost=keep-me" in server_block
    assert "[License]" in text


def test_idol_common_cfg_two_keys(tmp_path: Path):
    """Mirrors installer: LicenseServerHost then Clients on idol.common.cfg."""
    cfg = _write_cfg(tmp_path / "idol.common.cfg", "[Logging]\nLevel=info\n")
    assert update_kd_ini_file(cfg, "License", "LicenseServerHost", "licenseserver") is True
    assert update_kd_ini_file(cfg, "License", "Clients", "*.*.*.*") is True
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[License]") == 1
    assert "LicenseServerHost=licenseserver" in text
    assert "Clients=*.*.*.*" in text
    assert "licenseserver[License]" not in text
    assert "licenseserver[Clients]" not in text


def test_repairs_already_glued_value_section(tmp_path: Path):
    """If a previous broken run glued value+[Section], rewrite separates them."""
    cfg = tmp_path / "broken.cfg"
    cfg.write_text(
        "[Service]\r\nPort=9000\r\nLicenseServerHost=localhost[License]\r\n",
        encoding="utf-8",
        newline="",
    )
    # Updating any key triggers the safety-net rewrite on save
    assert update_kd_ini_file(cfg, "Service", "Port", "9001") is True
    text = cfg.read_text(encoding="utf-8")
    assert "localhost[License]" not in text
    assert "Port=9001" in text


def test_preserves_crlf(tmp_path: Path):
    cfg = _write_cfg(tmp_path / "c.cfg", "[Service]\nPort=1\n")
    assert update_kd_ini_file(cfg, "Service", "Port", "2") is True
    raw = cfg.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\r\n") >= 2
