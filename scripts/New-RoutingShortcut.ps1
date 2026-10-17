[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Path,
      [Parameter(Mandatory=$true)][string]$Target,
      [Parameter(Mandatory=$true)][string]$Arguments)
$ErrorActionPreference = 'Stop'
$routingShell = New-Object -ComObject WScript.Shell
$routingShortcut = $routingShell.CreateShortcut($Path)
$routingShortcut.TargetPath = $Target
$routingShortcut.Arguments = $Arguments
$routingShortcut.WindowStyle = 7
$routingShortcut.Description = 'Open the local Codex model-routing dashboard'
$routingShortcut.Save()
