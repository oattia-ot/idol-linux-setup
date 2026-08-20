"""Unit tests for pure helpers in kd.service_manager (no real services)."""

from __future__ import annotations

from kd.service_manager import expand_kd_args_template, get_kd_service_name


def test_service_name():
    assert get_kd_service_name("Content") == "KD-Content"
    assert get_kd_service_name("LicenseServer") == "KD-LicenseServer"


def test_expand_template_basic():
    template = ["-install", "-servicename", "{ServiceName}", "-displayname", "{DisplayName}"]
    values = {
        "ServiceName": "KD-Content",
        "DisplayName": "OpenText KD Content",
        "StartMode": "Auto",
    }
    result = expand_kd_args_template(template, values)
    assert result == [
        "-install",
        "-servicename",
        "KD-Content",
        "-displayname",
        "OpenText KD Content",
    ]


def test_expand_template_with_start_mode():
    template = ["-install", "-start", "{StartMode}", "-servicename", "{ServiceName}"]
    values = {"ServiceName": "KD-X", "StartMode": "Auto", "DisplayName": "X"}
    result = expand_kd_args_template(template, values)
    assert "-start" in result
    assert "Auto" in result
    assert "KD-X" in result


def test_expand_template_unknown_placeholder_left_alone():
    template = ["-foo", "{Unknown}"]
    result = expand_kd_args_template(template, {"ServiceName": "KD-A"})
    assert result == ["-foo", "{Unknown}"]
