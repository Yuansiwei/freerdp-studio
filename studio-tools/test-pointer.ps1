# test-pointer.ps1 -- verify kind 0x46524436 (pointer injection) end to end.
# Clicks the remote Start button, measures the pixel delta, then closes the menu with ESC.
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Drawing
# The client runs from C:\Studio\RdpBridge (outside the Low-integrity deepseek-harness sandbox);
# SendRdpInput only targets a client started from its own folder, so use that copy.
$runtime = 'C:\Studio\RdpBridge'
$tool = Join-Path $runtime 'SendRdpInput.exe'
$tgt  = '172.30.251.1:3390'
$out  = 'C:\Users\Studio\Documents\deepseek-harness\default-workspace\freerdp-diag'

function Cap([string]$tag) {
    $b = Join-Path $env:TEMP ("ptr-" + $tag + ".bmp")
    Remove-Item $b -Force -ErrorAction SilentlyContinue
    & $tool --target $tgt --capture $b | Out-Null
    if (-not (Test-Path $b)) { throw "capture failed: $tag" }
    return $b
}
function DiffPct([string]$bmpA, [string]$bmpB) {
    $ia = [System.Drawing.Bitmap]::FromFile($bmpA)
    $ib = [System.Drawing.Bitmap]::FromFile($bmpB)
    $changed = 0; $n = 0
    for ($y = 0; $y -lt $ia.Height; $y += 16) {
        for ($x = 0; $x -lt $ia.Width; $x += 16) {
            $pa = $ia.GetPixel($x, $y); $pb = $ib.GetPixel($x, $y)
            $d = [math]::Abs($pa.R-$pb.R) + [math]::Abs($pa.G-$pb.G) + [math]::Abs($pa.B-$pb.B)
            if ($d -gt 24) { $changed++ }
            $n++
        }
    }
    $ia.Dispose(); $ib.Dispose()
    return [math]::Round(100.0 * $changed / $n, 2)
}

Write-Output '=== clean state ==='
& $tool --target $tgt --key ESC | Out-Null
Start-Sleep -Milliseconds 700
$b1 = Cap 'before'
$img = [System.Drawing.Bitmap]::FromFile($b1)
Write-Output ("  baseline {0}x{1}" -f $img.Width, $img.Height)
$img.Dispose()

Write-Output '=== pointer: move ==='
& $tool --target $tgt --move 100,100
Write-Output ('  exit=' + $LASTEXITCODE)
Start-Sleep -Milliseconds 400

Write-Output '=== pointer: click Start button (30,2080) ==='
& $tool --target $tgt --click 30,2080
Write-Output ('  exit=' + $LASTEXITCODE)
Start-Sleep -Milliseconds 1200

$b2 = Cap 'after-click'
$d1 = DiffPct $b1 $b2
Write-Output ("  screen delta after click: {0}%" -f $d1)

$png = Join-Path $out 'pointer-after-click.png'
$i = [System.Drawing.Bitmap]::FromFile($b2)
$i.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
$i.Dispose()
Write-Output ('  saved ' + $png)

Write-Output '=== close the menu ==='
& $tool --target $tgt --key ESC | Out-Null
Start-Sleep -Milliseconds 1000
$b3 = Cap 'after-esc'
$d2 = DiffPct $b1 $b3
Write-Output ("  delta vs baseline after ESC: {0}%" -f $d2)

Remove-Item (Join-Path $env:TEMP 'ptr-*.bmp') -Force -ErrorAction SilentlyContinue
Write-Output 'done'
