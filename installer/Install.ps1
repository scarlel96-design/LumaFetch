$ErrorActionPreference = 'Stop'
$appName = 'Luma Fetch'
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\LumaFetch'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
New-Item -ItemType Directory -Force -Path $installDir, $startMenu | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LumaFetch.exe') -Destination (Join-Path $installDir 'LumaFetch.exe') -Force
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $startMenu "$appName.lnk"))
$shortcut.TargetPath = Join-Path $installDir 'LumaFetch.exe'
$shortcut.WorkingDirectory = $installDir
$shortcut.Description = 'Fast image batch downloader'
$shortcut.Save()
$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop) {
    $desktopShortcut = $shell.CreateShortcut((Join-Path $desktop "$appName.lnk"))
    $desktopShortcut.TargetPath = $shortcut.TargetPath
    $desktopShortcut.WorkingDirectory = $installDir
    $desktopShortcut.Description = $shortcut.Description
    $desktopShortcut.Save()
}
Start-Process -FilePath (Join-Path $installDir 'LumaFetch.exe')
