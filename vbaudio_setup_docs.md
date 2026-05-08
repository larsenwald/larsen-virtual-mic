# VB-Audio Virtual Cable — Programmatic Install, Rename & Configure

## Overview
This documents how to programmatically install VB-Audio Virtual Cable, rename its devices for WASAPI, and disable unwanted endpoints. All of this is designed to run during an app installer on any Windows machine.

---

## Prerequisites
- Installer must run as **Admin**
- Bundle the **entire VB-Audio driver folder** with your installer (not just the exe — it needs the INF and SYS files alongside it)
- No other dependencies needed (no PsExec, no third party tools)

```
MyApp/
  install.exe
  drivers/
    VBCABLE_Setup_x64.exe
    vbMmeCable64_win10.inf
    vbaudio_cable64_win10.sys
    ... all other files in the folder
```

---

## Step 1 — Check if VB-Audio is already installed
```powershell
$installed = Get-PnpDevice | Where-Object { $_.FriendlyName -like "*VB-Audio*" }
if ($installed) {
    # Warn user, then uninstall and reinstall fresh
} else {
    # Fresh install
}
```

---

## Step 2 — Uninstall and reinstall silently
VB-Audio supports `-i -h` for silent install and `-u -h` for silent uninstall. No GUI, no popups. Must be run from the folder containing all the driver files.

```powershell
# Uninstall
Start-Process -FilePath "drivers\VBCABLE_Setup_x64.exe" -ArgumentList "-u -h" -Verb RunAs -Wait

# Reinstall
Start-Process -FilePath "drivers\VBCABLE_Setup_x64.exe" -ArgumentList "-i -h" -Verb RunAs -Wait
```

---

## Step 3 — Rename & Configure Devices

After install, VB-Audio creates two render (output) devices and one capture (input) device. Their names are stored in the Windows audio endpoint registry under:
- `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render` — output devices
- `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture` — input devices

Each device has two name fields:
- `{a45c254e-df1c-4efd-8020-67d146a850e0},2` — text **before** the parentheses
- `{b3f8fa53-0004-438e-9003-51a46e139bfc},6` — text **inside** the parentheses

Windows always displays names as `"before (inside)"`. The parentheses cannot be removed.

These keys are owned by TrustedInstaller. Even SYSTEM doesn't have write access by default, so you must first grant SYSTEM full control, then write. The cleanest way to run as SYSTEM from an Admin process is the **scheduled task trick** — no PsExec or other dependencies needed.

### Finding devices dynamically
GUIDs are unique per machine and cannot be hardcoded. Always find them dynamically by searching for VB-Audio by name.

### Full configure_audio.ps1 script
Save this as `configure_audio.ps1` and call it via the scheduled task wrapper below.

