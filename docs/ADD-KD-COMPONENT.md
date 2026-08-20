# How to Manually Add a New IDOL / KD Component

Simple step-by-step guide for the **idol-linux-setup** toolkit.

---

## 1. Know your component type

| Type | Examples | Binary | Service method |
|------|----------|--------|----------------|
| **Native** | View, DAH, DIH, Content, QMS | `.exe` with `-install` | Built-in service (systemd or equivalent) |
| **Agentstore-like** | Agentstore, QMSAgentStore | `agentstore.exe` (copy of `content.exe`) | Built-in service (systemd or equivalent) |
| **Java** | Find, NiFi | `.war` / `.jar` / `java -jar` | systemd (or nifi.sh) wrapper |

Most IDOL engines (View, QMS, Content, etc.) are **Native**.  
**Agentstore-like** components have **no own ZIP** — they are assembled from the Content package.  
Find and NiFi are **Java**.

---

## 2. Put the ZIP in the zip folder

Copy the component package to:

```text
/opt/KD-Setup\zip-folder\
```

Examples of good names:

```text
view-26.2.0-windows.zip
qms-26.2.0-WIN64.zip
find-26.2.0.zip
content-26.2.0-windows.zip
```

The installer looks for `*<ComponentName>*.zip` (case-insensitive).

**Exception:** Agentstore-like components (Agentstore, QMSAgentStore) do **not** need their own ZIP. They reuse the **Content** package.

---

## 3. Edit the JSON config

Edit **both** files (keep them in sync):

- `config/default-config.json`
- `config/my-config.json`

### 3.1 Add to Components list

```json
"Components": [
  "LicenseServer",
  "Content",
  "Community",
  "Agentstore",
  "QMSAgentStore",
  "Category",
  "QMS",
  "View",
  "NiFi",
  "Find"
]
```

