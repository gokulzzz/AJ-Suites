param(
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$TaskName = 'AJ Suites KSEB Monthly Bill Fetch'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvFile = Join-Path $ProjectRoot '.env'
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed scheduled task '$TaskName' if it existed."
    exit 0
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Create $EnvFile from .env.example and enter KSEB settings before registering the task."
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python was not found at $PythonExe. Create the project virtual environment first."
}
$FetchDay = 15
$DaySetting = Select-String -LiteralPath $EnvFile -Pattern '^\s*KSEB_FETCH_DAY\s*=\s*(\d+)\s*$' | Select-Object -First 1
if ($DaySetting -and $DaySetting.Matches[0].Groups[1].Value -match '^\d+$') {
    $FetchDay = [int]$DaySetting.Matches[0].Groups[1].Value
}
if ($FetchDay -lt 1 -or $FetchDay -gt 28) {
    throw 'KSEB_FETCH_DAY in .env must be from 1 to 28.'
}

$TaskService = New-Object -ComObject 'Schedule.Service'
$TaskService.Connect()
$TaskFolder = $TaskService.GetFolder('\')
$TaskDefinition = $TaskService.NewTask(0)
$TaskDefinition.RegistrationInfo.Description = 'Fetch the latest bills for all linked KSEB WSS services into the AJ Suites dashboard.'
$TaskPrincipal = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$TaskDefinition.Principal.UserId = $TaskPrincipal
$TaskDefinition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN; the task uses the current user's .env access.
$TaskDefinition.Principal.RunLevel = 0 # TASK_RUNLEVEL_LUA (limited)

$Now = Get-Date
$StartBoundary = (Get-Date -Year $Now.Year -Month $Now.Month -Day 1 -Hour 9 -Minute 0 -Second 0).AddDays($FetchDay - 1)
if ($StartBoundary -le $Now) {
    $StartBoundary = $StartBoundary.AddMonths(1)
}
$Trigger = $TaskDefinition.Triggers.Create(4) # TASK_TRIGGER_MONTHLY
$Trigger.StartBoundary = $StartBoundary.ToString('s')
$Trigger.DaysOfMonth = [uint32][math]::Pow(2, $FetchDay - 1)
$Trigger.MonthsOfYear = 4095 # every month
$Trigger.Enabled = $true

$Action = $TaskDefinition.Actions.Create(0) # TASK_ACTION_EXEC
$Action.Path = $PythonExe
$Action.Arguments = '-m rental_automation --fetch-kseb'
$Action.WorkingDirectory = $ProjectRoot
$TaskDefinition.Settings.StartWhenAvailable = $true
$TaskDefinition.Settings.ExecutionTimeLimit = 'PT20M'
$TaskDefinition.Settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
$TaskDefinition.Settings.Enabled = $true

$TaskFolder.RegisterTaskDefinition(
    $TaskName,
    $TaskDefinition,
    6, # TASK_CREATE_OR_UPDATE
    $TaskPrincipal,
    $null,
    3, # TASK_LOGON_INTERACTIVE_TOKEN
    $null
) | Out-Null

Write-Output "Registered '$TaskName' for day $FetchDay at 09:00 when the current Windows account is signed in. It reads credentials from the project .env file."
