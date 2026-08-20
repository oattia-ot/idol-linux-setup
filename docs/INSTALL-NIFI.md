# Apache NiFi 2.x – Installation & Configuration (integrated with KD Installer)

**Last updated:** August 2026  
**NiFi Version covered:** 2.10.0 (and general 2.x)  
**Integration:** Optional component of the Knowledge Discovery Linux Installer (Python edition)

This guide covers installing Apache NiFi 2 on Linux as an optional component alongside OpenText Knowledge Discovery 26.2. NiFi is treated as a first-class component in the same `BasePath` and can be installed/configured via the existing installer.

---

## Architecture decisions (as configured in this toolkit)

| Decision | Choice |
|----------|--------|
| Install location | Same `BasePath` as KD components, e.g. `/opt/KnowledgeDiscovery\26.2\NiFi` |
| Service installation | **systemd (or nifi.sh)** via `nifi/setup-nifi-service.sh` → `nifi.sh run` (optional: `NiFi.InstallService`, default true) |
| Manual launcher | `BasePath\NiFi\bin\nifi.sh` (start/run/stop/status); service helper: `nifi/setup-nifi-service.sh` |
| JAVA_HOME + PATH | Permanent `JAVA_HOME` + `%JAVA_HOME%\bin` on machine PATH |
| Default heap profile | Heavy: `-Xms8g` / `-Xmx16g` (configurable) |
| Port | `8443` (HTTPS UI) – configurable via `Ports.NiFi` |
| Sensitive properties key | Configurable; strong password required |

> **Note**: The installer registers NiFi as service (systemd or equivalent) **`KD-NiFi`** using **systemd (or nifi.sh)**
> (Non-Sucking Service Manager). If systemd (or nifi.sh) isn't already available it is installed via
> **winget** (`winget install --id systemd (or nifi.sh).systemd (or nifi.sh) -e`); `nifi/systemd` (shipped with the
> toolkit), `PATH`, or a direct download from systemd.cc are used as fallbacks.
> Legacy service names `KD-Apache-NiFi` and `ApacheNifiService` are
> automatically removed if found so only one NiFi service remains. Manual helper:
> `nifi/setup-nifi-service.sh`.

---

## 1. Prerequisites

### 1.0.1 Install systemd (or nifi.sh) (if you don't already have it)

The installer does this automatically, but you can also do it yourself ahead of time:

```bash
winget install --id systemd (or nifi.sh).systemd (or nifi.sh) -e
```

Verify it's on PATH:

```bash
systemd version
```

If `winget` isn't available on the machine (older Linux builds, some
Server Core images), the installer falls back to the bundled `nifi/systemd`
or downloads the official systemd (or nifi.sh) 2.24 release from systemd.cc automatically.

### 1.0 One confirmation for all NiFi dependencies

When `NiFi` is in `Components` and you're running interactively, the installer
asks **once**, up front, before touching anything:

```
Apache NiFi may need the following installed automatically if missing:
  - Java 21 runtime (Eclipse Temurin JDK)
  - systemd (or nifi.sh) (service (systemd or equivalent) wrapper; bundled under nifi/systemd)
  - The NiFi binary package itself (if not already in ZipPath)
Install any missing NiFi dependencies now? (Y/N) [Y]:
```

- **Y** (default): proceeds and auto-installs whatever's missing, with no further
  prompts for the individual pieces.
- **N**: exits setup immediately (exit code 1) with instructions for how to
  proceed - install the dependencies yourself and re-run with
  `--non-interactive`, or remove `"NiFi"` from `Components` to install
  without it.
- `-NonInteractive` / `--non-interactive` skips this prompt and proceeds
  automatically (same as answering Y).

### 1.1 Java 21 (required by NiFi 2.x) - installed automatically

NiFi 2.x **requires Java 21**. The KD installer checks for it whenever `NiFi` is in the
`Components` list and, if it's missing, **downloads and silently installs Eclipse
Temurin JDK 21** and sets `JAVA_HOME` for you - no manual steps needed in the
common case.

