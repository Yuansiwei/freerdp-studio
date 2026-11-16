$ErrorActionPreference = 'SilentlyContinue'
Write-Output '=== C:\OpenSSL layout ==='
Get-ChildItem 'C:\OpenSSL' -Force | ForEach-Object {
    $kind = 'file'
    if ($_.PSIsContainer) { $kind = 'dir' }
    Write-Output ('  {0,-14} {1}' -f $_.Name, $kind)
}

Write-Output '=== version ==='
$v = 'C:\OpenSSL\include\openssl\opensslv.h'
if (Test-Path $v) {
    Select-String -Path $v -Pattern 'OPENSSL_VERSION_(MAJOR|MINOR|PATCH)' | ForEach-Object { Write-Output ('  ' + $_.Line.Trim()) }
} else {
    Write-Output '  opensslv.h missing'
}

Write-Output '=== libs ==='
Get-ChildItem 'C:\OpenSSL\lib' -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 12 | ForEach-Object {
    Write-Output ('  {0,-44} {1,10:N0}' -f $_.FullName.Replace('C:\OpenSSL\', ''), $_.Length)
}

Write-Output '=== bin ==='
Get-ChildItem 'C:\OpenSSL\bin' -File -ErrorAction SilentlyContinue | Select-Object -First 8 | ForEach-Object {
    Write-Output ('  {0,-28} {1,10:N0}' -f $_.Name, $_.Length)
}

Write-Output '=== openssl version ==='
$exe = 'C:\OpenSSL\bin\openssl.exe'
if (Test-Path $exe) {
    $b = [IO.File]::ReadAllBytes($exe)
    $pe = [BitConverter]::ToInt32($b, 0x3C)
    $machine = [BitConverter]::ToUInt16($b, $pe + 4)
    Write-Output ('  machine=0x{0:X4}' -f $machine)
    & $exe version 2>&1 | ForEach-Object { Write-Output ('  ' + $_) }
} else {
    Write-Output '  openssl.exe missing'
}

Write-Output '=== toolchain ==='
foreach ($p in @(
    'C:\Tools\cmake-4.4.5-windows-x86_64\bin\cmake.exe',
    'C:\Tools\ninja-v1.13.2\ninja.exe',
    'C:\BuildTools\VC\Auxiliary\Build\vcvars64.bat',
    'C:\BuildTools\MSBuild\Current\Bin\MSBuild.exe'
)) {
    Write-Output ('  {0,-6} {1}' -f (Test-Path $p), $p)
}
