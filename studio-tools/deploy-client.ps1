# deploy-client.ps1 -- stop the running client, swap in freshly built DLLs, restart it.
# Keeps a backup in %TEMP%\rdpbridge-backup so a bad build can be rolled back instantly.
#
# The client RUNS from C:\Studio\RdpBridge, not from the repo's dist folder: the repo lives under
# deepseek-harness\default-workspace, which carries a Low integrity label (an agent sandbox).
# A client started from there runs at Low IL and can no longer write its capture log, BMPs or
# open the capture pipe, and Medium-IL tools cannot reach it (2026-11-19). dist stays the
# versioned record and gets the same binaries.
#
# No password lives in this file (the repo is public). The restart arguments come from, in order:
#   1. the command line of the client that is running now (restart exactly as it was started)
#   2. Windows Credential Manager, GENERIC credential TERMSRV/<host>:<port> or TERMSRV/<host>
#      (the format the studio client itself saves; add one with:
#       cmdkey /generic:TERMSRV/172.30.251.1:3390 /user:bridge /pass)
#   3. $env:RDP_BRIDGE_PASSWORD
#   4. an interactive prompt (only when a person is at the console)
# If none works the script stops BEFORE touching the running client.
#
#   .\deploy-client.ps1              # deploy
#   .\deploy-client.ps1 -CheckOnly   # only report where the credentials would come from
param([switch]$CheckOnly)

$ErrorActionPreference = 'Continue'
$repo  = 'C:\Users\Studio\Documents\deepseek-harness\default-workspace\freerdp-studio'
$build = Join-Path $repo 'build'
$dist  = Join-Path $repo 'dist\rdpbridge-3.33.1-20261116'
$runtime = 'C:\Studio\RdpBridge'
$backup = Join-Path $env:TEMP 'rdpbridge-backup'
$names = @('wfreerdp.exe', 'wfreerdp-client3.dll', 'freerdp-client3.dll', 'freerdp3.dll', 'winpr3.dll')
$server = '172.30.251.1:3390'
$user = 'bridge'
$fixedArgs = "/v:$server /u:$user /sec:tls /cert:ignore /kbd:unicode:on /f"

function Get-RunningClientArgs {
    $p = Get-CimInstance Win32_Process -Filter "Name='wfreerdp.exe'" | Select-Object -First 1
    if (-not $p -or -not $p.CommandLine) { return $null }
    $cmd = $p.CommandLine.Trim()
    if ($cmd.StartsWith('"')) { $rest = $cmd.Substring($cmd.IndexOf('"', 1) + 1) }
    else { $rest = $cmd.Substring(([Math]::Max($cmd.IndexOf(' '), 0))) }
    $rest = $rest.Trim()
    if ($rest -match '(^|\s)/p:') { return $rest }
    return $null
}

