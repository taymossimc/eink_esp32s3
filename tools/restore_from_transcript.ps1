$Transcript = "C:\Users\TayMoss\.cursor\projects\c-Users-TayMoss-Cursor-Spaces-eInk-esp32s3-eink-esp32s3\agent-transcripts\173bc752-e115-4a13-b822-0f2862f8946c\173bc752-e115-4a13-b822-0f2862f8946c.jsonl"
$Root = "C:\Users\TayMoss\Cursor Spaces\eInk_esp32s3\eink_esp32s3"

function Get-TargetPath([string]$Path) {
    if ($Path -match 'eink_esp32s3[\\/](.+)$') {
        return Join-Path $Root ($Matches[1] -replace '\\', '/')
    }
    return $null
}

$writes = @{}
$strReplaces = New-Object System.Collections.Generic.List[object]

Get-Content -LiteralPath $Transcript -Encoding UTF8 | ForEach-Object {
    try {
        $obj = $_ | ConvertFrom-Json
    } catch {
        return
    }

    $content = $obj.message.content
    if (-not ($content -is [System.Array])) { return }

    foreach ($item in $content) {
        if ($item.type -ne 'tool_use') { continue }
        $name = $item.name
        $inp = $item.input
        if (-not $inp) { continue }

        $path = if ($inp.path) { $inp.path } else { $inp.file_path }
        if (-not $path) { continue }

        if ($name -eq 'Write') {
            $body = if ($null -ne $inp.contents) { $inp.contents } else { $inp.content }
            if ($null -ne $body) {
                $writes[$path] = $body
            }
        } elseif ($name -eq 'StrReplace') {
            $strReplaces.Add($inp)
        }
    }
}

$writeCount = 0
foreach ($entry in $writes.GetEnumerator()) {
    $target = Get-TargetPath $entry.Key
    if (-not $target) { continue }
    $dir = Split-Path $target -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($target, [string]$entry.Value)
    $writeCount++
}

$repOk = 0
$repFail = 0
foreach ($inp in $strReplaces) {
    $target = Get-TargetPath $inp.path
    if (-not $target -or -not (Test-Path $target)) {
        $repFail++
        continue
    }
    $text = [System.IO.File]::ReadAllText($target)
    if (-not $text.Contains($inp.old_string)) {
        $repFail++
        continue
    }
    $updated = $text.Replace($inp.old_string, $inp.new_string)
    [System.IO.File]::WriteAllText($target, $updated)
    $repOk++
}

$tmadsDir = Join-Path $Root 'inky_ESP32_13inch\tmads'
$tmadsFiles = if (Test-Path $tmadsDir) { (Get-ChildItem $tmadsDir | Select-Object -ExpandProperty Name) -join ', ' } else { 'none' }

Write-Output "writes_applied=$writeCount"
Write-Output "str_replaces_applied=$repOk"
Write-Output "str_replaces_failed=$repFail"
Write-Output "tmads_files=$tmadsFiles"
