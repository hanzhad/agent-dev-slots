<#
.SYNOPSIS
  Installs the `dev-container` command on Windows.

.DESCRIPTION
  The engine and every project live in WSL; a Windows checkout is a read-only mirror.
  So the command on Windows is a small dev-container.cmd that runs the Linux command
  (scripts/dev-container-global) inside WSL and hands over the Windows directory it was
  run from, which the Linux command maps with wslpath when it is inside a project.

  This script
    1. installs the Linux command inside WSL (scripts/install-cli.sh: a link in ~/.local/bin),
    2. writes dev-container.cmd into -Dir.
  It is safe to run again, needs no administrator rights, and does not change PATH or any
  profile: when -Dir is not on PATH it prints the line that adds it.

.PARAMETER Clone
  The engine clone inside WSL, as a Linux path (for example ~/Projects/dev-container-engine).
  Default: the clone this script is in, when that is inside WSL.

.PARAMETER Dir
  Where dev-container.cmd goes. Default: $env:USERPROFILE\bin.

.PARAMETER Distro
  The WSL distribution to use. Default: the default one.

.PARAMETER Uninstall
  Remove dev-container.cmd (only if this script wrote it) and the Linux link.
#>
[CmdletBinding()]
param(
    [string]$Clone = '',
    [string]$Dir = (Join-Path $env:USERPROFILE 'bin'),
    [string]$Distro = '',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$marker = 'rem written by scripts/install-cli.ps1'

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'wsl.exe was not found. Install WSL first (wsl --install); the engine and the projects live there.'
}

$wsl = @()
if ($Distro) { $wsl = @('-d', $Distro) }

function Invoke-Wsl {
    param([string[]]$Arguments)
    $out = & wsl.exe @wsl -e @Arguments
    if ($LASTEXITCODE -ne 0) { throw "wsl.exe $($Arguments -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

if ($Clone) {
    if ($Clone -eq '~' -or $Clone.StartsWith('~/')) {
        $homeDir = (Invoke-Wsl @('sh', '-c', 'echo $HOME') | Select-Object -First 1)
        $Clone = $homeDir + $Clone.Substring(1)
    }
    $linuxClone = (Invoke-Wsl @('sh', '-c', 'cd $1 && pwd -P', 'sh', $Clone) | Select-Object -First 1)
} else {
    $linuxScripts = (Invoke-Wsl @('wslpath', '-u', $PSScriptRoot) | Select-Object -First 1)
    if ($linuxScripts -like '/mnt/*') {
        throw "This script is on a Windows drive ($PSScriptRoot): that is a read-only mirror. Run the copy in the WSL clone, or pass -Clone <the clone, as a Linux path>."
    }
    $linuxClone = $linuxScripts -replace '/scripts/?$', ''
}
$linuxInstaller = "$linuxClone/scripts/install-cli.sh"
$linuxCommand = "$linuxClone/scripts/dev-container-global"
$shim = Join-Path $Dir 'dev-container.cmd'

if ($Uninstall) {
    if (Test-Path -LiteralPath $shim) {
        if ((Get-Content -LiteralPath $shim -Raw) -notlike "*$marker*") {
            throw "$shim was not written by this script, so it was left alone."
        }
        Remove-Item -LiteralPath $shim
        Write-Output "removed $shim"
    }
    Invoke-Wsl @('sh', $linuxInstaller, '--uninstall') | Write-Output
    return
}

Invoke-Wsl @('test', '-x', $linuxCommand) | Out-Null
Invoke-Wsl @('sh', $linuxInstaller) | Write-Output

$distroArgs = ''
if ($Distro) { $distroArgs = " -d $Distro" }
$lines = @(
    '@echo off',
    $marker,
    'set "DEV_CONTAINER_WINDOWS_CWD=%CD%"',
    'if defined WSLENV (set "WSLENV=DEV_CONTAINER_WINDOWS_CWD:%WSLENV%") else (set "WSLENV=DEV_CONTAINER_WINDOWS_CWD")',
    "wsl.exe$distroArgs -e `"$linuxCommand`" %*",
    'exit /b %ERRORLEVEL%'
)
$text = ($lines -join "`r`n") + "`r`n"

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
if (Test-Path -LiteralPath $shim) {
    $current = Get-Content -LiteralPath $shim -Raw
    if ($current -eq $text) {
        Write-Output "already installed: $shim"
    } elseif ($current -like "*$marker*") {
        [System.IO.File]::WriteAllText($shim, $text, [System.Text.Encoding]::ASCII)
        Write-Output "updated $shim"
    } else {
        throw "$shim exists and was not written by this script. Look at it, remove it if it is an old launcher, or pass another -Dir. Nothing was changed."
    }
} else {
    [System.IO.File]::WriteAllText($shim, $text, [System.Text.Encoding]::ASCII)
    Write-Output "installed $shim"
}

$onPath = @($env:Path -split ';' | Where-Object { $_.TrimEnd('\') -ieq $Dir.TrimEnd('\') }).Count -gt 0
if (-not $onPath) {
    Write-Output "$Dir is not on your PATH. To add it for your user, run this, then open a new terminal:"
    Write-Output "    [Environment]::SetEnvironmentVariable('Path', '$Dir;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')"
}
Write-Output 'Check it with: dev-container doctor'
