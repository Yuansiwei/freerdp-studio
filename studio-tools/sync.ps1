# sync.ps1 -- keep this repo and the studio remote from diverging again.
#
#   .\studio-tools\sync.ps1            # report only
#   .\studio-tools\sync.ps1 -Push      # push when local is strictly ahead
#
# Exit codes: 0 in sync / ready to push, 1 diverged, 2 remote ahead (pull first),
#             3 push failed.
param(
    [switch]$Push,
    [string]$Remote = 'studio',
    [string]$RemoteBranch = 'main'
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path $PSScriptRoot -Parent
$tracking = "$Remote/$RemoteBranch"

function Run([string[]]$argv) {
    $out = & git -C $repo @argv 2>&1
    return ($out | Out-String).Trim()
}

$fetch = Run @('fetch', $Remote, '--prune')
if ($LASTEXITCODE -ne 0) {
    Write-Host "fetch failed: $fetch"
    exit 1
}

$head = Run @('rev-parse', '--short', 'HEAD')
$remoteSha = Run @('rev-parse', '--short', $tracking)
if (-not $remoteSha) {
    Write-Host "remote ref $tracking not found"
    exit 1
}

$ahead = [int](Run @('rev-list', '--count', "$tracking..HEAD"))
$behind = [int](Run @('rev-list', '--count', "HEAD..$tracking"))

& git -C $repo merge-base --is-ancestor $tracking HEAD 2>$null
$remoteIsAncestor = ($LASTEXITCODE -eq 0)
& git -C $repo merge-base --is-ancestor HEAD $tracking 2>$null
$localIsAncestor = ($LASTEXITCODE -eq 0)

Write-Host "local  HEAD      : $head ($(Run @('log','-1','--format=%s')))"
Write-Host "remote $tracking : $remoteSha"
Write-Host "ahead=$ahead behind=$behind"

if ($remoteIsAncestor -and -not $localIsAncestor) {
    if ($ahead -eq 0) { Write-Host 'already in sync'; exit 0 }
    Write-Host "clean fast-forward: $ahead commit(s) ready to push"
    if (-not $Push) { Write-Host 'run with -Push to upload'; exit 0 }
    & git -C $repo push --progress $Remote "HEAD:refs/heads/$RemoteBranch" 2>&1 | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) { Write-Host 'push failed (credentials?)'; exit 3 }
    Write-Host 'pushed'
    exit 0
}

if ($localIsAncestor -and -not $remoteIsAncestor) {
    Write-Host "remote is ahead by $behind -- fast-forward pull available"
    if ($behind -gt 0) {
        & git -C $repo merge --ff-only $tracking 2>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) { Write-Host 'ff merge failed'; exit 2 }
        Write-Host 'pulled'
    }
    exit 0
}

if ($remoteIsAncestor -and $localIsAncestor) {
    Write-Host 'already in sync'
    exit 0
}

Write-Host ''
Write-Host 'DIVERGED -- no common history path, do not merge blindly:'
Write-Host ('  only local  : ' + $ahead + ' commit(s)')
& git -C $repo log --oneline "$tracking..HEAD" 2>&1 | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" }
Write-Host ('  only remote : ' + $behind + ' commit(s)')
& git -C $repo log --oneline "HEAD..$tracking" 2>&1 | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" }
Write-Host ''
Write-Host 'Resolution: cherry-pick the remote-only commits, then run:'
Write-Host '  git merge -s ours --no-ff --allow-unrelated-histories -m "unify history" ' + $tracking
exit 1
