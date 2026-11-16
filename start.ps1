# fx-never-sleeps Discord sandbox - runs in the locked-down "fx-sandbox" WSL distro.
#
#   .\start.ps1           copy this folder into the distro, build, and (re)start everything
#   .\start.ps1 -Status   show container status
#   .\start.ps1 -Logs     follow the logs (Ctrl+C to stop following)
#   .\start.ps1 -Stop     stop the containers (volumes and the repo clone are kept)
#
# The distro has no access to C:, so this script copies the project in with tar.
param([switch]$Status, [switch]$Logs, [switch]$Stop)

# Native tools (wsl, docker) write progress to stderr; Windows PowerShell 5.1
# would turn that into errors under 'Stop'. Exit codes are checked instead.
$ErrorActionPreference = 'Continue'
$distro = 'fx-sandbox'
$remote = '/opt/fx-sandbox'
$here = $PSScriptRoot

# A plain function on purpose: with a param() block PowerShell would bind
# "-d" to its own -Debug switch instead of passing it to docker compose.
function Invoke-Compose {
    & wsl.exe -d $distro -u root --cd $remote -- docker compose @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') failed" }
}

function Start-KeepAlive {
    # WSL stops an idle distro (and its containers) when no Windows-launched
    # process is using it. A hidden "sleep infinity" keeps it running.
    $running = Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" |
        Where-Object { $_.CommandLine -like "*-d $distro*sleep infinity*" }
    if (-not $running) {
        Start-Process -FilePath wsl.exe -ArgumentList @('-d', $distro, '-u', 'root', '--cd', '/', '--', 'sleep', 'infinity') -WindowStyle Hidden
    }
}

if ($Status) { Invoke-Compose ps; exit }
if ($Logs) { Invoke-Compose logs -f --tail 100; exit }
if ($Stop) { Invoke-Compose stop; exit }

# Check secrets before deploying.
$required = @{
    'secrets\proxy.env'  = @('DEEPSEEK_API_KEY')
    'secrets\agent.env'  = @('REPO', 'GH_TOKEN')
    'secrets\bridge.env' = @('DISCORD_BOT_TOKEN', 'DISCORD_CHANNEL_ID')
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

Write-Host "Copying project into $distro`:$remote ..."
& wsl.exe -d $distro -u root --cd / -- mkdir -p $remote
& wsl.exe -d $distro -u root --cd / -- find $remote -mindepth 1 -delete
# cmd.exe pipes raw bytes; Windows PowerShell 5.1 would corrupt the tar stream.
cmd.exe /c "tar -cf - --exclude=.git --exclude=__pycache__ -C `"$here`" . | wsl.exe -d $distro -u root --cd / -- tar -xf - -C $remote"
if ($LASTEXITCODE -ne 0) { throw 'Copying the project into the distro failed.' }
& wsl.exe -d $distro -u root --cd / -- chmod 700 "$remote/secrets"

Start-KeepAlive
Write-Host 'Building and starting containers ...'
Invoke-Compose up -d --build --remove-orphans
Invoke-Compose ps
