param(
    [string]$Downloads = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '..\botyara-loader-downloads'),
    [string]$Dotnet = "$env:USERPROFILE\.dotnet\dotnet.exe",
    [string]$Cargo = ''
)
$ErrorActionPreference = 'Stop'
$originalPath = $env:PATH
$originalClient = $env:BOTYARA_CLIENT_EXE
try {
    if (!(Test-Path -LiteralPath $Dotnet)) { $Dotnet = (Get-Command dotnet -ErrorAction Stop).Source }
    if (!$Cargo) {
        $candidate = Join-Path $Downloads 'toolchain\bin\cargo.exe'
        if (Test-Path -LiteralPath $candidate) { $Cargo = (Resolve-Path -LiteralPath $candidate).Path }
        else { $Cargo = (Get-Command cargo -ErrorAction Stop).Source }
    }
    $env:PATH = (Split-Path $Cargo -Parent) + ';' + $env:PATH
    $managed = Join-Path $PSScriptRoot ('target\managed-' + [Guid]::NewGuid().ToString('N'))
    $project = Join-Path $PSScriptRoot '..\dotnet\BotyaraClient.csproj'
    $restore = @()
    if (Test-Path -LiteralPath (Join-Path $Downloads 'microsoft.netframework.referenceassemblies.net40.1.0.3.nupkg')) { $restore = @('--source', (Resolve-Path -LiteralPath $Downloads).Path) }
    & $Dotnet build $project -c Release -t:Rebuild -o $managed @restore
    if ($LASTEXITCODE -ne 0) { throw 'net40 build failed' }
    $env:BOTYARA_CLIENT_EXE = (Resolve-Path -LiteralPath (Join-Path $managed 'BotyaraClient.exe')).Path
    Push-Location $PSScriptRoot
    try {
        & $Cargo build --release --locked --target-dir target --target x86_64-pc-windows-msvc
        if ($LASTEXITCODE -ne 0) { throw 'Rust build failed' }
        & $Cargo test --locked --target-dir target --target x86_64-pc-windows-msvc
        if ($LASTEXITCODE -ne 0) { throw 'Profile selection tests failed' }
    } finally { Pop-Location }
    $dist = Join-Path $PSScriptRoot 'dist'
    New-Item -ItemType Directory -Path $dist -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'target\x86_64-pc-windows-msvc\release\Loader.exe') -Destination (Join-Path $dist 'Loader.exe') -Force
    $result = Get-Item -LiteralPath (Join-Path $dist 'Loader.exe')
    Write-Output ("Loader: {0} ({1} bytes)" -f $result.FullName, $result.Length)
    Write-Output ('SHA256: ' + (Get-FileHash -LiteralPath $result.FullName -Algorithm SHA256).Hash)
} finally {
    $env:PATH = $originalPath
    $env:BOTYARA_CLIENT_EXE = $originalClient
}