```powershell
# ---- FIND DEVICES ----

# The 16ch output device (keep and rename)
$render = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render" | Where-Object {
    $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
    $props."{a45c254e-df1c-4efd-8020-67d146a850e0},2" -like "*16*" -and
    $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -like "*VB-Audio*"
} | Select-Object -First 1

# The capture (mic) device
$capture = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture" | Where-Object {
    $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
    $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -like "*VB-Audio*"
} | Select-Object -First 1

# The unwanted CABLE Input render device (disable this one)
$cableInput = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render" | Where-Object {
    $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
    $props."{a45c254e-df1c-4efd-8020-67d146a850e0},2" -eq "CABLE Input" -and
    $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -like "*VB-Audio*"
} | Select-Object -First 1


# ---- GRANT SYSTEM FULL CONTROL ----

foreach ($path in @(
    "SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\$($render.PSChildName)\Properties",
    "SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture\$($capture.PSChildName)\Properties"
)) {
    $rk = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
        $path,
        [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree,
        [System.Security.AccessControl.RegistryRights]::ChangePermissions)
    $acl = $rk.GetAccessControl()
    $acl.SetAccessRule((New-Object System.Security.AccessControl.RegistryAccessRule("SYSTEM","FullControl","Allow")))
    $rk.SetAccessControl($acl)
}


# ---- RENAME RENDER DEVICE ----
# Shows as "Larsen VM (Don't Touch)"
Set-ItemProperty -Path "$($render.PSPath)\Properties" -Name "{a45c254e-df1c-4efd-8020-67d146a850e0},2" -Value "Larsen VM"
Set-ItemProperty -Path "$($render.PSPath)\Properties" -Name "{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -Value "Don't Touch"


# ---- RENAME CAPTURE DEVICE ----
# Shows as "Larsen (Virtual Mic)"
Set-ItemProperty -Path "$($capture.PSPath)\Properties" -Name "{a45c254e-df1c-4efd-8020-67d146a850e0},2" -Value "Larsen"
Set-ItemProperty -Path "$($capture.PSPath)\Properties" -Name "{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -Value "Virtual Mic"


# ---- DISABLE CABLE INPUT RENDER DEVICE ----
# Uses IPolicyConfig COM interface, same as mmsys.cpl does it
$code = @"
using System;
using System.Runtime.InteropServices;

[Guid("f8679f50-850a-41cf-9c72-430f290290c8")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfig {
    void GetMixFormat();
    void GetDeviceFormat();
    void ResetDeviceFormat();
    void SetDeviceFormat();
    void GetProcessingPeriod();
    void SetProcessingPeriod();
    void GetShareMode();
    void SetShareMode();
    void GetPropertyValue();
    void SetPropertyValue();
    void SetDefaultEndpoint();
    [PreserveSig] int SetEndpointVisibility([MarshalAs(UnmanagedType.LPWStr)] string devId, bool visible);
}

[Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")]
[ClassInterface(ClassInterfaceType.None)]
[ComImport]
class PolicyConfigClient {}

public class AudioPolicy {
    public static void SetVisibility(string deviceId, bool visible) {
        var pc = (IPolicyConfig)new PolicyConfigClient();
        Marshal.ThrowExceptionForHR(pc.SetEndpointVisibility(deviceId, visible));
    }
}
"@
Add-Type -TypeDefinition $code
[AudioPolicy]::SetVisibility("{0.0.0.00000000}.$($cableInput.PSChildName)", $false)


# ---- RESTART AUDIO SERVICE ----
Restart-Service -Name Audiosrv
```

### Scheduled task wrapper
Call this from your installer. It writes the script to a temp file, runs it as SYSTEM, then cleans up.

```powershell
$script = Get-Content "configure_audio.ps1" -Raw
$script | Out-File "$env:TEMP\configure_audio.ps1"
$a = New-ScheduledTaskAction -Execute "powershell" -Argument "-ExecutionPolicy Bypass -File `"$env:TEMP\configure_audio.ps1`""
Register-ScheduledTask -TaskName "ConfigureAudio" -Action $a -Principal (New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest) -Force | Out-Null
Start-ScheduledTask -TaskName "ConfigureAudio"
Start-Sleep 10
Unregister-ScheduledTask -TaskName "ConfigureAudio" -Confirm:$false
Remove-Item "$env:TEMP\configure_audio.ps1"
```

---

## Caveats
- **Bundle the entire driver folder** — the exe alone won't work, it needs the INF and SYS files alongside it
- **Parentheses are hardcoded by Windows** — names always render as "before (inside)", can't be removed
- **GUIDs are machine-specific** — always find devices dynamically by name, never hardcode GUIDs
- **WASAPI apps** (Discord, DAWs, Studio One, etc.) will see the renamed devices correctly
- **Some apps** (Sound Recorder) read directly from the driver level and will still show original VB-Audio names — unavoidable
- **Audiosrv restart** is required after renaming for changes to take effect
- **The scheduled task trick** is the cleanest way to get SYSTEM privileges from an Admin installer without bundling any extra tools
- **Sleep 10** in the scheduled task wrapper — give it enough time to finish before cleaning up, especially on slower machines
