<#
.SYNOPSIS
Run the private Luccy comparison from a local DataGov Compose checkout.

.DESCRIPTION
Uses the existing A/A2/C comparison runner and the full golden set: direct Luccy,
repeat Luccy, and DataGov's full chat integration. The default is a dry run with
no paid model calls and no result posts. -Live enables paid calls; results stay
in DataGov's private evaluation store, with no comparison page in the app.

Prepare .env.documents and .env.analyzer as described in docs/document-analyzer.md
and start/sync the local analyzer stack first. For a live comparison, enable the
private evaluation endpoint and configure its token and authorized evaluation
user. This script does not start/recreate services or change access settings.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts/compare-luccy.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts/compare-luccy.ps1 -Live -MaxUsd 40
#>
[CmdletBinding()]
param(
    [switch]$Live,
    [ValidateScript({ $_ -gt 0 -and -not [double]::IsInfinity($_) -and -not [double]::IsNaN($_) })]
    [double]$MaxUsd = 40
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

foreach ($file in @('.env.documents', '.env.analyzer')) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $file) -PathType Leaf)) {
        throw "Missing $file in the DataGov checkout. See docs/document-analyzer.md for local Compose setup."
    }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker with Compose is required for the local comparison.'
}

$composeArgs = @(
    'compose',
    '--env-file', '.env.documents',
    '--env-file', '.env.analyzer',
    '-f', 'docker-compose.yml',
    '-f', 'docker-compose.documents.yml',
    '-f', 'docker-compose.analyzer.yml',
    '--profile', 'analyzer-jobs',
    'run', '--rm', '--no-deps', '-T', 'analyzer-compare',
    'python', '-m', 'luccy_qdrant.compare',
    '--arms', 'A,A2,C',
    '--max-usd', $MaxUsd.ToString('G', [System.Globalization.CultureInfo]::InvariantCulture)
)
if (-not $Live) {
    $composeArgs += '--dry-run'
    Write-Host 'Luccy comparison: dry run; no paid calls and no results posted.'
}
else {
    Write-Host "Luccy comparison: live A/A2/C run; budget USD $($MaxUsd.ToString('G', [System.Globalization.CultureInfo]::InvariantCulture))."
}

Push-Location -LiteralPath $repoRoot
try {
    & docker @composeArgs
    $comparisonExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $comparisonExitCode
