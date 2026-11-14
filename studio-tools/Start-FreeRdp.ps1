param(
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$Server,
    [ValidateRange(1,65535)][int]$Port=3389,
    [string]$Username,
    # Omit (0) to derive the session scale from the monitor the pointer is on.
    [ValidateRange(0,500)][int]$ScaleDesktop=0,
    [ValidateRange(1,5)][int]$Attempts=3
)
$ErrorActionPreference='Stop'
# Derive the install root from the script location so a non-ASCII path literal is not needed.
$root = $PSScriptRoot
$exe = Join-Path $root 'runtime\wfreerdp.exe'
$log = Join-Path $root 'client.stderr.log'
if(-not (Test-Path -LiteralPath $exe)){throw 'FreeRDP runtime is not built yet'}
if($Server -notmatch '^[A-Za-z0-9._:-]+$'){throw 'Specify a hostname or IP address'}
$endpoint=$Server+':'+$Port
if($Server.Contains(':')){$endpoint='['+$Server+']:'+$Port}
# +toggle-fullscreen keeps the session on one monitor; the client sizes it to that monitor
# and matches its DPI, so dynamic resolution stays consistent when moving between displays.
$options=@(('/v:'+$endpoint),'/f','+toggle-fullscreen','+dynamic-resolution','+auto-reconnect','/auto-reconnect-max-retries:10','/gdi:sw','/bpp:32','/kbd:unicode:on','/proxy:ignore','/cert:ignore','/log-level:WARN')
if($ScaleDesktop -gt 0){$options+=('/scale-desktop:'+$ScaleDesktop)}
if($Username){
    if($Username.Contains('"')){throw 'Invalid username'}
    $options+=('/u:"'+$Username+'"')
}
# A server that is still tearing down the previous session answers with an activation
# timeout; that is transient, so retry instead of reporting a hard failure.
for($attempt=1;$attempt -le $Attempts;$attempt++){
    if(Test-Path -LiteralPath $log){Remove-Item -LiteralPath $log -Force}
    $proc=Start-Process -FilePath $exe -ArgumentList $options -RedirectStandardError $log -PassThru
    $deadline=(Get-Date).AddSeconds(30)
    while((Get-Date) -lt $deadline){
        Start-Sleep -Milliseconds 500
        $proc.Refresh()
        if($proc.HasExited){break}
        if((Test-Path -LiteralPath $log) -and (Select-String -LiteralPath $log -Pattern 'ACTIVATION_TIMEOUT' -Quiet)){break}
    }
    $proc.Refresh()
    if(-not $proc.HasExited){exit 0}
    $text=if(Test-Path -LiteralPath $log){Get-Content -LiteralPath $log -Raw}else{''}
    if($text -notmatch 'ACTIVATION_TIMEOUT'){
        throw "FreeRDP exited before the session became active (exit $($proc.ExitCode))"
    }
    Start-Sleep -Seconds 2
}
throw "FreeRDP could not reach an active session after $Attempts attempts"
