[CmdletBinding()]
param(
    [ValidateSet('Get', 'Set', 'Record', 'Reset')][string]$Action = 'Get',
    [ValidateRange(0, 100)][int]$LocalTargetPercent = 50,
    [bool]$WaitForResults = $true,
    [ValidateRange(1, 86400)][int]$WaitTimeoutSec = 1800,
    [ValidateSet('ganglion', 'local', 'native', 'unavailable', 'none')][string]$SubagentRoute = 'none',
    [switch]$EligibleTask,
    [string]$StatePath,
    [string]$TaskId
)
$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'manage-routing-policy.js'), $Action.ToLowerInvariant())
if ($StatePath) { $arguments += @('--state', $StatePath) }
if ($Action -eq 'Set') {
    $arguments += @('--target', [string]$LocalTargetPercent, '--wait', $WaitForResults.ToString().ToLowerInvariant(), '--timeout', [string]$WaitTimeoutSec)
}
if ($Action -eq 'Record') {
    $arguments += @('--route', $SubagentRoute)
    if ($EligibleTask) { $arguments += '--eligible' }
    if ($TaskId) { $arguments += @('--task-id', $TaskId) }
}
& node @arguments
if ($LASTEXITCODE -ne 0) { throw 'Routing policy operation failed.' }
