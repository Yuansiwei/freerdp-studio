# Persistent Windows.Media.Ocr worker (one process, many requests).
#
# Protocol (lines of ASCII on both sides; images never touch the disk):
#   stdout first line : {"ready":true,"lang":"zh-Hans-CN","max_dim":10000}
#   stdin, in-memory  : {"id":7,"w":1920,"h":1080,"scale":0.5}
#                       <base64 of w*h Gray8 bytes, one line>
#   stdin, from file  : {"id":8,"path":"C:\\...\\image.png","scale":1}   (manual use)
#   stdout           : {"id":7,"ok":true,"ms":312.4,"w":1920,"h":1080,
#                       "lines":[{"text":"...","box":[x,y,w,h]}, ...]}
# Boxes are divided by "scale", so they are in the coordinates of the original
# (unscaled) frame, i.e. remote-session pixels usable with SendRdpInput --click.
# (Re-reading one overwritten file through StorageFile proved unreliable, hence Gray8.)
param([string]$Lang = 'zh-Hans-CN')

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Write-Reply($obj) {
    $json = $obj | ConvertTo-Json -Compress -Depth 6
    # Escape everything outside ASCII so the pipe never depends on code pages.
    $json = [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $script:asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    [void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    [void][Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
    [void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    [void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    [void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    [void][Windows.Graphics.Imaging.BitmapPixelFormat, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    [void][Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
    [void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language $Lang))
}
catch {
    Write-Reply @{ ready = $false; error = $_.Exception.Message }
    exit 3
}
if (-not $engine) {
    Write-Reply @{ ready = $false; error = "no OCR engine for language $Lang" }
    exit 3
}

function Await($op, [Type]$type) {
    $task = $script:asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

Write-Reply @{ ready = $true; lang = $Lang; max_dim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension }

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if (-not $line.Trim()) { continue }
    $id = $null
    try {
        $req = $line | ConvertFrom-Json
        $id = $req.id
        $scale = if ($req.scale) { [double]$req.scale } else { 1.0 }
        if ($req.path) {
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync([string]$req.path)) ([Windows.Storage.StorageFile])
            $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
            try {
                $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
                $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            }
            finally { $stream.Dispose() }
        }
        else {
            $payload = [Console]::In.ReadLine()
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $w = [int]$req.w; $h = [int]$req.h
            $bytes = [Convert]::FromBase64String($payload)
            if ($w -le 0 -or $h -le 0 -or $bytes.Length -ne $w * $h) { throw "expected $w x $h Gray8 bytes, got $($bytes.Length)" }
            $buffer = [System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions]::AsBuffer($bytes)
            $bitmap = [Windows.Graphics.Imaging.SoftwareBitmap]::CreateCopyFromBuffer(
                $buffer, [Windows.Graphics.Imaging.BitmapPixelFormat]::Gray8, $w, $h)
        }
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = New-Object System.Collections.Generic.List[object]
        foreach ($l in $result.Lines) {
            $x0 = [double]::MaxValue; $y0 = [double]::MaxValue; $x1 = 0.0; $y1 = 0.0
            foreach ($w in $l.Words) {
                $r = $w.BoundingRect
                if ($r.X -lt $x0) { $x0 = $r.X }
                if ($r.Y -lt $y0) { $y0 = $r.Y }
                if ($r.X + $r.Width -gt $x1) { $x1 = $r.X + $r.Width }
                if ($r.Y + $r.Height -gt $y1) { $y1 = $r.Y + $r.Height }
            }
            $box = @([int][math]::Round($x0 / $scale), [int][math]::Round($y0 / $scale),
                [int][math]::Round(($x1 - $x0) / $scale), [int][math]::Round(($y1 - $y0) / $scale))
            $lines.Add(@{ text = $l.Text; box = $box })
        }
        $sw.Stop()
        Write-Reply @{ id = $id; ok = $true; ms = [math]::Round($sw.Elapsed.TotalMilliseconds, 1)
            w = $bitmap.PixelWidth; h = $bitmap.PixelHeight; lines = $lines.ToArray() }
        $bitmap.Dispose()
    }
    catch {
        $inner = $_.Exception
        while ($inner.InnerException) { $inner = $inner.InnerException }
        Write-Reply @{ id = $id; ok = $false; error = $inner.Message; hresult = ('0x{0:X8}' -f $inner.HResult) }
    }
}
