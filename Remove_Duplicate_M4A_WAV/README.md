# Remove Duplicate M4A/WAV Files

After converting tracks between formats, you end up with both `.mp3` and `.m4a` (and sometimes `.wav`) sitting in the same folder. This script cleans up the duplicates by keeping whichever format you prefer and deleting the rest.

By default it keeps `.mp3`. Use `-Keep m4a` to keep `.m4a` instead.

Matches files by name within the same directory, so `Folder1\song.mp3` won't touch `Folder2\song.wav`.

Requires PowerShell 5.1+ (ships with Windows 10/11).

## Usage

Dry run first to see what it would delete:

```powershell
.\Remove_Duplicate_M4A_WAV_Files.ps1 -Path "D:\Music" -WhatIf
```

Actually delete (keeps .mp3 by default):

```powershell
.\Remove_Duplicate_M4A_WAV_Files.ps1 -Path "D:\Music"
```

Keep .m4a instead of .mp3:

```powershell
.\Remove_Duplicate_M4A_WAV_Files.ps1 -Path "D:\Music" -Keep m4a
```

If you get an execution policy error:

```powershell
powershell -ExecutionPolicy Bypass -File .\Remove_Duplicate_M4A_WAV_Files.ps1 -Path "D:\Music"
```

`-Confirm` is also supported if you want to approve each deletion individually.

## Example Output

```
Deleted: D:\Music\Track01.m4a
Deleted: D:\Music\Track01.wav
Deleted: D:\Music\Subfolder\Track02.m4a

--- Summary ---
Files deleted : 3
Space freed   : 42.15 MB
```
