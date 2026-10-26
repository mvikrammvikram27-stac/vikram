$ErrorActionPreference = "Stop"

$python = Get-Command "python.exe" -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python 3 is required to run the backend, but it was not found." -ForegroundColor Red
    Write-Host "Install Python 3, then close and reopen this terminal."
    Write-Host "Download: https://www.python.org/downloads/"
    exit 1
}

if (-not (Test-Path (Join-Path $PSScriptRoot ".env")) -and
    [string]::IsNullOrWhiteSpace($env:MYSQL_PASSWORD)) {
    $securePassword = Read-Host "Enter your MySQL root password (input is hidden; press Enter if none)" -AsSecureString
    $passwordPointer = [IntPtr]::Zero
    try {
        $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $env:MYSQL_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    } finally {
        if ($passwordPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
        $securePassword.Dispose()
    }
}

Push-Location $PSScriptRoot
try {
    & $python.Source -c "import mysql.connector" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing Python backend dependencies..."
        & $python.Source -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Dependency installation failed. Check the pip output above."
            exit $LASTEXITCODE
        }
    }

    & $python.Source (Join-Path $PSScriptRoot "server.py")
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
