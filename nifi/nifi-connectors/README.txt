NiFi connector staging folder (source)

Place or extract *.nar files here. Typical workflow in the Config Dashboard:

  1. Put NiFiIngest_*.zip under ZipPath
  2. Click "Extract from ZIP" in the NiFi Connectors panel
     → all *.nar members are written into this folder
  3. Click "Copy source → target"
     → files are copied to BasePath\NiFi\extensions (loaded by NiFi at startup)

This directory is <SetupPath>\nifi\nifi-connectors
(SetupPath = the toolkit root, e.g. C:\KD-Setup\idol-windows-setup).
