$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$Volume = "v2-telegram-bot-api-data"
# Image must be the patched build (Dockerfile: IDLE_TIMEOUT 500 -> 7200, tag idle7200-real / latest).
# Upstream closed the HTTP connection 500 s into a long upload.
# DO NOT enable --verbosity>=3 / --log in production: the server then writes the bot token in clear in its log.
$Image = "v2-telegram-bot-api"
$Container = "v2-telegram-bot-api"
$HttpPort = "127.0.0.1:8081"
$StatPort = "127.0.0.1:8082"

if (-not (Test-Path $EnvFile)) {
    Write-Error ".env introuvable dans $Root"
    exit 1
}

# NOTE: /data doit etre un volume Docker nomme (ext4 dans WSL2), PAS un
# bind-mount Windows : le serveur cree des repertoires "../../<userid>:<token>"
# dont le ':' est invalide sur NTFS (crash verify en POC, SIGABRT/PosixError).
$volExists = docker volume inspect $Volume 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    docker volume create $Volume | Out-Null
}

$running = docker ps -a --filter "name=^/$Container$" --format "{{.Names}}"
if ($running) {
    docker rm -f $Container | Out-Null
}

docker run -d --name $Container `
    --env-file $EnvFile `
    -v "${Volume}:/data" `
    -p "${HttpPort}:8081" `
    -p "${StatPort}:8082" `
    $Image `
    --local `
    --http-port=8081 --http-stat-port=8082 `
    --dir=/data --temp-dir=/tmp/v2botapi

if ($LASTEXITCODE -ne 0) {
    Write-Error "docker run failed"
    exit 1
}
Write-Host "Container $Container started (volume $Volume)."
Write-Host "  API  : http://$HttpPort"
Write-Host "  Stats: http://$StatPort"
Write-Host "  Logs : docker logs -f $Container"