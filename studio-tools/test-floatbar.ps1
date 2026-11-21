# test-floatbar.ps1 -- live check: a connection bar dragged sideways stays where it was dropped.
#
# Drives the running client's floatbar with window messages (button down, move, button up; the
# pointer itself never moves and the drag stays horizontal, so fullscreen is never left), waits
# for the drop handling (re-bind to the display -> resize -> re-place), then reads the bar's
# position back. Finally drags it back to where it started.
#
#   .\test-floatbar.ps1            # default: 300 px to the left and back
#   .\test-floatbar.ps1 -Dx 200
param([int]$Dx = 300, [int]$SettleMs = 1500)

$ErrorActionPreference = 'Stop'
if (-not ('StudioFloatbar.W' -as [type])) {
    Add-Type -Namespace StudioFloatbar -Name W -MemberDefinition @'
public delegate bool EnumProc(System.IntPtr hwnd, System.IntPtr lParam);
[DllImport("user32.dll")] public static extern bool EnumChildWindows(System.IntPtr parent, EnumProc cb, System.IntPtr lParam);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(System.IntPtr hwnd, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] public static extern bool GetWindowRect(System.IntPtr hwnd, out RECT r);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr hwnd);
[DllImport("user32.dll")] public static extern System.IntPtr SendMessage(System.IntPtr hwnd, int msg, System.IntPtr w, System.IntPtr l);
[DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(System.IntPtr ctx);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
public static System.IntPtr FindChild(System.IntPtr parent, string cls) {
    System.IntPtr found = System.IntPtr.Zero;
    EnumChildWindows(parent, (h, l) => {
        var sb = new System.Text.StringBuilder(64); GetClassName(h, sb, 64);
        if (sb.ToString() == cls) { found = h; return false; }
        return true; }, System.IntPtr.Zero);
    return found;
}
public static System.IntPtr Pt(int x, int y) { return (System.IntPtr)(((y & 0xFFFF) << 16) | (x & 0xFFFF)); }
'@
}
$W = [StudioFloatbar.W]
# Physical pixels, like the client: a DPI-unaware process gets scaled window rectangles, and the
# message coordinates would then not match the bar's own client coordinates.
[void]$W::SetProcessDpiAwarenessContext([IntPtr]-4)   # PER_MONITOR_AWARE_V2

$procs = @(Get-Process wfreerdp -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
if ($procs.Count -ne 1) { throw "expected one running wfreerdp window, found $($procs.Count)" }
$bar = $W::FindChild($procs[0].MainWindowHandle, 'floatbar')
if ($bar -eq [IntPtr]::Zero) { throw 'no floatbar window (is the bar enabled for this client?)' }

function Get-BarLeft {
    $r = New-Object StudioFloatbar.W+RECT
    [void]$W::GetWindowRect($bar, [ref]$r)
    return $r
}

# One drag: press in the middle of the bar (title text, away from buttons and resize grips),
# move by $by on the same row, release. Returns the bar's left edge before and after.
function Invoke-Drag([int]$by) {
    $r = Get-BarLeft
    $x = [int](($r.Right - $r.Left) / 2); $y = [int](($r.Bottom - $r.Top) / 2)
    if ($y -lt 1) { $y = 1 }
    [void]$W::SendMessage($bar, 0x0201, [IntPtr]1, $W::Pt($x, $y))           # WM_LBUTTONDOWN
    [void]$W::SendMessage($bar, 0x0200, [IntPtr]1, $W::Pt($x + $by, $y))     # WM_MOUSEMOVE
    $moved = (Get-BarLeft).Left    # read before the drop: the drop handling re-places it at once
    [void]$W::SendMessage($bar, 0x0202, [IntPtr]0, $W::Pt($x + $by, $y))     # WM_LBUTTONUP
    Start-Sleep -Milliseconds $SettleMs
    return [pscustomobject]@{ before = $r.Left; moved = $moved; settled = (Get-BarLeft).Left }
}

$start = (Get-BarLeft).Left
if ($start -lt $Dx) { $Dx = [Math]::Max(1, $start) }   # room to slide left without hitting the edge
$a = Invoke-Drag (-$Dx)
$b = Invoke-Drag ($Dx)
"bar start=$start  drag -$Dx -> moved=$($a.moved) settled=$($a.settled)  drag +$Dx -> moved=$($b.moved) settled=$($b.settled)"
$ok = $true
if ($a.moved -ne $a.before - $Dx) { "FAIL: the drag did not move the bar by -$Dx (it went to $($a.moved))"; $ok = $false }
if ($a.settled -ne $a.moved) { "FAIL: dropped at $($a.moved), but it went to $($a.settled) after ${SettleMs} ms"; $ok = $false }
if ($b.settled -ne $start) { "FAIL: could not drag it back to $start (now $($b.settled))"; $ok = $false }

# Past the left edge: the captured pointer ends up left of the bar (negative x). Read as an
# unsigned word that became ~65000 and threw the bar to the right edge; it must stop at 0.
$c = Invoke-Drag (-($start + 200))
$d = Invoke-Drag ($start)
"past the left edge -> moved=$($c.moved) settled=$($c.settled)  back +$start -> settled=$($d.settled)"
if ($c.moved -ne 0 -or $c.settled -ne 0) { "FAIL: dragged past the left edge, the bar went to $($c.moved) / $($c.settled) instead of 0"; $ok = $false }
if ($d.settled -ne $start) { "FAIL: could not drag it back to $start (now $($d.settled))"; $ok = $false }

if ($ok) { 'PASS: the bar stays where it is dropped, stops at the screen edge, and is back at its start now'; exit 0 }
exit 1
