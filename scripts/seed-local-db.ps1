<#
    seed-local-db.ps1 — dump PRODUCTION Postgres into the local Docker 'db' service.

    What it does (production is only ever READ):
      1. Reads PROD_DB_* (source) and DB_* (local target) from backend/.env.
      2. Makes sure the local 'db' container is up and healthy.
      3. Runs pg_dump against production inside a throwaway postgres:16 container
         (no need for psql/pg_dump on your host) -> .seed/prod.sql.
      4. Restores that dump into the local 'db' container.

    After this, the app (DB_HOST=db) runs entirely against the local copy, so
    every ETL write stays local and never touches production.

    Usage (from anywhere):   pwsh scripts/seed-local-db.ps1
    Re-run any time to refresh the local copy / pull fresh credentials.
#>
$ErrorActionPreference = 'Stop'

# Pin the Postgres client image. Must be >= production's major version, or
# pg_dump refuses to dump. Production is currently Postgres 18.
$PG_IMAGE = 'postgres:18'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$envFile  = Join-Path $repoRoot 'backend\.env'
$seedDir  = Join-Path $repoRoot '.seed'
$dumpFile = Join-Path $seedDir 'prod.sql'

if (-not (Test-Path $envFile)) { throw "Cannot find $envFile" }

# ── Parse backend/.env (simple KEY=VALUE, ignores comments/blanks) ──────────
$cfg = @{}
foreach ($line in Get-Content $envFile) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
    $i = $t.IndexOf('=')
    $cfg[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
}

function Need($key) {
    if (-not $cfg.ContainsKey($key) -or -not $cfg[$key]) { throw "Missing $key in backend/.env" }
    return $cfg[$key]
}

# Source = production (read-only), Target = local Docker container.
$prodHost = Need 'PROD_DB_HOST'; $prodPort = Need 'PROD_DB_PORT'
$prodUser = Need 'PROD_DB_USER'; $prodName = Need 'PROD_DB_NAME'; $prodPass = Need 'PROD_DB_PASSWORD'
$locUser  = Need 'DB_USER';      $locName  = Need 'DB_NAME'

New-Item -ItemType Directory -Force -Path $seedDir | Out-Null

Push-Location $repoRoot
try {
    # ── 1. Ensure the local db container is up and accepting connections ────
    Write-Host '==> Starting local db container...' -ForegroundColor Cyan
    docker compose up -d db
    if ($LASTEXITCODE -ne 0) { throw 'docker compose up -d db failed' }

    Write-Host '==> Waiting for local Postgres to be ready...' -ForegroundColor Cyan
    $ready = $false
    foreach ($n in 1..30) {
        docker compose exec -T db pg_isready -U $locUser -d $locName *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Local Postgres did not become ready in time' }

    # ── 2. Dump production into .seed/prod.sql ──────────────────────────────
    Write-Host "==> Dumping production ($prodHost/$prodName) ..." -ForegroundColor Cyan
    docker run --rm `
        -e PGPASSWORD=$prodPass `
        -e PGSSLMODE=require `
        -v "${seedDir}:/dump" `
        $PG_IMAGE `
        pg_dump -h $prodHost -p $prodPort -U $prodUser -d $prodName `
            --no-owner --no-privileges --clean --if-exists --schema=public `
            -f /dump/prod.sql
    if ($LASTEXITCODE -ne 0) { throw 'pg_dump from production failed' }

    $sizeKb = [math]::Round((Get-Item $dumpFile).Length / 1KB, 1)
    Write-Host "    wrote $dumpFile ($sizeKb KB)" -ForegroundColor DarkGray

    # ── 3. Restore into the local container ─────────────────────────────────
    Write-Host "==> Restoring into local '$locName' database..." -ForegroundColor Cyan
    docker compose cp $dumpFile db:/tmp/prod.sql
    if ($LASTEXITCODE -ne 0) { throw 'copying dump into db container failed' }

    docker compose exec -T db psql -U $locUser -d $locName -f /tmp/prod.sql
    if ($LASTEXITCODE -ne 0) { throw 'psql restore failed' }

    Write-Host ''
    Write-Host '✅ Local database seeded from production.' -ForegroundColor Green
    Write-Host '   Next:  docker compose up --build   (then visit http://localhost)' -ForegroundColor Green
}
finally {
    Pop-Location
}
