# make-openssl-dev.ps1 -- build a usable OpenSSL "dev" tree WITHOUT compiling OpenSSL.
# Downloads the 3.6.2 headers, then generates import libraries from the shipping DLLs
# using dumpbin/lib from MSVC. Result: C:\Tools\openssl-3.6.2\{include,lib}
$ErrorActionPreference = 'Continue'
$dest = 'C:\Tools\openssl-3.6.2'
$zip  = Join-Path $env:TEMP 'openssl-3.6.2.tar.gz'
$srcTree = Join-Path $env:TEMP 'openssl-src-3.6.2'
$curl = 'C:\Studio\curl.exe'
$dllDir = 'C:\Users\Studio\Documents\deepseek-harness\default-workspace\freerdp-studio\dist\rdpbridge-3.33.1-20261116'
$vcvars = 'C:\BuildTools\VC\Auxiliary\Build\vcvars64.bat'

Write-Output '=== 1. download headers tarball ==='
Remove-Item $zip -Force -ErrorAction SilentlyContinue
& $curl -sL --max-time 120 -o $zip "https://github.com/openssl/openssl/archive/refs/tags/openssl-3.6.2.tar.gz"
if (-not (Test-Path $zip)) { Write-Output '  DOWNLOAD FAILED'; exit 1 }
Write-Output ('  {0:N1} MB' -f ((Get-Item $zip).Length / 1MB))

Write-Output '=== 2. extract only what is needed ==='
Remove-Item $srcTree -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $srcTree | Out-Null
& tar.exe -xzf $zip -C $srcTree 2>&1 | Select-Object -First 3 | ForEach-Object { Write-Output ('  ' + $_) }
$root = Get-ChildItem $srcTree -Directory | Select-Object -First 1
if (-not $root) { Write-Output '  EXTRACT FAILED'; exit 1 }
Write-Output ('  extracted: ' + $root.Name)

Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $dest 'include'), (Join-Path $dest 'lib') | Out-Null
Copy-Item (Join-Path $root.FullName 'include\openssl') -Destination (Join-Path $dest 'include') -Recurse -Force
Write-Output ('  headers: ' + (Get-ChildItem (Join-Path $dest 'include\openssl') -Recurse -File | Measure-Object).Count + ' files')
$ver = Join-Path $dest 'include\openssl\opensslv.h'
if (Test-Path $ver) {
    Select-String -Path $ver -Pattern 'OPENSSL_VERSION_(MAJOR|MINOR|PATCH)' | ForEach-Object { Write-Output ('    ' + $_.Line.Trim()) }
}

Write-Output '=== 3. generate import libs from the shipping DLLs ==='
$pairs = @(
    @{ dll = 'libcrypto-3-x64.dll'; lib = 'libcrypto' },
    @{ dll = 'libssl-3-x64.dll';    lib = 'libssl'    }
)
foreach ($p in $pairs) {
    $dllPath = Join-Path $dllDir $p.dll
    if (-not (Test-Path $dllPath)) { Write-Output ('  missing ' + $dllPath); continue }
    $defPath = Join-Path $env:TEMP ($p.lib + '.def')
    $out = & "$env:SystemRoot\System32\cmd.exe" /c "call `"$vcvars`" >nul 2>&1 && dumpbin /nologo /exports `"$dllPath`"" 2>&1
    $names = @()
    foreach ($line in $out) {
        if ($line -match '^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(\S+)\s*$') { $names += $Matches[1] }
    }
    if ($names.Count -eq 0) { Write-Output ('  no exports parsed for ' + $p.dll); continue }
    $def = "LIBRARY $($p.dll)`r`nEXPORTS`r`n" + (($names | Sort-Object -Unique) -join "`r`n") + "`r`n"
    Set-Content -LiteralPath $defPath -Value $def -Encoding ASCII
    $libOut = Join-Path $dest ('lib\' + $p.lib + '.lib')
    & "$env:SystemRoot\System32\cmd.exe" /c "call `"$vcvars`" >nul 2>&1 && lib /nologo /machine:x64 /def:`"$defPath`" /out:`"$libOut`"" 2>&1 |
        Select-Object -Last 3 | ForEach-Object { Write-Output ('    ' + $_) }
    if (Test-Path $libOut) {
        Write-Output ('  ' + $p.lib + '.lib  {0:N0} bytes  ({1} exports)' -f (Get-Item $libOut).Length, $names.Count)
    } else {
        Write-Output ('  FAILED to build ' + $p.lib + '.lib')
    }
}

Write-Output '=== 4. result ==='
$ok = (Test-Path (Join-Path $dest 'include\openssl\ssl.h')) -and
      (Test-Path (Join-Path $dest 'lib\libcrypto.lib')) -and
      (Test-Path (Join-Path $dest 'lib\libssl.lib'))
if ($ok) { Write-Output 'READY'; Get-ChildItem (Join-Path $dest 'lib') | ForEach-Object { Write-Output ('  ' + $_.Name + '  ' + $_.Length) } }
else { Write-Output 'NOT_READY' }
