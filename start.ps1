# opencord - build, deploy and manage the containers on Windows (Docker Desktop).
#
#   .\start.ps1           build and (re)start everything from this folder
#   .\start.ps1 -Status   show container status and which commit each one runs
#   .\start.ps1 -Logs     follow the logs (Ctrl+C to stop following)
#   .\start.ps1 -Stop     stop the containers (volumes and the sandbox are kept)
#
# start.sh does the same on macOS and Linux.
# Docker Desktop's dashboard shows the same containers under the "opencord"
# project, with their logs, stats and an "opencord.revision" label naming the commit.
param([switch]$Status, [switch]$Logs, [switch]$Stop)

# Native tools (docker, git) write progress to stderr; Windows PowerShell 5.1
# would turn that into errors under 'Stop'. Exit codes are checked instead.
$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot

# A plain function on purpose: with a param() block PowerShell would bind
# "-d" to its own -Debug switch instead of passing it to docker compose.
function Invoke-Compose {
    & docker compose --project-directory $here -f (Join-Path $here 'compose.yaml') @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') failed" }
}

# The commit this folder is at, with "-dirty" if it has uncommitted changes.
function Get-Revision {
    $rev = & git -C $here rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $rev) { return 'unknown' }
    if (& git -C $here status --porcelain 2>$null) { $rev += '-dirty' }
    return $rev
}

& docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not running. Start it and try again.' }

if ($Status) {
    $ids = Invoke-Compose ps -a -q
    if (-not $ids) { Write-Host 'No containers. Run .\start.ps1 to deploy.'; exit }
    $local = Get-Revision
    $containers = & docker inspect $ids | Out-String | ConvertFrom-Json
    $containers | ForEach-Object {
        $deployed = $_.Config.Labels.'opencord.revision'
        [pscustomobject]@{
            Service  = $_.Config.Labels.'com.docker.compose.service'
            State    = if ($_.State.Health) { "$($_.State.Status) ($($_.State.Health.Status))" } else { $_.State.Status }
            Since    = ([datetime]$_.State.StartedAt).ToLocalTime().ToString('yyyy-MM-dd HH:mm')
            Revision = $deployed
            UpToDate = $deployed -eq $local -and $local -notlike '*-dirty' -and $local -ne 'unknown'
        }
    } | Sort-Object Service | Format-Table -AutoSize
    Write-Host "Local folder is at $local."
    exit
}
if ($Logs) { Invoke-Compose logs -f --tail 100; exit }
if ($Stop) { Invoke-Compose stop; exit }

# Check secrets before deploying.
$required = @{
    'secrets\proxy.env'  = @('DEEPSEEK_API_KEY')
    'secrets\bridge.env' = @('DISCORD_BOT_TOKEN')
    'secrets\shared.env' = @('OPENCODE_SERVER_PASSWORD')
}
foreach ($file in $required.Keys) {
    $path = Join-Path $here $file
    if (-not (Test-Path $path)) { throw "Missing $file. Copy it from secrets.example and fill it in." }
    $content = Get-Content $path -Raw
    foreach ($key in $required[$file]) {
        if ($content -notmatch "(?m)^$key=(?!replace-me)\S+") {
            Write-Warning "$file`: $key is not set yet. That part will not work until you fill it in."
        }
    }
}

# compose.yaml puts this on every container as the "opencord.revision" label. A new
# value recreates the containers, so the label always names what is running.
$env:OPENCORD_REVISION = Get-Revision
Write-Host "Building and starting containers at $env:OPENCORD_REVISION ..."
Invoke-Compose up -d --build --remove-orphans
Invoke-Compose ps
