<#
  Installe (une seule fois) les tâches planifiées Windows du projet, pour l'utilisateur courant, sans droits administrateur :

    V2AutomationWorker  le worker (surveillance, téléchargements, publication)
    V2AutomationPanel   le panneau web d'administration (127.0.0.1:8085)
    V2AutomationAdmin   le bot Telegram d'administration

  Chaque tâche démarre à l'ouverture de session et est relancée automatiquement après un plantage (toutes les minutes,
  999 fois). Un arrêt demandé depuis le panneau (« Arrêter le worker ») se termine proprement avec le code 0 :
  la tâche n'est alors PAS relancée ; le bouton « Démarrer le worker » la relance à la demande.

  Usage :   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
  Retrait : powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"      # sans fenêtre console
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$tasks = @(
  @{ Name = "V2AutomationWorker"; Arg = "run-worker" },
  @{ Name = "V2AutomationPanel";  Arg = "serve" },
  @{ Name = "V2AutomationAdmin";  Arg = "run-admin" }
)

foreach ($t in $tasks) {
  if ($Remove) {
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "retirée : $($t.Name)"
    continue
  }
  $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m v2_automation.cli $($t.Arg)" -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
      -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
      -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings `
      -Description "v2_automation ($($t.Arg))" -Force | Out-Null
  Write-Host "installée : $($t.Name)  ->  $($t.Arg)"
}
if (-not $Remove) {
  Write-Host ""
  Write-Host "Terminé. Les tâches démarrent à l'ouverture de session. Pour lancer maintenant :"
  Write-Host "  schtasks /Run /TN V2AutomationWorker"
}
