# Push-Version — Setup & Usage Guide

Simple guide to write `version.txt`, commit the full toolkit root, and push to GitHub.

**GitHub repo:** https://github.com/oattia-ot/idol-linux-setup.git

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|--------|
| **Git for Linux** | Must be on `PATH`. Check: `git --version` |
| **shell** | Linux shell 5.1 or shell 7+ |
| **GitHub access** | Account with push rights to the repo (PAT, Credential Manager, or SSH) |

Optional but recommended once per machine:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

If these are missing, the script sets a **local** identity (`KD Setup` / `kd-setup@local`) for the repo only.

---

## 2. Place the scripts

Copy both files into a **subfolder** of the toolkit root (for example `config/` or `tools/`):

```text
/opt/KD-Setup\kd-win-setup-python\          ← repo root (version.txt + .git live here)
├── config/
│   ├── push-version.sh
│   ├── push-version.sh
│   └── PUSH-VERSION.md                   ← this file
├── tools/                                ← also OK to put scripts here
├── version.txt                           ← created by the script
└── .git\                                 ← created by the script (hidden folder)
```

The script always treats **one level up** from its own folder as the repo root.

---

## 3. Set the version

Use **one** of these (first match wins):

| Priority | How | Example |
|----------|-----|---------|
| 1 | Argument `-Version` | `.\push-version.sh -Version ver-0.6r42m0` |
| 2 | Env var `KD_VERSION` | `$env:KD_VERSION = "ver-0.6r42m0"` |
| 3 | Env var `VERSION` | `$env:VERSION = "ver-0.6r42m0"` |

### shell

```bash
cd /opt/KD-Setup\kd-win-setup-python\tools

# Option A — argument
.\push-version.sh -Version ver-0.6r42m0

# Option B — environment variable
$env:KD_VERSION = "ver-0.6r42m0"
.\push-version.sh
```

### CMD / Batch

```bat
cd /opt/KD-Setup\kd-win-setup-python\tools

REM Option A — argument
push-version.sh ver-0.6r42m0

REM Option B — environment variable
set KD_VERSION=ver-0.6r42m0
push-version.sh
```

Optional commit message:

```bash
.\push-version.sh -Version ver-0.6r42m0 -Message "Release ver-0.6r42m0"
```

---

## 4. What the script does

1. **Writes `version.txt`** at the repo root, for example:

   ```text
   ver-0.6r42m0
   2026-08-06T10:41:23+03:00
   ```

   - Line 1 = version  
   - Line 2 = local timestamp when the script ran  

2. **Ensures a git repo** at the root (`git init` if `.git` is missing).

3. **Sets remote** `origin` → `https://github.com/oattia-ot/idol-linux-setup.git`  
   (updates the URL if a different one was set).

4. **Creates a default `.gitignore`** at the repo root if one doesn't already
   exist yet (excludes `*.zip`, `*.msi`, `*.exe`, `logs/`, `ssl/`, `env/`,
   extracted NiFi `.nar` connectors, and Python cache files — see §7). An
   existing `.gitignore` is never touched. This is what keeps large
   component packages and installers from ending up in the repo and
   causing push timeouts (see §9).

5. **Stages all files** under the root (`git add -A`).

6. **Warns before committing** if any staged file is **20 MB or larger**
   (e.g. a package that slipped past `.gitignore` because it was already
   tracked, or force-added). You can bail out, unstage it, and fix
   `.gitignore` before continuing.

7. **Commits** when there are changes (or on a brand-new repo).

8. **Asks for confirmation** before push:

   ```text
   Push to GitHub now? (Y/N) [N]:
   ```

   Type **Y** to push. Anything else cancels (local commit is kept).

   Before pushing, the script also raises `http.postBuffer` and disables
   the low-speed abort **for this repo only**, and retries once
   automatically on failure — both aimed at large/slow pushes that would
   otherwise hit `HTTP 408` (see §9).

---

## 5. Confirm `.git` location

`.git` is created on the **root**, not next to the script.

```bash
Test-Path /opt/KD-Setup\kd-win-setup-python.git
# True

Get-ChildItem /opt/KD-Setup\kd-win-setup-python -Force -Directory | Where-Object Name -eq '.git'
```

In File Explorer: **View → Show → Hidden items**.

---

## 6. GitHub authentication

First push may ask for credentials:

