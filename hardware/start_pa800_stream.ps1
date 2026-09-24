# Start the PA-800 bench camera stream on http://localhost:9000
$ErrorActionPreference = "Stop"

$Port = 9000
$CameraIndex = 1
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StreamScript = Join-Path $ScriptDir "pa800_stream.py"

function Stop-PortListener {
    param([int]$TargetPort)
    $connections = netstat -ano | Select-String ":$TargetPort\s"
    foreach ($line in $connections) {
        if ($line -match "\sLISTENING\s+(\d+)\s*$") {
            $pid = [int]$Matches[1]
            if ($pid -gt 0) {
                Write-Host "Stopping process $pid on port $TargetPort"
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

Stop-PortListener -TargetPort $Port
Start-Sleep -Milliseconds 500

Write-Host "Starting PA-800 stream (camera index $CameraIndex) on http://localhost:$Port"
py -3.11 $StreamScript --camera-index $CameraIndex --port $Port
