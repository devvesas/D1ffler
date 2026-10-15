[CmdletBinding()]
param(
    [switch]$Release,
    [switch]$SkipInstall,
    [switch]$SkipSmokeTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found on PATH. The supplied executable build profile requires 64-bit Python 3.10.7."
}
$buildArguments = @((Join-Path $projectRoot "scripts\build_app.py"))
if ($Release) { $buildArguments += "--release" }
if ($SkipInstall) { $buildArguments += "--skip-install" }
if ($SkipSmokeTests) { $buildArguments += "--skip-smoke-tests" }
python @buildArguments
if ($LASTEXITCODE -ne 0) {
    throw "Executable build or verification failed ($LASTEXITCODE). See the build logs."
}
