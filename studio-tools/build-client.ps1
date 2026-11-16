# build-client.ps1 -- configure and build wfreerdp on this machine.
# Toolchain: C:\BuildTools (MSVC 14.44) + cmake 4.4.5 + ninja 1.13.2 + C:\OpenSSL (3.6.2)
$ErrorActionPreference = 'Continue'
$repo    = 'C:\Users\Studio\Documents\deepseek-harness\default-workspace\freerdp-studio'
$build   = Join-Path $repo 'build'
$vcvars  = 'C:\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
$cmake   = 'C:\Tools\cmake-4.4.5-windows-x86_64\bin\cmake.exe'
$ninjaD  = 'C:\Tools\ninja-v1.13.2'
$ssl     = 'C:\OpenSSL'

Write-Output '=== import MSVC environment ==='
$envLines = & "$env:SystemRoot\System32\cmd.exe" /c "call `"$vcvars`" >nul 2>&1 && set"
$count = 0
foreach ($line in $envLines) {
    if ($line -match '^([^=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        $count++
    }
}
Write-Output ("  imported $count variables")
Write-Output ('  cl: ' + (Get-Command cl.exe -ErrorAction SilentlyContinue).Source)
$env:PATH = $ninjaD + ';' + $env:PATH
Write-Output ('  ninja: ' + (Get-Command ninja.exe -ErrorAction SilentlyContinue).Source)

Write-Output '=== configure ==='
if (Test-Path (Join-Path $build 'CMakeCache.txt')) {
    Remove-Item (Join-Path $build 'CMakeCache.txt') -Force -ErrorAction SilentlyContinue
}
$opts = @(
    '-G', 'Ninja',
    '-DCMAKE_BUILD_TYPE=Release',
    "-DCMAKE_PREFIX_PATH=$ssl",
    "-DOPENSSL_ROOT_DIR=$ssl",
    '-DWITH_OPENSSL=ON',
    '-DWITH_CLIENT_WINDOWS=ON',
    '-DWITH_WIN_CONSOLE=OFF',
    '-DWITH_CLIENT_SDL=OFF', '-DWITH_CLIENT_SDL2=OFF', '-DWITH_CLIENT_SDL3=OFF',
    '-DWITH_SERVER=OFF', '-DWITH_PROXY=OFF', '-DWITH_SHADOW=OFF', '-DWITH_SAMPLE=OFF',
    '-DWITH_FFMPEG=OFF', '-DWITH_SWSCALE=OFF', '-DWITH_OPENH264=OFF',
    '-DWITH_OPUS=OFF', '-DWITH_JPEG=OFF', '-DWITH_AAD=OFF', '-DWITH_WEBVIEW=OFF',
    '-DWITH_KRB5=OFF', '-DWITH_JSON_DISABLED=ON', '-DWITH_SMARTCARD_PCSC=OFF',
    '-DWITH_SMARTCARD_EMULATE=OFF', '-DWITH_MANPAGES=OFF', '-DBUILD_TESTING=OFF',
    '-DWITH_WINPR_TOOLS=OFF', '-DWITH_CLANG_FORMAT=OFF', '-DWITH_NATIVE_SSPI=ON',
    '-DCHANNEL_URBDRC=OFF',
    '-DWITH_MEDIA_FOUNDATION=OFF', '-DWITH_DSP_FFMPEG=OFF', '-DWITH_VIDEO_FFMPEG=OFF',
    '-DWITH_SOXR=OFF',
    '-DWITH_INTERNAL_MD4=ON', '-DWITH_INTERNAL_MD5=ON', '-DWITH_INTERNAL_RC4=ON'
)
& $cmake @opts -S $repo -B $build 2>&1 | Select-Object -Last 40 | ForEach-Object { Write-Output ('  ' + $_) }
$cfg = $LASTEXITCODE
Write-Output ("  configure exit=$cfg")
if ($cfg -ne 0) { Write-Output 'CONFIGURE_FAILED'; exit 1 }

Write-Output '=== build target wfreerdp ==='
& $cmake --build $build --target wfreerdp --parallel 8 2>&1 |
    Select-Object -Last 60 | ForEach-Object { Write-Output ('  ' + $_) }
$bld = $LASTEXITCODE
Write-Output ("  build exit=$bld")

Write-Output '=== artifacts ==='
Get-ChildItem $build -Recurse -File -Include '*.dll', 'wfreerdp.exe' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\client\\Windows\\' } |
    ForEach-Object { Write-Output ('  {0,-28} {1,10:N0}  {2}' -f $_.Name, $_.Length, $_.LastWriteTime.ToString('HH:mm:ss')) }

if ($bld -eq 0) { Write-Output 'BUILD_OK' } else { Write-Output 'BUILD_FAILED' }
