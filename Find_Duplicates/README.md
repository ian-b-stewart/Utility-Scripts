# Duplicate File Finder

Finds duplicate files across one or more directories by comparing content hashes. Outputs a CSV report and can optionally delete the dupes (sends them to the Recycle Bin, not permanent delete).

It narrows things down in stages so it doesn't have to read every file fully: group by size first, then partial hash (first + last 8 KB), then full hash only for the remaining candidates.

## Setup

Python 3.10+

```bash
pip install -r requirements.txt
```

Deps: `xxhash` (fast hashing), `send2trash` (Recycle Bin deletion), `rich` (optional, for progress bars).

## Usage

Scan a folder and get a CSV report:

```bash
python find_duplicates.py D:\Media
```

Multiple folders:

```bash
python find_duplicates.py D:\Media E:\Backup C:\Users\Me\Downloads
```

Custom report path:

```bash
python find_duplicates.py D:\Media --report my_dupes.csv
```

Delete duplicates (you'll have to confirm with `YES`):

```bash
python find_duplicates.py D:\Media --delete
```

Pick which copy to keep with `--keep`:

```bash
python find_duplicates.py D:\Media --delete --keep newest
```

Options: `shortest-path` (default), `longest-path`, `oldest`, `newest`

Only care about large files? Use `--min-size` (bytes):

```bash
python find_duplicates.py D:\Media --min-size 1048576
```

Skip certain files:

```bash
python find_duplicates.py D:\Media --exclude "*.tmp" "Thumbs.db" "desktop.ini"
```

Top-level only (no recursion):

```bash
python find_duplicates.py D:\Media --no-recursive
```

Kitchen sink:

```bash
python find_duplicates.py D:\Media E:\Backup ^
    --report dupes.csv ^
    --delete ^
    --keep newest ^
    --min-size 1024 ^
    --exclude "*.tmp" "*.log"
```

## CSV Columns

`group_id` - groups duplicates together, `action` - KEEP or DELETE, `file_path`, `file_size`, `file_size_human`, `modified_date`, `hash`

Open the CSV in Excel to review before running with `--delete`.

## Safety Features

- **Report-only by default** — no files are deleted unless `--delete` is explicitly passed
- **Recycle Bin** — deleted files go to the Recycle Bin, not permanently removed
- **Confirmation prompt** — you must type `YES` before deletion proceeds
- **Content-verified** — files are matched by full content hash, not just name or size
- **Hard-link aware** — hard links to the same data are detected and excluded
- **Error-tolerant** — locked files, permission errors, and disappeared files are logged and skipped gracefully
- **Long path support** — handles Windows paths longer than 260 characters

## Troubleshooting

| Issue | Solution |
|-------|---------|
| `xxhash is required` | Run `pip install -r requirements.txt` |
| `send2trash is not installed` | Run `pip install send2trash` (only needed for `--delete`) |
| Permission errors on some files | These are logged and skipped automatically. Run as Administrator if needed. |
| Slow on network drives | Network I/O is the bottleneck. Consider copying to a local drive first. |
