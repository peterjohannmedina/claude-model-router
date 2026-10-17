[CmdletBinding()]
param([string]$UserRoot, [string]$ClaudeHome, [string]$StateDir, [string]$PythonPath, [switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$routingArguments = @{ Command = $(if ($OpenBrowser) { 'open' } else { 'start' }); PythonPath = $PythonPath }
if ($UserRoot) { $routingArguments.UserRoot = $UserRoot }
if ($ClaudeHome) { $routingArguments.ClaudeHome = $ClaudeHome }
if ($StateDir) { $routingArguments.StateDir = $StateDir }
& (Join-Path $PSScriptRoot 'bootstrap.ps1') @routingArguments
exit $LASTEXITCODE
