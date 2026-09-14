[CmdletBinding()]
param(
    [string]$DeviceSsid = "EInk-Setup-8FCBA4",
    [string]$PortalBaseUri = "http://192.168.4.1",
    [string]$HomeProfile = "",
    [string]$OutputDirectory = "",
    [ValidateRange(30, 600)]
    [int]$MaxOfflineSeconds = 180,
    [string]$UploadPath = "",
    [switch]$RefreshDisplay,
    [ValidateSet("", "wifi", "cellular")]
    [string]$BandwidthTransport = "",
    [ValidateSet(262144, 1048576, 5242880)]
    [int]$BandwidthBytes = 1048576
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Get-ConnectedWifiProfile {
    $text = (& netsh.exe wlan show interfaces | Out-String)
    $match = [regex]::Match(
        $text,
        "(?m)^\s*Profile\s*:\s*(?<profile>.+?)\s*$"
    )
    if (-not $match.Success) {
        throw "Unable to determine the currently connected Wi-Fi profile."
    }
    return $match.Groups["profile"].Value.Trim()
}

function Wait-WifiProfile {
    param(
        [Parameter(Mandatory = $true)][string]$Profile,
        [int]$TimeoutSeconds = 25
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            if ((Get-ConnectedWifiProfile) -eq $Profile) {
                return $true
            }
        } catch {
            # The adapter normally reports no profile briefly while switching.
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Invoke-PortalJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateSet("GET", "POST")][string]$Method = "GET",
        [hashtable]$Body = $null,
        [int]$TimeoutSeconds = 15
    )

    $parameters = @{
        Uri = "$PortalBaseUri$Path"
        Method = $Method
        TimeoutSec = $TimeoutSeconds
        UseBasicParsing = $true
    }
    if ($null -ne $Body) {
        $parameters["Body"] = $Body
        $parameters["ContentType"] = "application/x-www-form-urlencoded"
    }
    return Invoke-RestMethod @parameters
}

$originalProfile = if ($HomeProfile) {
    $HomeProfile
} else {
    Get-ConnectedWifiProfile
}

if ($originalProfile -eq $DeviceSsid) {
    throw "HomeProfile must identify the internet-connected Wi-Fi, not the device AP."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $PSScriptRoot "portal-test-results\$stamp"
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$transcriptPath = Join-Path $OutputDirectory "run.log"
$profileXmlPath = Join-Path $env:TEMP "eink-portal-$PID.xml"
$watchdog = $null
$testSucceeded = $false

function Write-TestLog {
    param([string]$Message)
    $line = "{0:o}  {1}" -f (Get-Date), $Message
    $line | Tee-Object -FilePath $transcriptPath -Append
}

$escapedSsid = [System.Security.SecurityElement]::Escape($DeviceSsid)
$profileXml = @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>$escapedSsid</name>
  <SSIDConfig><SSID><name>$escapedSsid</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security><authEncryption>
    <authentication>open</authentication>
    <encryption>none</encryption>
    <useOneX>false</useOneX>
  </authEncryption></security></MSM>
</WLANProfile>
"@
$profileXml | Set-Content -Path $profileXmlPath -Encoding UTF8

# This independent process restores internet if this script, a request, or the
# invoking terminal becomes unresponsive after switching away from home Wi-Fi.
$escapedHomeForCommand = $originalProfile.Replace("'", "''")
$watchdogCommand = @"
Start-Sleep -Seconds $MaxOfflineSeconds
& netsh.exe wlan connect "name=$escapedHomeForCommand" | Out-Null
"@
$watchdogEncoded = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($watchdogCommand)
)