- **Interactive runs**: you're prompted first (`Download and install Eclipse Temurin
  JDK 21 now? (Y/N) [Y]`) - default is Yes.
- **`-NonInteractive` runs**: it installs automatically without prompting.
- **To disable auto-install** (e.g. you manage Java via your own imaging/patching
  process) set `"AutoInstallJava": false` in the `NiFi` config section - the installer
  will then only warn and let you decide whether to continue.

What happens under the hood:
1. Downloads the official Temurin 21 MSI from `api.adoptium.net`.
2. Runs it silently: `msiexec /i ... /qn /norestart ADDLOCAL=FeatureMain,FeatureEnvironment,FeatureJavaHome`
   (the same official installer features Adoptium documents for setting `JAVA_HOME`
   and `PATH` machine-wide).
3. Locates the resulting install directory under `/opt\Eclipse Adoptium\jdk-21...`.
4. Sets `JAVA_HOME` machine-wide via `setx JAVA_HOME <path> /M` as a belt-and-braces
   fallback on top of the MSI's own `FeatureJavaHome`, and updates the current
   process's environment so the rest of the same installer run (and the
   `bootstrap.conf` / systemd (or nifi.sh) `AppEnvironmentExtra` `JAVA_HOME` propagation for the service) sees it immediately.

> **Permanent environment variables**: both `JAVA_HOME` and `NIFI_HOME` are
> written as **permanent, machine-wide Linux environment variables**
> (`setx <NAME> <value> /M`) when the `KD-NiFi` service is installed - the
> same place they'd appear if you set them by hand via *System Properties >
> Environment Variables*. This is in addition to (not instead of) the systemd (or nifi.sh)
> `AppEnvironmentExtra` setting used by the `KD-NiFi` service itself, so both
> the service and any interactive shell / other tooling on the box see
> consistent values. **If a variable is already set permanently, it is left
> untouched** - the installer only fills in what's missing, it never
> overwrites an existing value. A brand-new shell/session is required to
> pick up `setx` changes if one was already open before the install ran.

If you'd rather install Java yourself:

1. Download **Eclipse Temurin JDK 21** from [Adoptium](https://adoptium.net/) (recommended) or Oracle JDK 21.
2. Install it (example path: `/opt//opt\Eclipse Adoptium\jdk-21.x.x.x-hotspot`).
3. Set system environment variables:
   - `JAVA_HOME` → point to the JDK folder.
   - Add `%JAVA_HOME%\bin` to `Path`.
4. Verify in a **new** elevated Command Prompt:
   ```cmd
   java -version
   ```
   You should see `openjdk version "21.x.x"`.

The installer's check will then pass without needing to install anything.

### 1.2 NiFi binary ZIP

**Option A – Automatic download (recommended)**

When you include `"NiFi"` in the `Components` list and the installer cannot find a suitable ZIP in `ZipPath`, it automatically downloads the official binary (~800 MB) from the Apache CDN directly into your `ZipPath`. No second download prompt is required.

- Version is controlled by `NiFi.VersionHint` (default `"2.10.0"`).
- When the NiFi ZIP is missing from `ZipPath`, the installer offers a manual-copy option or automatic download. Automatic downloads use the fastest available transfer method (aria2c with parallel connections when installed, otherwise curl/streaming fallback) and resume partial downloads.
- Mirrors tried in order: `dlcdn.apache.org` → `downloads.apache.org` → `archive.apache.org`.

**Option B – Manual download**

1. Go to [https://nifi.apache.org/download/](https://nifi.apache.org/download/).
2. Download **`nifi-2.10.0-bin.zip`** (or the version matching `VersionHint`).
3. Place the ZIP in your `ZipPath` folder, e.g.:
   ```
   /opt/KD-Setup\zip-folder\nifi-2.10.0-bin.zip
   ```

The discovery logic looks for any `*nifi*-bin*.zip` (case-insensitive).

**Option C – Menu 07 after a partial install (or UI warning)**

If the NiFi ZIP is missing:

- During **01) Install**, the installer suggests **07) Install Java Services (NiFi|Find)** as the next step.
- In the **config dashboard** (menu **00**), the ZIP packages panel shows a dedicated **NiFi binary package missing** warning and tells you to run menu **07** from the CLI. Other components can still be exported.

