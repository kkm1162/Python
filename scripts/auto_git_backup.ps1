# Daily auto-commit helper. Point $RepoRoot at the folder where you ran `git init`.
$RepoRoot = "C:\Users\thelastsun\Documents\Python"

Set-Location -LiteralPath $RepoRoot
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git"))) {
    exit 1
}

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    exit 1
}

$dirty = git status --porcelain 2>$null
if ([string]::IsNullOrWhiteSpace($dirty)) {
    exit 0
}

git add -A 2>$null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$msg = "auto backup $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
git commit -m $msg 2>$null
exit $LASTEXITCODE
