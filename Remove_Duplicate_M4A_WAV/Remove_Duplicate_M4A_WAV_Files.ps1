#Requires -Version 5.1
<#
.SYNOPSIS
    Removes duplicate audio files when the preferred format exists in the same folder.
.PARAMETER Path
    The root folder to scan (recursively).
.PARAMETER Keep
    Which format to keep: mp3 (default) or m4a.
.EXAMPLE
    .\Remove_Duplicate_M4A_WAV_Files.ps1 -Path "D:\Music"
.EXAMPLE
    .\Remove_Duplicate_M4A_WAV_Files.ps1 -Path "D:\Music" -Keep m4a -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path $_ -PathType Container })]
    [string]$Path,

    [ValidateSet("mp3", "m4a")]
    [string]$Keep = "mp3"
)

$audioFiles = Get-ChildItem -Path $Path -Recurse -File -Include *.mp3, *.m4a, *.wav

if (-not $audioFiles) {
    Write-Host "No audio files found in: $Path" -ForegroundColor Yellow
    return
}

$keepExt = ".$Keep"

# Group by folder + base name so only true duplicates in the same directory match
$grouped = $audioFiles | Group-Object { Join-Path $_.DirectoryName $_.BaseName }

$deletedCount = 0
$freedBytes   = 0

foreach ($group in $grouped) {
    $files = $group.Group

    $preferred = $files | Where-Object { $_.Extension -ieq $keepExt }
    if (-not $preferred) { continue }

    $toDelete = $files | Where-Object { $_.Extension -ine $keepExt }
    foreach ($file in $toDelete) {
        if ($PSCmdlet.ShouldProcess($file.FullName, "Delete")) {
            Remove-Item -LiteralPath $file.FullName -Force
            Write-Host "Deleted: $($file.FullName)" -ForegroundColor Red
            $deletedCount++
            $freedBytes += $file.Length
        }
    }
}

$freedMB = [math]::Round($freedBytes / 1MB, 2)
Write-Host "`n--- Summary ---" -ForegroundColor Cyan
Write-Host "Files deleted : $deletedCount"
Write-Host "Space freed   : $freedMB MB"
