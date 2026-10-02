# Import Project Brain workflows into n8n (owner setup + REST import + publish).
# Requires: n8n running, .env with N8N_* and N8N_OWNER_* vars.

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Read-DotEnv([string]$path) {
    $vars = @{}
    if (-not (Test-Path $path)) { return $vars }
    Get-Content $path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { return }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        if ($val.StartsWith('"') -and $val.EndsWith('"')) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $vars[$key] = $val
    }
    return $vars
}

$envVars = Read-DotEnv (Join-Path $root ".env")
foreach ($key in @(
    "N8N_WEBHOOK_URL", "N8N_BASIC_AUTH_USER", "N8N_BASIC_AUTH_PASSWORD",
    "N8N_OWNER_EMAIL", "N8N_OWNER_PASSWORD"
)) {
    if ($envVars[$key]) { Set-Item -Path "env:$key" -Value $envVars[$key] }
}
if ($envVars["N8N_WEBHOOK_URL"]) {
    $base = $envVars["N8N_WEBHOOK_URL"].TrimEnd("/")
    Set-Item -Path "env:N8N_BASE_URL" -Value $base
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python (Join-Path $root "scripts\import_n8n_workflows.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