That mode:

1. Shows the same dependency prompt as Install (Java 21 / systemd (or nifi.sh) / NiFi package)
2. Verifies the ZIP under `ZipPath` and downloads it if missing
3. Extracts under `BasePath\NiFi` if not already present
4. Registers and starts the `KD-NiFi` service (systemd or equivalent)

```bash
./install-kd.sh
# choose 07) Install Java Services (NiFi|Find)

# or non-interactive:
./install-kd.sh -Mode InstallJavaServices -NonInteractive
```

---

## 2. Configuration (config JSON)

Add `"NiFi"` to the `Components` array and supply the optional `NiFi` section:

```json
{
  "BasePath": "C:/KnowledgeDiscovery/26.2",
  "ZipPath": "C:/KD-Setup/zip-folder",
  "Components": [
    "LicenseServer", "Content", "Community", "Agentstore", "Category", "NiFi"
  ],
  "Ports": {
    "Content": "9000",
    "Category": "9020",
    "Community": "9030",
    "Agentstore": "9040",
    "LicenseServer": "20000",
    "NiFi": "8443"
  },
  "NiFi": {
    "HeapXms": "8g",
    "HeapXmx": "16g",
    "SensitivePropsKey": "MyStrongPassword123!",
    "WebHttpsPort": "8443",
    "Username": "admin",
    "Password": "ChangeMe-AdminPassword123!",
    "VersionHint": "2.10.0",
    "AutoDownload": true,
    "AutoInstallJava": true,
    "InstallService": true
  },
  "InstallService": true,
  "StartMode": "Auto"
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `NiFi.HeapXms` | `8g` | Initial heap (`java.arg.2`) |
| `NiFi.HeapXmx` | `16g` | Maximum heap (`java.arg.3`) |
| `NiFi.SensitivePropsKey` | (required) | Value written to `nifi.sensitive.props.key` |
| `NiFi.WebHttpsPort` | `8443` | HTTPS port for the UI |
| `NiFi.Username` | `admin` | Single-user UI login username (applied via `nifi.sh set-single-user-credentials`) |
| `NiFi.Password` | (required for fixed login) | Single-user UI login password. If empty, NiFi keeps its first-start random password. |
| `NiFi.VersionHint` | `2.10.0` | Version used for official download URL |
| `NiFi.AutoDownload` | `true` | Automatically download the NiFi ZIP when it is missing from `ZipPath` |
| `NiFi.AutoInstallJava` | `true` | If `true` (default), auto-install Eclipse Temurin JDK 21 and set `JAVA_HOME` (and `%JAVA_HOME%/bin` on PATH) when Java 21+ isn't detected. Set `false` to only warn. |
| `NiFi.InstallService` | `true` | If `true` (default) and global `InstallService` is true, register service (systemd or equivalent) **KD-NiFi** via systemd (or nifi.sh) by running `nifi/setup-nifi-service.sh` (`nifi.sh run`). Set `false` to extract/configure only; start later with `bin/nifi.sh start` or re-run `nifi/setup-nifi-service.sh`. |
| `Ports.NiFi` | `8443` | Used by validation / summary |

Environment overrides follow the usual `KD_*` pattern where applicable (e.g. `KD_NIFI_HEAPXMX`).

---

## 3. Install via the KD installer

```bash
# Dry-run first
./install-kd.sh -Mode Install -DryRun -ConfigPath config/my-config.json

