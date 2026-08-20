"""
Shared fixtures for KD installer unit tests.
All tests are pure-Python and run on Linux or Windows without admin rights.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure the package root is on sys.path when tests are run from any cwd
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_config_file(tmp_path: Path) -> Path:
    """Write a minimal valid default-config.json into a temp dir."""
    cfg = {
        "BasePath": str(tmp_path / "KD"),
        "ZipPath": str(tmp_path / "zips"),
        "LicenseHost": "licenseserver.example",
        "LicensePort": "20000",
        "LicenseKeyPath": str(tmp_path / "licensekey.dat"),
        "ThisHost": "host1.example",
        "IndexPath": str(tmp_path / "indexes"),
        "Components": ["LicenseServer", "Content", "Agentstore"],
        "InstallService": True,
        "StartMode": "Auto",
        "Ports": {
            "Content": "9100",
            "LicenseServer": "20000",
            "Agentstore": "9050",
        },
        "ServiceInstallArgsTemplate": [
            "-install",
            "-servicename",
            "{ServiceName}",
            "-displayname",
            "{DisplayName}",
        ],
        "ServiceUninstallArgsTemplate": ["-remove", "-servicename", "{ServiceName}"],
    }
    path = tmp_path / "config" / "test-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def sample_zip_dir(tmp_path: Path) -> Path:
    """Create a fake ZIP folder with Windows- and Linux-named packages."""
    zdir = tmp_path / "zips"
    zdir.mkdir()
    # Minimal valid ZIP (empty archive is enough for discovery tests)
    import zipfile

    for name in (
        "LicenseServer_26.2.0_WINDOWS_X86_64.zip",
        "Content_26.2.0_WINDOWS_X86_64.zip",
        "Community_26.2.0_LINUX_X86_64.zip",  # should be rejected when Windows preferred
        "Category_26.2.0.zip",  # unmarked – acceptable fallback
    ):
        with zipfile.ZipFile(zdir / name, "w") as zf:
            zf.writestr("dummy.txt", "test")
    return zdir


@pytest.fixture
def sample_component_tree(tmp_path: Path) -> Path:
    """
    Create a realistic-ish component folder layout with several .exe files
    so ranking / exclusion logic can be exercised.
    """
    root = tmp_path / "Content"
    (root / "tools").mkdir(parents=True)
    (root / "langfiles").mkdir()
    (root / "filters").mkdir()

    def write_exe(path: Path, size: int = 100) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # MZ header so Test-KDWindowsExecutable passes
        path.write_bytes(b"MZ" + b"\x00" * (size - 2))

    write_exe(root / "content.exe", 5000)  # the real one
    write_exe(root / "tools" / "makeda.exe", 200)
    write_exe(root / "langfiles" / "jpn.exe", 150)
    write_exe(root / "filters" / "extract.exe", 180)
    write_exe(root / "vcredist_x64.exe", 300)  # should be excluded
    write_exe(root / "setup.exe", 250)  # should be excluded
    write_exe(root / "content.cfg", 50)  # not an exe – ignore
    # Also drop a cfg so configure tests can find it
    (root / "content.cfg").write_text("[Service]\nPort=9100\n", encoding="utf-8")
    return root
