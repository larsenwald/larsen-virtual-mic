# configure_audio.ps1
# Runs as SYSTEM via scheduled task - required to write MMDevices registry keys owned by TrustedInstaller

# ---- FIND DEVICES ----

# The 16ch output device (keep, rename to "Larsen VM (Don't Touch)")
$render = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render" | Where-Object {
    $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
    $props."{a45c254e-df1c-4efd-8020-67d146a850e0},2" -like "*16*" -and
    $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -like "*VB-Audio*"
} | Select-Object -First 1

# Fallback: any VB-Audio render not named "CABLE Input"
if (-not $render) {
    $render = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render" | Where-Object {
        $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
        $name = $props."{a45c254e-df1c-4efd-8020-67d146a850e0},2"
        $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -like "*VB-Audio*" -and $name -ne "CABLE Input"
    } | Select-Object -First 1
}

# The capture (mic) device - rename to "Larsen (Virtual Mic)"
$capture = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture" | Where-Object {
    $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
    $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -like "*VB-Audio*"
} | Select-Object -First 1

# The "CABLE Input" render device - disable this one (we pipe into it internally, hide from users)
$cableInput = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render" | Where-Object {
    $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
    $props."{a45c254e-df1c-4efd-8020-67d146a850e0},2" -eq "CABLE Input" -and
    $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -like "*VB-Audio*"
} | Select-Object -First 1

if (-not $render -or -not $capture) {
    Write-Output "ERROR: Could not find VB-Audio devices"
    exit 1
}

# ---- GRANT SYSTEM FULL CONTROL ON PROPERTIES KEYS ----

$paths = @(
    "SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\$($render.PSChildName)\Properties"
    "SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture\$($capture.PSChildName)\Properties"
)

foreach ($path in $paths) {
    $rk = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
        $path,
        [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree,
        [System.Security.AccessControl.RegistryRights]::ChangePermissions
    )
    if ($rk) {
        $acl = $rk.GetAccessControl()
        $rule = New-Object System.Security.AccessControl.RegistryAccessRule(
            "SYSTEM", "FullControl", "Allow"
        )
        $acl.SetAccessRule($rule)
        $rk.SetAccessControl($acl)
        $rk.Close()
    }
}

# ---- RENAME RENDER DEVICE ----
# Displays as: "Larsenwald VM (Don't Touch)"
Set-ItemProperty -Path "$($render.PSPath)\Properties" `
    -Name "{a45c254e-df1c-4efd-8020-67d146a850e0},2" -Value "Larsenwald VM"
Set-ItemProperty -Path "$($render.PSPath)\Properties" `
    -Name "{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -Value "Don't Touch"

# ---- RENAME CAPTURE DEVICE ----
# Displays as: "Larsenwald (Virtual Mic)"
Set-ItemProperty -Path "$($capture.PSPath)\Properties" `
    -Name "{a45c254e-df1c-4efd-8020-67d146a850e0},2" -Value "Larsenwald"
Set-ItemProperty -Path "$($capture.PSPath)\Properties" `
    -Name "{b3f8fa53-0004-438e-9003-51a46e139bfc},6" -Value "Virtual Mic"

# ---- DISABLE CABLE INPUT RENDER DEVICE ----
# Also used to restore default playback device after audio service restart

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
    [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string devId, uint role);
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
    public static void SetDefault(string deviceId) {
        var pc = (IPolicyConfig)new PolicyConfigClient();
        // Roles: 0 = Console, 1 = Multimedia, 2 = Communications
        pc.SetDefaultEndpoint(deviceId, 0);
        pc.SetDefaultEndpoint(deviceId, 1);
        pc.SetDefaultEndpoint(deviceId, 2);
    }
}
"@
Add-Type -TypeDefinition $code

# ---- READ CURRENT DEFAULT PLAYBACK DEVICE BEFORE RESTART ----
# The default render device GUID is flagged in the registry under each device's key
$defaultDeviceId = $null
$renderRoot = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
Get-ChildItem $renderRoot | ForEach-Object {
    try {
        # Default devices have Level:0 = 0 (or higher) stored under their key directly
        $val = Get-ItemProperty -Path $_.PSPath -Name "Level:0" -ErrorAction SilentlyContinue
        if ($val -and $val."Level:0" -ge 0) {
            # Exclude our VB-Audio device from being saved as the default to restore
            $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
            $devName = $props."{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
            if ($devName -notlike "*VB-Audio*" -and $devName -notlike "*CABLE*") {
                $defaultDeviceId = "{0.0.0.00000000}.$($_.PSChildName)"
            }
        }
    } catch {}
}

if ($cableInput) {
    [AudioPolicy]::SetVisibility("{0.0.0.00000000}.$($cableInput.PSChildName)", $false)
}

# ---- RESTART AUDIO SERVICE ----
Restart-Service -Name Audiosrv -Force
Start-Sleep -Seconds 2

# ---- RESTORE DEFAULT PLAYBACK DEVICE ----
if ($defaultDeviceId) {
    try {
        [AudioPolicy]::SetDefault($defaultDeviceId)
    } catch {}
}

Write-Output "CONFIGURE_DONE"