| Method | Notes |
|--------|--------|
| **Git Credential Manager** | Browser login (recommended with Git for Linux) |
| **Personal Access Token (PAT)** | Use as password when HTTPS prompts for password |
| **SSH** | Change remote to `git@github.com:oattia-ot/idol-linux-setup.git` if you use SSH keys |

---

## 7. `.gitignore` (auto-created)

As of this version, the script **creates a default `.gitignore`** at
`/opt/KD-Setup\kd-win-setup-python.gitignore` automatically on first run, if
one doesn't already exist:

```gitignore
# Packages / binaries (component ZIPs, JDK/systemd (or nifi.sh) installers, etc.)
*.zip
*.msi
*.exe
!nifi/systemd

# Runtime / secrets
logs/
ssl/
env/
*.log

# Generated NiFi extension NARs (re-extracted from NiFiIngest ZIP on demand)
nifi/nifi-connectors/*.nar

# Python
__pycache__/
*.pyc
.venv/
```

This is what keeps the large KD/NiFi component packages, installers, and
secrets out of GitHub — and it's also the #1 cause of the push timeouts
described in §9, so don't delete it. Edit it freely to match what you want
published; **an existing `.gitignore` is never overwritten** by the script.

**Already have large files tracked from before this existed?** A
`.gitignore` only stops *new* files from being added — it does not remove
files already committed. See §9 for how to untrack (or, if they're deep in
history, purge) them.

---

## 8. Typical full run

```bash
cd /opt/KD-Setup\kd-win-setup-python\tools

$env:KD_VERSION = "ver-0.6r42m0"
.\push-version.sh
```

Expected flow:

1. `[OK] Wrote version.txt -> ver-0.6r42m0  (2026-08-06T...)`  
2. Staging / commit messages  
3. Prompt: `Push to GitHub now? (Y/N) [N]:` → type **Y**  
4. `[OK] version.txt (...) pushed to https://github.com/oattia-ot/idol-linux-setup.git (main)`  

---

## 9. Troubleshooting

| Symptom | What to do |
|---------|------------|
| `utf8NoBOM` / Encoding error | Use the latest `push-version.sh` (uses .NET UTF-8 without BOM). |
| `fatal: not a git repository` as terminating error | Use the latest script (`Invoke-Git` helper). |
| `src refspec main does not match any` | No commit yet; latest script always creates a first commit. |
| Only `version.txt` pushed (2 objects) | Old script staged only that file; latest uses `git add -A`. |
| Cannot see `.git` | Enable hidden items; check **root**, not `tools/`. |
| Push auth failed | Sign in via Credential Manager or use a PAT / SSH. |
| Push rejected (non-fast-forward) | `git pull --rebase origin main` then run the script again. |
| `error: RPC failed; HTTP 408 curl 22 ... 408` / `unexpected disconnect while reading sideband packet` / `fatal: the remote end hung up unexpectedly` | Almost always a large push (hundreds of MB) timing out — usually a ZIP/MSI/EXE that got committed before `.gitignore` existed. The script already raises `http.postBuffer`/disables the low-speed abort and retries once automatically. If it still fails: find the largest tracked objects with `git rev-list --objects --all \| git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \| sort -k3 -n -r \| head`, then `git rm --cached <file>` (add a `.gitignore` rule so it doesn't come back), commit, and re-run. If the large file is further back in history (not just the latest commit), untracking it isn't enough — you need `git filter-repo` or the BFG Repo-Cleaner to rewrite history before the push will succeed. As a fallback, try SSH instead of HTTPS: `git remote set-url origin git@github.com:oattia-ot/idol-linux-setup.git`. |
| Warned about a large staged file before commit | Expected — the script flags anything ≥20 MB before committing so you can catch it early instead of discovering it at push time. Unstage with `git restore --staged <file>` if it shouldn't be tracked. |

---

## 10. Parameters reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `-Version` | *(from env)* | Version string written to `version.txt` |
| `-Message` | `Bump version to <Version>` | Commit message |
| `-RemoteName` | `origin` | Git remote name |
| `-Branch` | current, or `main` | Branch to push |
| `-RemoteUrl` | `https://github.com/oattia-ot/idol-linux-setup.git` | Remote URL |

---

## Quick checklist

- [ ] Git installed and on `PATH`
- [ ] Scripts under `config/` or `tools/`
- [ ] Version set via `-Version` or `KD_VERSION`
- [ ] Run script → confirm `version.txt` on root
- [ ] Confirm `.git` on root (hidden items on)
- [ ] Answer **Y** only when you want to push
- [ ] Optional: add `.gitignore` before first full push
