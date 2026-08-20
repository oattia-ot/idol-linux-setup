KD Configuration Dashboard
==========================

A local web UI for configuring the KD / IDOL Windows setup before installation.

Files (keep this folder intact)
-------------------------------
  kd-config-dashboard.html      Main UI
  kd-config-server.py           Local HTTP + JSON API server
  kd-nifi-auto-fix.js           Client-side NiFi auto-sync helper
  Start-KD-Config-Dashboard.bat Windows launcher
  README-KD-Config-Dashboard.txt  This file
  apply_kd_fixes.py             Optional one-shot patcher (historical)

Requirements
------------
  - Python 3.8+ on PATH (same environment used by the KD installer)

Quick start
-----------
  Double-click  Start-KD-Config-Dashboard.bat

  Browser opens at:
    http://127.0.0.1:5000/kd-config-dashboard.html

  Stop the server with Ctrl+C in the console, or use the Exit button
  in the UI.

What you can configure
----------------------
  - Ports for every component (Content, Agentstore, AnswerServer, QMS,
    View, NiFi, Find, …)
  - ThisHost / LicenseHost
  - Components to install
  - SetupPath (toolkit root, auto-filled) and ZipPath
  - Per-component ZIP locations (including optional NiFiIngest package)
  - Browser URLs (auto-updated from ports)
  - NiFi settings (heap, credentials)
  - NiFi Connectors:
      Source  = <SetupPath>\nifi\nifi-connectors
      Target  = <BasePath>\NiFi\extensions
      *.nar files are extracted automatically from NiFiIngest_*.zip
      into Source when the ZIP is found under ZipPath.
      One-shot auto-sync endpoint: POST /api/auto-sync-nifi
      Manual buttons: Copy source → target, + add NAR, − remove
  - Find settings
  - Sizing guidance (S / M / L)

Export / Save
-------------
  Use “Save changes” or “Export JSON” in the page.

  When started with this server the file is written to:

    <setup-root>\config\my-config.json

  Default setup root (auto-detected by the .bat when present):
    C:\KD-Setup\idol-windows-setup

  Override:
    set KD_SETUP_ROOT=C:\KD-Setup\idol-windows-setup
    Start-KD-Config-Dashboard.bat

  Or run the server directly:
    python kd-config-server.py --setup-root "C:\KD-Setup\idol-windows-setup"
    python kd-config-server.py --config-path "C:\...\config\my-config.json"

  The header of the UI shows the full export path when the API is reachable.

LLM / Grok API key (AnswerServer RAG)
-------------------------------------
  The dashboard does not edit the Grok / LLM provider key.

  After exporting my-config.json (or after install), set the key with the
  PowerShell tool (also available as installer menu 11):

    .\tools\Update-ConfigFiles.ps1
    .\Install-KD.ps1 -Mode UpdateConfigFiles

  Edit the “to” value for YOUR_AI_LLM_PROVIDER_KEY (and any ports) in
  tools\replacements.json before running the script.
  Backups (*.bak) are created automatically.

  The script also keeps config\my-config.json in sync with the target
  ports so the dashboard and installer stay aligned.

  Files that receive the key:
    config\cfg\answerserver\rag\grok.lua
    config\cfg\answerserver\rag\grok.py
    config\cfg\answerserver\rag\grok\grok4.py

API endpoints (local only)
--------------------------
  GET  /api/config-path
  GET  /api/app-version
  GET  /api/load-config
  GET  /api/default-config
  GET  /api/replacements
  POST /api/save-config
  POST /api/check-zips | check-file | check-port | check-path
  POST /api/ping-host
  POST /api/basepath-version
  POST /api/upload-zip | pick-zip | pick-folder | list-dir
  POST /api/copy-server-file | client-log
  POST /api/list-nars | pick-nar | delete-nar
  POST /api/extract-nars-from-zip | copy-nars | auto-sync-nifi
  POST /api/open-urls | shutdown
  POST /api/replacements | validate-replacements | apply-replacements
  WS   /ws/host-status   (real-time host DNS + reachability stream)

Troubleshooting
---------------
  - 404 on /api/auto-sync-nifi
    Ensure you are running the latest kd-config-server.py (the route must
    be registered in do_POST). Restart the server after any file update.

  - Native file/folder dialogs fail or appear twice
    Only one dialog may be open at a time; concurrent requests return 423.

  - Python not found
    Install Python 3 and tick “Add python.exe to PATH”.

Notes
-----
  - The server binds to 127.0.0.1 only.
  - Static files and the API share the same origin; no CORS issues in normal use.
  - All write operations target paths under the resolved Setup / Base paths.
