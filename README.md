# Larsen VM

A virtual mic app with a DAW-style plugin chain, powered by VB-Audio Virtual Cable.

## Project Structure

```
larsen-vm/
  main.py                   # Python backend + pywebview entry point
  requirements.txt
  drivers/                  # Bundle the full VB-Audio driver folder here
    VBCABLE_Setup_x64.exe
    vbMmeCable64_win10.inf
    vbaudio_cable64_win10.sys
    ... (all other files from the VB-Audio zip)
  scripts/
    install_vbaudio.ps1     # Silent install/uninstall
    configure_audio.ps1     # Rename + disable devices (runs as SYSTEM)
  web/
    index.html              # Full frontend
```

## Setup

```bash
pip install -r requirements.txt
```

Download VB-Audio Virtual Cable from https://vb-audio.com/Cable/
and place all files from the zip into the `drivers/` folder.

## Run

```bash
python main.py
```

The app auto-requests admin via UAC on launch (configure your shortcut or manifest).
The setup flow handles all driver installation on first run.

## Signal Flow

```
[Mic Input] → Python sounddevice capture
           → Plugin chain (ordered by x position on canvas)
           → VB-Audio "CABLE Input" (renamed: "Larsen VM")
           → Other apps see "Larsen (Virtual Mic)" as a mic source
```

## On Launch (every time)

1. Check registry for VB-Audio driver state
2. If already configured by us → skip straight to main UI
3. If VB-Audio present but not ours → hijack warning → reinstall + configure
4. If not installed → install consent → install + configure
5. Wait for devices to register → load main UI

## Notes

- The app must run as Administrator (UAC) for driver install + registry writes
- `configure_audio.ps1` runs as SYSTEM via scheduled task trick (no PsExec needed)
- Device names in WASAPI apps (Discord, DAWs, etc.) will show the renamed names
- Some apps read at driver level and may still show original VB-Audio names
