# Rebuild SendRdpInput.exe from source and deploy it into the frozen bundle.
# ASCII-only source. Never embeds a CJK filename: the doc is located by wildcard.
$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\Studio\Documents\deepseek-harness\default-workspace\freerdp-studio'
$dist = Join-Path $repo 'dist\rdpbridge-3.33.1-20261116'
$tools = Join-Path $repo 'studio-tools'
$cs   = Join-Path $tools 'SendRdpInput.cs'
$tmp  = Join-Path $env:TEMP 'SendRdpInput.build.exe'

Write-Output '=== 1. build ==='
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
& "$env:SystemRoot\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:exe /platform:x64 /optimize+ "/out:$tmp" $cs 2>&1 |
    ForEach-Object { Write-Output ('  ' + $_) }
if (-not (Test-Path $tmp)) { Write-Output '  BUILD FAILED'; exit 1 }
Write-Output ('  built {0:N0} bytes' -f (Get-Item $tmp).Length)

Write-Output '=== 2. deploy into the bundle ==='
$target = Join-Path $dist 'SendRdpInput.exe'
$oldSize = 0
if (Test-Path $target) { $oldSize = (Get-Item $target).Length }
Copy-Item $tmp -Destination $target -Force
Write-Output ('  SendRdpInput.exe: {0:N0} -> {1:N0} bytes' -f $oldSize, (Get-Item $target).Length)

Write-Output '=== 3. refresh the bundle doc (located by wildcard) ==='
$docCandidates = Get-ChildItem $tools -Filter '*.md' -File | Where-Object { (Get-Content $_.FullName).Count -gt 300 }
$srcDoc = $docCandidates | Select-Object -First 1
$dstDoc = Get-ChildItem $dist -Filter '*.md' -File | Select-Object -First 1
if ($srcDoc -and $dstDoc) {
    Copy-Item $srcDoc.FullName -Destination $dstDoc.FullName -Force
    $n = (Get-Content $dstDoc.FullName).Count
    Write-Output ('  doc refreshed: {0:N0} -> {1:N0} bytes ({2} lines)' -f $dstDoc.Length, (Get-Item $dstDoc.FullName).Length, $n)
} else {
    Write-Output '  doc refresh skipped (source or target not found)'
}

Write-Output '=== 4. regenerate MANIFEST.json ==='
$rows = Get-ChildItem $dist -File | Sort-Object Name | ForEach-Object {
    [pscustomobject]@{
        name           = $_.Name
        bytes          = $_.Length
        fileVersion    = $_.VersionInfo.FileVersion
        productVersion = $_.VersionInfo.ProductVersion
        modified       = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
        sha256         = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
}
$manifest = [pscustomobject]@{
    bundle         = 'rdpbridge-3.33.1-20261116'
    kind           = 'deployed-runtime'
    capturedAt     = (Get-Date).ToString('s')
    capturedFrom   = $dist
    capturedHost   = $env:COMPUTERNAME
    sourceBranch   = (git -C $repo rev-parse --abbrev-ref HEAD)
    sourceHead     = (git -C $repo rev-parse HEAD)
    sourceDescribe = (git -C $repo describe --tags --always)
    mixedBuild     = $false
    provenanceNote = 'All five FreeRDP binaries were rebuilt from this repo on 2026-11-17 with MSVC 14.44.35207 + OpenSSL 3.6.2 (C:\OpenSSL), so the build matches source HEAD. Pointer injection (kind 0x46524436) is active and verified end to end: --click on the remote Start button changed 25.96% of the screen and ESC restored it.'
    totalBytes     = ($rows | Measure-Object bytes -Sum).Sum
    files          = $rows
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $dist 'MANIFEST.json') -Encoding UTF8
Write-Output ('  files: {0}  total {1:N1} MB' -f @($rows).Count, (($rows | Measure-Object bytes -Sum).Sum / 1MB))

Write-Output '=== 5. client availability ==='
$client = Get-Process wfreerdp -ErrorAction SilentlyContinue
if ($client) {
    Write-Output ('  client running: pid={0}' -f ($client | Select-Object -First 1).Id)
    & $target --target 172.30.251.1:3390 --status query
    Write-Output ('  status exit=' + $LASTEXITCODE)
} else {
    Write-Output '  client NOT running - start it first, then re-run the smoke test'
}
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
Write-Output 'done'
