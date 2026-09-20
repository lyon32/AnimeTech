$ErrorActionPreference = "Continue"
$Container = "v2-telegram-bot-api"
docker stop $Container 2>$null | Out-Null
docker rm $Container 2>$null | Out-Null
if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 1) {
    Write-Host "Container $Container stopped and removed."
} else {
    Write-Host "Nothing to stop (container not found or docker error)."
}