try {
    Write-TestLog "Original Wi-Fi profile: $originalProfile"
    Write-TestLog "Installing temporary open-network profile: $DeviceSsid"
    & netsh.exe wlan add profile "filename=$profileXmlPath" user=current |
        Tee-Object -FilePath $transcriptPath -Append | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install the temporary device Wi-Fi profile."
    }

    $watchdog = Start-Process powershell.exe -WindowStyle Hidden -PassThru `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $watchdogEncoded"
    Write-TestLog "Recovery watchdog armed for $MaxOfflineSeconds seconds."

    Write-TestLog "Connecting to $DeviceSsid"
    & netsh.exe wlan connect "name=$DeviceSsid" "ssid=$DeviceSsid" |
        Tee-Object -FilePath $transcriptPath -Append | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Wait-WifiProfile $DeviceSsid 30)) {
        throw "Laptop did not connect to $DeviceSsid."
    }
    Write-TestLog "Connected to device AP."

    $root = Invoke-WebRequest -Uri "$PortalBaseUri/" -UseBasicParsing `
        -TimeoutSec 15
    $root.Content | Set-Content -Path (Join-Path $OutputDirectory "portal.html") `
        -Encoding UTF8
    Write-TestLog "Portal root returned HTTP $($root.StatusCode)."

    $checks = [ordered]@{
        status = "/api/status"
        wifi_scan = "/api/wifi/scan"
        sms = "/api/sms"
        phone_home = "/api/logs"
        media = "/api/media"
    }
    foreach ($name in $checks.Keys) {
        Write-TestLog "GET $($checks[$name])"
        $result = Invoke-PortalJson -Path $checks[$name] -TimeoutSeconds 20
        $result | ConvertTo-Json -Depth 20 |
            Set-Content -Path (Join-Path $OutputDirectory "$name.json") `
                -Encoding UTF8
    }

    foreach ($slot in @("current", "next")) {
        try {
            $destination = Join-Path $OutputDirectory "$slot.bmp"
            Invoke-WebRequest -Uri "$PortalBaseUri/media/$slot.bmp?download=1" `
                -OutFile $destination -UseBasicParsing -TimeoutSec 45
            Write-TestLog "Downloaded $slot media."
        } catch {
            Write-TestLog "$slot media unavailable: $($_.Exception.Message)"
        }
    }

    if ($UploadPath) {
        $resolvedUpload = (Resolve-Path $UploadPath).Path
        Write-TestLog "Uploading media: $resolvedUpload"
        $curlOutput = & curl.exe --silent --show-error --fail-with-body `
            --max-time 90 -F "media=@$resolvedUpload" `
            "$PortalBaseUri/api/media/upload" 2>&1
        $curlOutput | Set-Content `
            -Path (Join-Path $OutputDirectory "upload-response.txt") `
            -Encoding UTF8
        if ($LASTEXITCODE -ne 0) {
            throw "Media upload failed: $curlOutput"
        }
    }

    if ($RefreshDisplay) {
        Write-TestLog "Queueing manual display refresh/swap."
        Invoke-PortalJson -Path "/api/media/refresh" -Method POST |
            ConvertTo-Json -Depth 10 |
            Set-Content -Path (Join-Path $OutputDirectory "refresh.json") `
                -Encoding UTF8
    }

    if ($BandwidthTransport) {
        Write-TestLog "Queueing $BandwidthTransport bandwidth test."
        Invoke-PortalJson -Path "/api/bandwidth" -Method POST -Body @{
            transport = $BandwidthTransport
            bytes = $BandwidthBytes
        } | ConvertTo-Json -Depth 10 |
            Set-Content -Path (Join-Path $OutputDirectory "bandwidth.json") `
                -Encoding UTF8
    }

    $testSucceeded = $true
    Write-TestLog "Portal checks completed."
} catch {
    Write-TestLog "FAILED: $($_.Exception.Message)"
    throw
} finally {
    Write-TestLog "Restoring Wi-Fi profile: $originalProfile"
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        & netsh.exe wlan connect "name=$originalProfile" |
            Tee-Object -FilePath $transcriptPath -Append | Out-Null
        if (Wait-WifiProfile $originalProfile 20) {
            break
        }
    }

    if ($watchdog -and -not $watchdog.HasExited) {
        Stop-Process -Id $watchdog.Id -Force -ErrorAction SilentlyContinue
    }
    & netsh.exe wlan delete profile "name=$DeviceSsid" |
        Tee-Object -FilePath $transcriptPath -Append | Out-Null
    Remove-Item $profileXmlPath -Force -ErrorAction SilentlyContinue

    if (Wait-WifiProfile $originalProfile 5) {
        Write-TestLog "Internet Wi-Fi restored."
    } else {
        Write-TestLog "WARNING: automatic Wi-Fi restoration was not confirmed."
    }
    Write-TestLog "Result directory: $OutputDirectory"
}

if ($testSucceeded) {
    Write-Host "PASS - portal results saved to $OutputDirectory"
}
