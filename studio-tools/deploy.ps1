# 把 build 产物部署到 runtime 与 release，并校验确实生效。
# 存在被占用/复制失败时直接报错，避免悄悄留下旧 exe。
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'build'))) { $root = $PSScriptRoot }
$build = Join-Path $root 'build'
$runtime = Join-Path $root 'runtime'
$release = Join-Path $root 'release\studio-1.0'

$exe = Get-ChildItem $build -Recurse -File -Filter 'wfreerdp.exe' |
    Where-Object { $_.Directory.Name -eq 'Release' } | Select-Object -First 1
if (-not $exe) { throw "找不到 build 里的 wfreerdp.exe" }

$dlls = Get-ChildItem $build -Recurse -File -Filter '*.dll' |
    Where-Object { $_.Directory.Name -eq 'Release' }

$targets = @($runtime, $release) | Where-Object { Test-Path $_ }
if (-not $targets) { throw "runtime/release 目录都不存在" }

foreach ($target in $targets) {
    foreach ($item in @($exe) + $dlls) {
        $dest = Join-Path $target $item.Name
        try {
            Copy-Item -LiteralPath $item.FullName -Destination $dest -Force
        }
        catch {
            throw ("复制失败（可能被运行中的客户端占用）：{0} -> {1}`n先关闭正在运行的客户端再部署。`n{2}" -f $item.Name, $target, $_.Exception.Message)
        }
    }
    $copied = Get-Item (Join-Path $target 'wfreerdp.exe')
    if ($copied.Length -ne $exe.Length) {
        throw ("校验失败：{0} 的 wfreerdp.exe 大小 {1} 与构建产物 {2} 不一致" -f $target, $copied.Length, $exe.Length)
    }
    Write-Output ("已部署 {0}  (wfreerdp.exe {1} 字节, {2})" -f $target, $copied.Length, $copied.LastWriteTime)
}

Write-Output "部署完成。"
