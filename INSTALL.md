# INSTALL.md — Linux environment

> Converted from the Windows toolkit. See README.md for the full guide and the Linux port note.

## Prerequisites

- Linux (x86_64 recommended)
- bash, python3 (≥ 3.8), pip, unzip
- root / sudo privileges for service and system changes
- KD component ZIP packages (Linux builds preferred; Windows packages may be rejected by backend checks)
- At least 4 GB RAM and 10 GB free disk on the install volume
- **Official Knowledge Discovery library requirements**
  - Minimum symbols: `GLIBC_2.34`, `GLIBCXX_3.4.30`, `GCC_12.2`
  - The KD installer ships matching `libgcc_s` / `libstdc++` under `InstallDir/common` and `InstallDir/common/runtimes`. When starting components from the command line (instead of systemd), set `LD_LIBRARY_PATH` to include those directories, or copy the shared libraries into the component working directory.
- **WKOOP packages** (required by Web Connector, NiFi Ingest HTML processors, Connector Framework Server, File Content Extraction, PDF Export, View, …). `./setup.sh` / the installer install them automatically when possible:
  - **RHEL 8**: `libatomic libX11 libX11-xcb libXtst libXScrnSaver libXcomposite atk at-spi2-core at-spi2-atk cups cairo pango alsa-lib alsa-lib-devel`
  - **Debian / Ubuntu**: `libatomic1 libx11-6 libx11-xcb1 libxcursor1 libxdamage1 libxrandr2 libxtst6 libxss1 libxcomposite1 libatk1.0-0 (or libatk1.0-0t64) at-spi2-core libatk-bridge2.0-0 libcups2 libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libpciaccess0` (plus common Chromium runtime libs). The setup script installs the correct names for your release.
  - **SLES 15**: `libatomic1 libX11-6 libXtst6 libXss1 libXcomposite1 at-spi2-core cups libcairo2 libpci3`
- **Java (per component)** – the installer auto-installs Temurin JDK 21 when needed for NiFi/Find:
  - Find / Data Admin / Site Admin → JRE 17 or 21
  - NiFi Ingest → JRE 21
  - Documentum / FileNet / Hadoop Connectors → JRE 11+
  - Named Entity Recognition Java SDK → JDK 8 or 11
  - View (non-Windows) → JRE 8–17
  - MMAP → JRE 8 or 11

## 1. Get the files

Clone or copy this toolkit to the target machine, e.g.:

```bash
git clone https://github.com/oattia-ot/idol-linux-setup.git
cd idol-linux-setup
```

## 2. Prep the environment (run once)

```bash
sudo ./setup.sh
# or
sudo ./initialize-environment.sh
```

This checks Python, creates a local virtual environment under `./env` (PEP 668 safe on Ubuntu 24.04+/Debian), installs requirements into it, and verifies common tools (unzip, tar, openssl). All entry-point scripts (`install-kd.sh`, `install-kd-menu.sh`, …) automatically prefer the venv interpreter when present.

## 3. Configure

```bash
./config/ui-config/start-kd-config-dashboard.sh
```

Open http://127.0.0.1:5000, set BasePath (e.g. `/opt/KnowledgeDiscovery/26.2`), ZipPath, components, ports, then **Export JSON** → `config/my-config.json`.

## 4. Install

```bash
sudo ./install-kd.sh --mode Install --non-interactive --config config/my-config.json
```

Other modes: `Uninstall`, `Configure`, `Repair`, `Upgrade` (see `./install-kd.sh --help` / `python3 install_kd.py --help`).

## 5. Services

```bash
./status-all-kdservices.sh
sudo ./start-all-kdservices.sh
sudo ./stop-all-kdservices.sh
```

**Note:** Services are native systemd units (`kd-*.service`). The Python service manager uses `systemctl` (with an `_sc()` compatibility shim that preserves the original Windows-style return values for callers).

## 6. SSL

```bash
./generate-ssl.sh --auto
# or
python3 tools/generate_ssl.py --auto
```

## 7. Cleanup

```bash
sudo ./cleanup-kd.sh --basepath /opt/KnowledgeDiscovery/26.2
# or
sudo ./install-kd.sh --mode Uninstall --non-interactive --config config/my-config.json
```

For more detail see [README.md](README.md).
