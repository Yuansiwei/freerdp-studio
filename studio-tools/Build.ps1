$ErrorActionPreference='Stop'
$dep='C:\tools\vcpkg\installed\x64-windows'
if(-not(Test-Path -LiteralPath ($dep+'\include\openssl\ssl.h'))){throw 'OpenSSL dependency is not installed'}
$options=@(
    '-G','Visual Studio 17 2022','-A','x64','-DCMAKE_GENERATOR_INSTANCE=C:\BuildTools',
    ('-DCMAKE_PREFIX_PATH='+$dep),('-DOPENSSL_ROOT_DIR='+$dep),
    '-DWITH_CLIENT_WINDOWS=ON','-DWITH_WIN_CONSOLE=OFF',
    '-DWITH_CLIENT_SDL=OFF','-DWITH_CLIENT_SDL2=OFF','-DWITH_CLIENT_SDL3=OFF',
    '-DWITH_SERVER=OFF','-DWITH_PROXY=OFF','-DWITH_SHADOW=OFF','-DWITH_SAMPLE=OFF',
    '-DWITH_FFMPEG=OFF','-DWITH_SWSCALE=OFF','-DWITH_OPENH264=OFF',
    '-DWITH_OPUS=OFF','-DWITH_JPEG=OFF','-DWITH_AAD=OFF','-DWITH_WEBVIEW=OFF',
    '-DWITH_KRB5=OFF','-DWITH_JSON_DISABLED=ON','-DWITH_SMARTCARD_PCSC=OFF',
    '-DWITH_SMARTCARD_EMULATE=OFF','-DWITH_MANPAGES=OFF','-DBUILD_TESTING=OFF',
    '-DWITH_WINPR_TOOLS=OFF','-DWITH_CLANG_FORMAT=OFF','-DWITH_NATIVE_SSPI=ON','-DCHANNEL_URBDRC=OFF',
    '-DWITH_MEDIA_FOUNDATION=OFF','-DWITH_DSP_FFMPEG=OFF','-DWITH_VIDEO_FFMPEG=OFF','-DWITH_SOXR=OFF',
    '-DWITH_INTERNAL_MD4=ON','-DWITH_INTERNAL_MD5=ON','-DWITH_INTERNAL_RC4=ON'
)
& cmake.exe -S ('C:\Users\13611\Documents\电脑管理\freerdp-98184'+'\upstream') -B ('C:\Users\13611\Documents\电脑管理\freerdp-98184'+'\build') @options
if($LASTEXITCODE -ne 0){throw 'FreeRDP configure failed'}
& cmake.exe --build ('C:\Users\13611\Documents\电脑管理\freerdp-98184'+'\build') --config Release --target wfreerdp --parallel 4
if($LASTEXITCODE -ne 0){throw 'FreeRDP build failed'}
$runtime=Join-Path 'C:\Users\13611\Documents\电脑管理\freerdp-98184' 'runtime'
[void][IO.Directory]::CreateDirectory($runtime)
$artifacts=@(Get-ChildItem -LiteralPath ('C:\Users\13611\Documents\电脑管理\freerdp-98184'+'\build') -Recurse -File | Where-Object {
    $_.Directory.Name -eq 'Release' -and ($_.Extension -eq '.dll' -or $_.Name -eq 'wfreerdp.exe')
})
foreach($artifact in $artifacts){Copy-Item -LiteralPath $artifact.FullName -Destination $runtime -Force}
Get-ChildItem -LiteralPath ($dep+'\bin') -Filter '*crypto*.dll' | Copy-Item -Destination $runtime -Force
Get-ChildItem -LiteralPath ($dep+'\bin') -Filter '*ssl*.dll' | Copy-Item -Destination $runtime -Force
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:exe /platform:x64 /optimize+ ('/out:'+'C:\Users\13611\Documents\电脑管理\freerdp-98184'+'\SendRdpInput.exe') ('C:\Users\13611\Documents\电脑管理\freerdp-98184'+'\SendRdpInput.cs')
if($LASTEXITCODE -ne 0){throw 'Input sender build failed'}
if(-not(Test-Path -LiteralPath ($runtime+'\wfreerdp.exe'))){throw 'Client executable missing'}
'FreeRDP client and input sender built.'
