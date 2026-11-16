# prepare-openssl.ps1 -- one-time dependency bootstrap for building wfreerdp on this machine.
# Installs vcpkg + openssl:x64-windows into C:\Tools\vcpkg (no admin required).
$ErrorActionPreference = 'Continue'
$root = 'C:\Tools\vcpkg'

Write-Output "=== 1. vcpkg at $root ==="
if (-not (Test-Path (Join-Path $root 'vcpkg.exe'))) {
    if (-not (Test-Path $root)) {
        Write-Output '  cloning vcpkg ...'
        & git clone --depth 1 https://github.com/microsoft/vcpkg.git $root 2>&1 |
            Select-Object -Last 3 | ForEach-Object { Write-Output ('  ' + $_) }
    }
    Write-Output '  bootstrapping ...'
    & "$env:SystemRoot\System32\cmd.exe" /c "cd /d `"$root`" && bootstrap-vcpkg.bat -disableMetrics" 2>&1 |
        Select-Object -Last 6 | ForEach-Object { Write-Output ('  ' + $_) }
} else {
    Write-Output '  already bootstrapped'
}

Write-Output '=== 2. openssl:x64-windows ==='
$ssl = Join-Path $root 'installed\x64-windows\include\openssl\ssl.h'
if (Test-Path $ssl) {
    Write-Output '  already installed'
} else {
    Write-Output '  installing (builds OpenSSL from source, expect 10-25 min) ...'
    & "$root\vcpkg.exe" install openssl:x64-windows --triplet x64-windows --clean-after-build 2>&1 |
        ForEach-Object { Write-Output ('  ' + $_) }
    $rc = $LASTEXITCODE
    Write-Output ("  vcpkg exit=$rc")
}

Write-Output '=== 3. result ==='
if (Test-Path $ssl) {
    Write-Output ("  OK  " + $ssl)
    $lib = Join-Path $root 'installed\x64-windows\lib'
    Get-ChildItem $lib -Filter '*ssl*' -ErrorAction SilentlyContinue | ForEach-Object { Write-Output ('  lib: ' + $_.Name) }
    Get-ChildItem $lib -Filter '*crypto*' -ErrorAction SilentlyContinue | ForEach-Object { Write-Output ('  lib: ' + $_.Name) }
    Write-Output 'READY'
} else {
    Write-Output '  FAILED - openssl headers not found'
    Write-Output 'NOT_READY'
}