# Full install (elevated)
./install-kd.sh -Mode Install -NonInteractive -ConfigPath config/my-config.json
```

Or pure Python:

```bash
python3 install_kd.py --mode Install --non-interactive --config config/my-config.json
```

What the installer does for the `NiFi` component:

1. **Extract** – Locates `*nifi*-bin*.zip` in `ZipPath` and calls `unzip-one.sh` (native `tar.exe`, strips the top-level `nifi-x.y.z` folder) into `BasePath\NiFi`.
2. **Configure**
   - Sets `nifi.sensitive.props.key` in `conf\nifi.properties`.
   - Sets `nifi.web.https.port` (and related host if needed).
   - Updates `conf\bootstrap.conf`:
     ```
     java.arg.2=-Xms8g
     java.arg.3=-Xmx16g
     ```
3. **Service** – Registers a service (systemd or equivalent) **`KD-NiFi`** via **systemd (or nifi.sh)**
   (installer runs `nifi/setup-nifi-service.sh -NonInteractive`):
   - systemd (or nifi.sh) is located in this order: bundled `nifi/systemd` → already on `PATH`
     → installed automatically via **`winget install --id systemd (or nifi.sh).systemd (or nifi.sh) -e`** →
     as a last resort, downloaded directly from the official systemd (or nifi.sh) 2.24 release.
     After a winget install, the installer verifies it with `systemd version`.
   - Any existing `KD-NiFi` registration is stopped and removed first.
   - Legacy alternate names **`KD-Apache-NiFi`** and **`ApacheNifiService`**
     are also removed if present, so only one NiFi service remains.
   - systemd (or nifi.sh) is configured as:
     - Application: `%COMSPEC%` (`cmd.exe`)
     - AppParameters: `/c nifi.sh run`  (relative; AppDirectory = bin)
     - AppDirectory: `<BasePath>/NiFi/bin`
     - AppEnvironmentExtra: `JAVA_HOME=...;NIFI_HOME=...`
     - Start: Automatic; stdout/stderr rotated under `NiFi/logs/systemd-*.log`
     - AppStopMethodConsole + AppKillProcessTree for clean shutdown
   - `JAVA_HOME` and `NIFI_HOME` are **also** set as permanent, machine-wide
     Linux environment variables (`setx <NAME> <value> /M`), so they show
     up under *System Properties > Environment Variables* and in any new
     shell, not just inside the systemd (or nifi.sh) service process. **Only if not already
     set** - an existing permanent value for either variable is left as-is.
   - Because systemd (or nifi.sh) implements the Linux Service Control Handler API, the
     resulting service works with `systemctl (or service manager)`, Services.msc, and
     `Start-Service`/`Stop-Service`.

   Manual fallback (same service name): `nifi/setup-nifi-service.sh -NifiHome "<BasePath>/NiFi" -NonInteractive`

4. **Start order** – NiFi is started after LicenseServer (same post-install phase as other components).

   The installer also performs a **Java 21+** pre-flight check when NiFi is requested (required by NiFi 2.x).

---

## 4. NiFi Connectors (NAR files)

Apache NiFi processors for KD ingestion ship as `.nar` connector files inside a
separate **NiFiIngest** package (`NiFiIngest_<version>_LINUX_X86_64.zip`),
not inside the NiFi binary itself. These need to land in
`BasePath/NiFi/extensions` before the `KD-NiFi` service starts, or NiFi won't
load the KD-specific processors.

### 4.1 Automatic extraction (recommended)

Place the NiFiIngest ZIP in your `ZipPath` folder alongside the other
component packages, e.g.:

```
/opt/kd-setup/zip-folder/NiFiIngest_26.3.0_LINUX_X86_64.zip
```

Then open the **config dashboard** (menu **00) Prerequisite**) and, in the
**ZIP packages** panel, wait for the `NiFiIngest` row to show **found**. As
soon as that happens, the dashboard's **NiFi Connectors** panel automatically:

1. Extracts every `*.nar` from the ZIP into `SetupPath\nifi\nifi-connectors` (**Source**).
2. No second manual step is needed to get them onto disk — this used to
   require pressing an **Extract from ZIP** button, but as of this version
   extraction runs on its own once the NiFiIngest ZIP is detected.

You'll see a toast confirming how many `.nar` files were extracted.

### 4.2 Staging into NiFi's extensions folder

Extraction only populates the **Source** folder
(`SetupPath\nifi\nifi-connectors`); NiFi itself loads connectors from the
**Target** folder (`BasePath\NiFi\extensions`). In the **NiFi Connectors**
panel:

- **Copy source → target** copies every `.nar` from Source into Target.
- **+** adds a single connector: the file-picker opens in the Source folder
  by default (falling back to Target if Source doesn't exist yet), and the
  chosen file is copied into Target.
- **−** on a row in the Target list removes that connector from
  `BasePath\NiFi\extensions`.
- Source and Target are listed side by side so you can quickly see which
  connectors have and haven't been staged into Target yet.

Do this **before** the `KD-NiFi` service first starts (or before a
restart) — NiFi only picks up new NARs under `extensions` on startup.

### 4.3 Manual placement

If you'd rather manage `.nar` files by hand, just copy them directly into
`BasePath\NiFi\extensions` (create the folder if it doesn't exist) and
(re)start the `KD-NiFi` service. The dashboard's Target list will pick them
up on refresh.

---

## 5. First-start credentials

### Preferred: set fixed credentials in config JSON

Add `Username` / `Password` under the `NiFi` section (already present in
`config/default-config.json` and `config/my-config.json`):

```json
"NiFi": {
  "Username": "admin",
  "Password": "ChangeMe-AdminPassword123!",
  ...
}
```

During **Install** and **Configure**, the installer runs:

```cmd
nifi.sh set-single-user-credentials <Username> <Password>
```

so the UI login is known and stable (no need to scrape the log).

Then open:

```
https://localhost:8443/nifi
```

(Accept the self-signed certificate warning if using the default cert.)

### Fallback: random first-start password

If `NiFi.Username` or `NiFi.Password` is empty, NiFi generates a random
single-user username/password on first start.

```cmd
cd /opt/KnowledgeDiscovery\26.2\NiFi\logs
findstr "Generated" nifi-app.log
```

Look for:

```
Generated Username [xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx]
Generated Password [xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx]
```

### Manual override

```cmd
cd /opt/KnowledgeDiscovery\26.2\NiFi\bin
nifi.sh set-single-user-credentials admin YourNewStrongPassword123
```

Restart the service afterwards.

---

## 6. Service management

Manual start without the service (after install/configure):

```cmd
cd /opt/KnowledgeDiscovery\26.2\NiFi\bin
nifi.sh start
nifi.sh status
nifi.sh stop
```

The service (systemd or equivalent) (when `NiFi.InstallService` is true) uses systemd (or nifi.sh) with AppDirectory=`bin` and AppParameters=`/c nifi.sh run`.

```bash
# shell
Get-Service KD-NiFi
Start-Service KD-NiFi
Stop-Service KD-NiFi
Restart-Service KD-NiFi