Use the **exact** name you want for the folder and service suffix  
(e.g. `"View"` → folder `View\`, service `KD-View`).

Put **Content** before any Agentstore-like component so Content is extracted first.

### 3.2 Add a port

```json
"Ports": {
  "Content": "9100",
  "Category": "9020",
  "Community": "9030",
  "Agentstore": "9050",
  "QMSAgentStore": "9150",
  "QMS": "16000",
  "View": "9080",
  "LicenseServer": "20000",
  "NiFi": "8443",
  "Find": "8080"
}
```

Use the port from the product docs or the sample `.cfg`.

### 3.3 Optional block (Java components only)

For Find-style apps:

```json
"Find": {
  "HeapXms": "1g",
  "HeapXmx": "2g",
  "ServerPort": "8080",
  "HomeDir": "home",
  "InstallService": true
}
```

Native and Agentstore-like components usually **do not** need this block.

---

## 4. Native component — config templates

### 4.1 Create the template folder

```text
config/cfg\view\
```

### 4.2 Add the main config file

```text
config/cfg\view\view.cfg
```

Copy a working `view.cfg` from a manual install or from the extracted ZIP.  
Set at least:

- `[Server] Port=` → same as `Ports.View` in JSON  
- License host / port (or leave for the installer to patch)

Optional:

```text
config/cfg\view\idol.common.cfg
```

### 4.3 Register the template in code

**File:** `kd\installer.py`

Find:

```python
_CFG_TEMPLATE_COMPONENTS = frozenset(
    {"content", "community", "agentstore", "category", "qms", "qmsagentstore"}
)
```

Add your lowercase name, e.g. `"view"`:

```python
_CFG_TEMPLATE_COMPONENTS = frozenset(
    {"content", "community", "agentstore", "category", "qms", "qmsagentstore", "view"}
)
```

On install, the toolkit copies these files into:

```text
/opt/KnowledgeDiscovery\26.2\View\
```

---

## 5. Agentstore-like component (copy of Content)

Use this path when the component is **not** a separate product ZIP, but an Agentstore engine built from the **Content** package (same binary, different ports and config).

Examples already in the toolkit:

| Component | Folder | ACI port | Service |
|-----------|--------|----------|---------|
| **Agentstore** | `Agentstore\` | 9050 | `KD-Agentstore` |
| **QMSAgentStore** | `QMSAgentStore\` | 9150 | `KD-QMSAgentStore` |

QMS points at QMSAgentStore for promotion rules:

```ini
[PromotionAgentStore]
Host=localhost
Port=9150
```

### 5.1 What the installer does automatically

1. Ensures **Content** is extracted under `BasePath\Content\`
2. Copies the whole Content tree into `BasePath\<YourName>\`
3. Creates `agentstore.exe` from `content.exe` (if missing)
4. Overwrites root `.cfg` files from `config/cfg\<yourname>\`
5. Patches `[Server] Port` and `[Index] IndexPath` from JSON

### 5.2 Steps to add another Agentstore-like engine (e.g. AnswerBankAgentStore)

**A. JSON** — add to `Components` and `Ports` in both config files:

```json
"Components": [ ..., "AnswerBankAgentStore", ... ],
"Ports": {
  ...
  "AnswerBankAgentStore": "9450"
}
```

**B. Templates** — create:

```text
config/cfg\answerbankagentstore\
  agentstore.cfg      ← copy from config/cfg\agentstore\agentstore.cfg
  idol.common.cfg
```

In `agentstore.cfg` set ports (convention: ACI / Index / Service = N / N+1 / N+2):

```ini
[Service]
ServicePort=9452

[Server]
Port=9450
IndexPort=9451
```

Keep the main file named **`agentstore.cfg`** (not `answerbankagentstore.cfg`). The binary is always `agentstore.exe`.

**C. Code** — `kd\installer.py`:

1. Add to template set (lowercase folder name):

```python
_CFG_TEMPLATE_COMPONENTS = frozenset(
    {..., "answerbankagentstore"}
)
```

2. Add to the Agentstore-like set (exact JSON name):

```python
_AGENTSTORE_LIKE = frozenset({"Agentstore", "QMSAgentStore", "AnswerBankAgentStore"})
```

That single set drives:

- No ZIP lookup (reuses Content)
- Post-extract copy + `agentstore.exe` creation
- Template deploy during assembly
- Skip re-deploy in the Configure stage
- IndexPath patching

**D. Discovery** — `kd\discovery.py`:

```python
search_component = (
    "Content" if component in ("Agentstore", "QMSAgentStore", "AnswerBankAgentStore")
    else component
)
```

Executable scoring already prefers `agentstore.exe` for names ending in / equal to agentstore-style components when listed there; extend the condition if needed.

**E. Browser URLs** — `install_kd.py`:

```python
("AnswerBankAgentStore", ("AnswerBankAgentStore", "answerbankagentstore"), "9450"),
```

**F. Cleanup** — `config/cleanup.json`:

```json
"KD-AnswerBankAgentStore"
```

### 5.3 Checklist (Agentstore-like)

- [ ] `"YourName"` in `Components` (both JSON files) — **after** Content
- [ ] `"YourName": "<port>"` in `Ports`
- [ ] `config/cfg\<yournamelowercase>\agentstore.cfg` + `idol.common.cfg` (ports set)
- [ ] `"yournamelowercase"` in `_CFG_TEMPLATE_COMPONENTS`
- [ ] `"YourName"` in `_AGENTSTORE_LIKE`
- [ ] `"YourName"` in discovery `search_component` Content list
- [ ] Browser URL row in `install_kd.py`
- [ ] `"KD-YourName"` in `cleanup.json`
- [ ] Content ZIP present under `ZipPath` (no separate ZIP for this component)
- [ ] Install as root (sudo)

---

## 6. Native component — discovery (usually automatic)

`kd\discovery.py` already finds:

- ZIP: `*View*.zip`
- EXE: `view.exe` (ranked; tool utilities are ignored)

**Only change discovery if:**

- The ZIP name does **not** contain the component name, or  
- The main binary is not named like `view.exe`

Then add a special case similar to the existing `find` / `nifi` blocks.

Agentstore-like components are covered in **section 5** (they reuse Content).

---

## 7. Java component — extra code (Find-style only)

Skip this section for native engines like View / QMS and for Agentstore-like engines.

| Step | File | Action |
|------|------|--------|
| 1 | `kd\discovery.py` | Detect `*name*.zip` and the `.war` / `.jar` |
| 2 | `kd\service_manager.py` | Add `install_kd_<name>_service()` (copy from `install_kd_find_service`) |
| 3 | `kd\installer.py` | In Configure + Service install stages, add `elif component.lower() == "<name>"` |
| 4 | `config/cfg\<name>\` | Optional `config.json` or properties |

Do **not** run `something.war -install` — that causes `WinError 193`. Use systemd (or nifi.sh) + `java -jar`.

---

## 8. Browser URL summary

**File:** `install_kd.py`

Find the **BROWSER URLS** section. Native ACI components are listed like this:

```python
for label, keys, default_port in (
    ("LicenseServer", ("LicenseServer", "licenseserver"), "20000"),
    ("Content", ("Content", "content"), "9100"),
    ("Community", ("Community", "community"), "9030"),
    ("Category", ("Category", "category"), "9020"),
    ("Agentstore", ("Agentstore", "agentstore"), "9050"),
    ("QMSAgentStore", ("QMSAgentStore", "qmsagentstore"), "9150"),
    ("QMS", ("QMS", "qms"), "16000"),
    ("View", ("View", "view"), "9080"),
):
```

Add your component:

```python
    ("View", ("View", "view"), "9080"),
```

Without this line, the install summary will **not** show the new URL even if the component is installed.

Java UIs (Find / NiFi) have their own `if` blocks below that loop.

---

## 9. Cleanup list

**File:** `config/cleanup.json`

Add the service (systemd or equivalent) name:

```json
"LinuxServices": [
  "KD-LicenseServer",
  "KD-Content",
  "KD-Community",
  "KD-Agentstore",
  "KD-QMSAgentStore",
  "KD-Category",
  "KD-QMS",
  "KD-View",
  "KD-NiFi",
  "KD-Find"
]
```

Service name pattern: **`KD-` + component name** → `KD-View`, `KD-QMSAgentStore`.

---

## 10. Install and verify

```bash
# 1. Confirm ZIP is present (native / Java only)
dir /opt/KD-Setup\zip-folder\*view*
dir /opt/KD-Setup\zip-folder\*qms*
dir /opt/KD-Setup\zip-folder\*content*

# 2. Run install (root (sudo))
cd /opt/KD-Setup\idol-linux-setup
./install-kd.sh -Mode Install

# 3. Or create services only
.\manage-kdservices.sh
# choose: Create all services
```

Check:

```bash
Get-Service KD-View
Get-Service KD-QMS
Get-Service KD-QMSAgentStore
systemctl (or service manager) qc KD-View
dir /opt/KnowledgeDiscovery\26.2\View
dir /opt/KnowledgeDiscovery\26.2\QMSAgentStore
```

Open the admin URL (if listed):

```text
http://localhost:9080/action=admin
http://localhost:16000/action=admin
http://localhost:9150/action=admin
```

---

## 11. Checklist (copy/paste)

### Native (e.g. View or QMS)

- [ ] ZIP in `/opt/KD-Setup\zip-folder\*View*.zip` (or `*QMS*.zip`)
- [ ] `"View"` / `"QMS"` in `Components` (`default-config.json` + `my-config.json`)
- [ ] Port in `Ports` (e.g. `"View": "9080"`, `"QMS": "16000"`)
- [ ] `config/cfg\view\view.cfg` or `config/cfg\qms\qms.cfg` created
- [ ] Lowercase name added to `_CFG_TEMPLATE_COMPONENTS` in `kd\installer.py`
- [ ] Browser URL row in `install_kd.py`
- [ ] `"KD-View"` / `"KD-QMS"` in `config/cleanup.json` → `LinuxServices`
- [ ] Install run as root (sudo)
- [ ] `Get-Service KD-View` (or `KD-QMS`) shows the service

### Agentstore-like (e.g. QMSAgentStore)

- [ ] See **section 5.3** checklist above
- [ ] Content ZIP present (no separate ZIP for this component)
- [ ] `"YourName"` in `_AGENTSTORE_LIKE`
- [ ] Templates under `config/cfg\<name>\` with **`agentstore.cfg`** and correct ports

### Java (e.g. new Find-like app)

- [ ] All native checklist items that apply (Components, Ports, cleanup)
- [ ] Discovery special-case for ZIP + launcher
- [ ] systemd (or nifi.sh) install function in `service_manager.py`
- [ ] Configure + service branches in `installer.py`
- [ ] Optional home `config.json` under `config/cfg\<name>\`
- [ ] Browser URL `if` block if it has a web UI

---

## Common problems

| Problem | Cause | Fix |
|---------|--------|-----|
| `No file matching *View*.zip` | ZIP name does not contain the component name | Rename ZIP or fix discovery |
| Agentstore-like fails extract | Content not extracted yet / missing Content ZIP | Put Content first in `Components`; ensure `*Content*.zip` exists |
| `WinError 193` | Treating a `.war` as a native `.exe` | Use systemd (or nifi.sh) / Java path |
| Service missing after Install | Resume skipped service create | Manage → Create all, or clear state |
| URL not in summary | Component not in BROWSER URLS list | Add row in `install_kd.py` |
| Wrong port | JSON port ≠ `.cfg` port | Align `Ports` and template `.cfg` |
| Service left after uninstall | Not in `cleanup.json` | Add `KD-<Name>` to `LinuxServices` |
| Wrong binary for Agentstore-like | Missing `agentstore.exe` copy step | Ensure name is in `_AGENTSTORE_LIKE` |

---

## Quick reference — files to touch

| File | Native | Agentstore-like | Java |
|------|--------|-----------------|------|
| `config/default-config.json` | Yes | Yes | Yes |
| `config/my-config.json` | Yes | Yes | Yes |
| `config/cfg\<name>\*.cfg` | Yes | Yes (`agentstore.cfg`) | Optional |
| `config/cleanup.json` | Yes | Yes | Yes |
| `kd\installer.py` (`_CFG_TEMPLATE_COMPONENTS`) | Yes | Yes | Yes (+ branches) |
| `kd\installer.py` (`_AGENTSTORE_LIKE`) | No | **Yes** | No |
| `kd\discovery.py` | Rarely | Yes (Content reuse list) | Yes |
| `kd\service_manager.py` | No | No | Yes |
| `install_kd.py` (BROWSER URLS) | Yes | Yes | Yes |

---

## Example: adding View (native) in 5 minutes

1. Copy `view-*.zip` → `/opt/KD-Setup\zip-folder\`  
2. Add `"View"` to `Components` and `"View": "9080"` to `Ports` in both JSON files  
3. Create `config/cfg\view\view.cfg` and add `"view"` to `_CFG_TEMPLATE_COMPONENTS`  
4. Add View to BROWSER URLS and `"KD-View"` to `cleanup.json`  
5. Run `./install-kd.sh -Mode Install`

Done.

---

## Example: adding QMSAgentStore (Agentstore-like) in 5 minutes

1. Ensure `*Content*.zip` is in `/opt/KD-Setup\zip-folder\` (no separate QMSAgentStore ZIP)  
2. Add `"QMSAgentStore"` to `Components` (after Content) and `"QMSAgentStore": "9150"` to `Ports`  
3. Create `config/cfg\qmsagentstore\agentstore.cfg` (ports 9150/9151/9152) + `idol.common.cfg`  
4. Add `"qmsagentstore"` to `_CFG_TEMPLATE_COMPONENTS` and `"QMSAgentStore"` to `_AGENTSTORE_LIKE`  
5. Add discovery Content-reuse entry, browser URL, and `"KD-QMSAgentStore"` to cleanup  
6. Run `./install-kd.sh -Mode Install`

The installer copies Content → `QMSAgentStore\`, creates `agentstore.exe`, applies templates, and installs `KD-QMSAgentStore`.