function Get-StoredPassword {
    if (-not ('StudioDeploy.Cred' -as [type])) {
        Add-Type -Namespace StudioDeploy -Name Cred -MemberDefinition @'
[DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool CredRead(string target, int type, int flags, out System.IntPtr credential);
[DllImport("advapi32.dll")]
public static extern void CredFree(System.IntPtr credential);
[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct CREDENTIAL {
    public int Flags; public int Type; public string TargetName; public string Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public int CredentialBlobSize; public System.IntPtr CredentialBlob; public int Persist;
    public int AttributeCount; public System.IntPtr Attributes; public string TargetAlias; public string UserName;
}
'@
    }
    foreach ($target in @("TERMSRV/$server", ('TERMSRV/' + $server.Split(':')[0]))) {
        $ptr = [IntPtr]::Zero
        if ([StudioDeploy.Cred]::CredRead($target, 1, 0, [ref]$ptr)) {  # 1 = CRED_TYPE_GENERIC
            try {
                $c = [Runtime.InteropServices.Marshal]::PtrToStructure($ptr, [type][StudioDeploy.Cred+CREDENTIAL])
                if ($c.CredentialBlobSize -gt 0) {
                    return [Runtime.InteropServices.Marshal]::PtrToStringUni($c.CredentialBlob, $c.CredentialBlobSize / 2)
                }
            } finally { [StudioDeploy.Cred]::CredFree($ptr) }
        }
    }
    return $null
}

function Resolve-ClientArgs {
    $a = Get-RunningClientArgs
    if ($a) { return @{ Args = $a; Source = 'command line of the running client' } }
    $pw = Get-StoredPassword
    $source = 'Credential Manager'
    if (-not $pw -and $env:RDP_BRIDGE_PASSWORD) { $pw = $env:RDP_BRIDGE_PASSWORD; $source = 'RDP_BRIDGE_PASSWORD' }
    $interactive = [Environment]::UserInteractive -and -not ([Environment]::GetCommandLineArgs() -match '^-NonI')
    if (-not $pw -and $interactive) {
        $secure = Read-Host -AsSecureString "RDP password for $user@$server"
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $pw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $source = 'prompt'
    }
    if (-not $pw) { return $null }
    return @{ Args = ($fixedArgs + ' "/p:' + $pw.Replace('"', '\"') + '"'); Source = $source }
}

Write-Output '=== 0. resolve restart credentials ==='
$launch = Resolve-ClientArgs
if (-not $launch) {
    Write-Output '  no credentials: client not running, nothing in Credential Manager, no RDP_BRIDGE_PASSWORD'
    Write-Output '  nothing was changed. Store one with: cmdkey /generic:TERMSRV/172.30.251.1:3390 /user:bridge /pass'
    exit 2
}
Write-Output ('  source: ' + $launch.Source)
if ($CheckOnly) { exit 0 }

Write-Output '=== 1. backup current runtime binaries ==='
New-Item -ItemType Directory -Force -Path $backup, $runtime | Out-Null
foreach ($n in $names) {
    $p = Join-Path $runtime $n
    if (Test-Path $p) { Copy-Item $p -Destination (Join-Path $backup $n) -Force; Write-Output ('  backed up ' + $n) }
}

Write-Output '=== 2. stop client ==='
$proc = Get-Process wfreerdp -ErrorAction SilentlyContinue
if ($proc) {
    $proc | ForEach-Object { Write-Output ('  stopping pid=' + $_.Id) }
    $proc | Stop-Process -Force
    Start-Sleep -Seconds 2
} else {
    Write-Output '  not running'
}

Write-Output '=== 3. swap binaries (runtime + dist record) ==='
foreach ($n in $names) {
    $src = Get-ChildItem $build -Recurse -File -Filter $n -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $src) { Write-Output ('  no build output for ' + $n); continue }
    foreach ($dir in @($runtime, $dist)) { Copy-Item $src.FullName -Destination (Join-Path $dir $n) -Force }
    $h = (Get-FileHash (Join-Path $runtime $n) -Algorithm SHA256).Hash
    Write-Output ('  {0,-24} {1,10:N0}  {2}' -f $n, (Get-Item (Join-Path $runtime $n)).Length, $h.Substring(0, 16))
}
# the rest of the bundle (OpenSSL, SendRdpInput, docs) must sit next to the exe as well
Get-ChildItem $dist -File | Where-Object { $names -notcontains $_.Name } | ForEach-Object {
    Copy-Item $_.FullName -Destination (Join-Path $runtime $_.Name) -Force
}

Write-Output '=== 4. restart client ==='
$exe = Join-Path $runtime 'wfreerdp.exe'
Start-Process -FilePath $exe -WorkingDirectory $runtime -ArgumentList $launch.Args
Start-Sleep -Seconds 6
$p = Get-Process wfreerdp -ErrorAction SilentlyContinue
if ($p) {
    Write-Output ('  running pid=' + ($p | Select-Object -First 1).Id)
} else {
    Write-Output '  FAILED TO START - rolling back'
    foreach ($n in $names) {
        $b = Join-Path $backup $n
        if (Test-Path $b) { foreach ($dir in @($runtime, $dist)) { Copy-Item $b -Destination (Join-Path $dir $n) -Force } }
    }
    Start-Process -FilePath $exe -WorkingDirectory $runtime -ArgumentList $launch.Args
    Start-Sleep -Seconds 5
    if (Get-Process wfreerdp -ErrorAction SilentlyContinue) { Write-Output '  rolled back, client running' }
    else { Write-Output '  rollback also failed' }
    exit 1
}

Write-Output '=== 5. status ==='
$tool = Join-Path $runtime 'SendRdpInput.exe'
& $tool --target $server --status query
Write-Output ('  exit=' + $LASTEXITCODE)