# Or systemctl (or service manager) / systemd
sc query KD-NiFi
sc start KD-NiFi
sc stop KD-NiFi
systemd status KD-NiFi
systemd restart KD-NiFi
```

Logs (when installed via systemd (or nifi.sh)):

- `NiFi\logs\systemd-stdout.log` / `systemd-stderr.log`
- `NiFi\logs\nifi-app.log` / `nifi-bootstrap.log`

Uninstall **stops** `KD-NiFi` (and legacy `KD-Apache-NiFi` / `ApacheNifiService`) with `systemd stop`, then **removes** them with `systemd remove <name> confirm`, and deletes the `BasePath\NiFi` folder (same Phase 1-2-3 flow as other components).

**Scope of service management**: Install, Repair, and Uninstall only ever
touch service (systemd or equivalent)s for components listed in `Components` in your config
JSON. If `NiFi` isn't in `Components`, `KD-NiFi` is never installed, started,
stopped, or removed. Likewise, other `KD-*` services on the machine that
aren't in your config are left alone unless you explicitly set
`"CleanupLeftoverServices": true`.

**Repair/Uninstall service handling**: before touching any configured
service, the installer checks whether it's currently running. If it is, it's
stopped first, and only then does the uninstall/repair continue (deleting
the service registration and, for Uninstall, the component folder). This
applies to `KD-NiFi` the same way it applies to every other KD component.

---

## 7. Heap memory profiles

| Profile | Xms | Xmx | Typical use |
|---------|-----|-----|-------------|
| Light / Dev | 1g | 2g | Laptops / POCs |
| Normal | 4g | 8g | Small servers |
| **Heavy (default)** | **8g** | **16g** | Production / heavy flows |
| Extreme | 16g | 32g | Large clusters (leave 4 GB+ free for OS) |

Change via config and re-run Configure mode, or edit `conf\bootstrap.conf` manually and restart.

Best practice: keep `-Xms` and `-Xmx` identical for stable performance. Leave at least 2–4 GB free for Linux + NiFi repositories.

---

## 8. Manual / advanced steps (if not using the installer)

If you prefer to install NiFi outside the KD component model, follow the original Apache guide:

1. Extract to a path **without spaces** (e.g. `/opt/nifi\nifi-2.10.0`).
2. Set `nifi.sensitive.props.key`.
3. Adjust heap in `bootstrap.conf`.
4. Install systemd (or nifi.sh) (if you don't already have it):

   ```bash
   winget install --id systemd (or nifi.sh).systemd (or nifi.sh) -e
   ```

   Verify it's on PATH:

   ```bash
   systemd version
   ```

5. Make `NIFI_HOME` and `JAVA_HOME` permanent machine environment variables
   (so they persist across reboots and are visible to every user/session,
   not just the systemd (or nifi.sh) service):

   ```bash
   setx NIFI_HOME "/opt/KnowledgeDiscovery\26.2\NiFi" /M
   setx JAVA_HOME "/opt//opt\Eclipse Adoptium\jdk-21.0.x" /M
   ```

   Open a **new** elevated shell afterwards for `setx` changes to be visible.

6. For a robust service (systemd or equivalent), use **systemd (or nifi.sh)** (same approach as the installer):

   ```bash
   # Preferred: toolkit helper (service name KD-NiFi; removes legacy KD-Apache-NiFi / ApacheNifiService)
   .\nifi\setup-nifi-service.sh -NifiHome "/opt/KnowledgeDiscovery\26.2\NiFi" -NonInteractive -Start

   # Or manually with systemd:
   systemd install KD-NiFi /opt/Linux\System32\cmd.exe
   systemd set KD-NiFi AppDirectory /opt/KnowledgeDiscovery\26.2\NiFi\bin
   systemd set KD-NiFi AppParameters "/c nifi.sh run"
   systemd set KD-NiFi AppEnvironmentExtra JAVA_HOME=/opt//opt\Eclipse Adoptium\jdk-21.0.x NIFI_HOME=/opt/KnowledgeDiscovery\26.2\NiFi
   systemd set KD-NiFi DisplayName "OpenText KD Apache NiFi"
   systemd set KD-NiFi Start SERVICE_AUTO_START
   systemd start KD-NiFi
   ```

---

## 9. Quick reference

```cmd
# Manual start/stop (when not using the service)
cd /opt/KnowledgeDiscovery\26.2\NiFi\bin
nifi.sh start
nifi.sh stop
nifi.sh status
nifi.sh run

# Change credentials
nifi.sh set-single-user-credentials admin MyPassword123

# Service (installer-created name)
sc start KD-NiFi
sc stop KD-NiFi
```

- Default UI: `https://localhost:8443/nifi`
- Official docs: https://nifi.apache.org/
- NiFi 2.x requires **Java 21**
- Prefer installation paths **without spaces**

---

**End of NiFi guide**
