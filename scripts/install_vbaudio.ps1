param(
    [string]$DriversPath
)

$setup = Join-Path $DriversPath "VBCABLE_Setup_x64.exe"

# Uninstall existing (silent, ignore failure if not installed)
Start-Process -FilePath $setup -ArgumentList "-u -h" -Wait -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

# Fresh install (silent)
Start-Process -FilePath $setup -ArgumentList "-i -h" -Wait
Start-Sleep -Seconds 5

Write-Output "INSTALL_DONE"
