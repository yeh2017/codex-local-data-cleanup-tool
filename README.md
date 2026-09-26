# Codex Local Data Cleanup Tool [简体中文](README.zh-CN.md) | English

An unofficial Windows GUI for inspecting, backing up, restoring, and cleaning local records created by the Codex desktop app. The interface supports Chinese and English.

> This community project is not affiliated with or supported by OpenAI. Fully exit Codex before modifying local data.

Project page: [Open-source Tools](https://www.qfo-quant-platform.com/tools/)

## Download and Start

1. Download the Windows x64 ZIP from the [latest release](https://github.com/yeh2017/codex-local-data-cleanup-tool/releases/latest).
2. Extract the complete folder. Do not copy only the EXE.
3. Run `Codex Local Data Cleanup Tool.exe`.
4. If startup fails, run `diagnose_codex_cleanup_tool.bat` from the same folder.

The standalone package does not require Python, OpenCV, or other third-party runtimes. Because the EXE is unsigned, Windows SmartScreen may display a warning. Verify the SHA-256 value shown on the Release page before running it.

The executable is named `Codex Local Data Cleanup Tool.exe` in both Chinese and English interface modes.

## Supported Systems

- Windows 10 64-bit
- Windows 11 64-bit

Windows 32-bit, macOS, and Linux are not supported.

## Main Features

- Scan the total `.codex` size and show storage used by records, logs, and caches.
- Select Chinese or English automatically from the Windows display language, with a saved manual override.
- List local task history and select individual tasks for backup or deletion.
- Before deleting a task, create a persistent backup and remove its session files, database relationships, local task index entries, and logs with the same task ID.
- Restore session files, database records, relationships, indexes, and related logs from a backup created by the tool.
- Inspect the log database and run a short log-growth check.
- Back up, validate, clean, and compact old logs, with automatic rollback on failure.
- Move allowlisted cache items to the Windows Recycle Bin instead of permanently deleting them.

## Preparation, Paths, and Local Data

The Codex data folder is detected in this order:

1. The `CODEX_HOME` environment variable;
2. `%USERPROFILE%\.codex`;
3. The last folder selected manually.

If no valid `.codex` folder is found, the tool does not create one and does not scan or delete anything. Use **Browse** to select a valid Codex data folder stored elsewhere; an ordinary folder is rejected.

Settings and startup logs are stored under:

```text
%LOCALAPPDATA%\CodexLocalCleanupTool
```

The default persistent backup folder is:

```text
%USERPROFILE%\Documents\Codex历史记录备份
```

You can change the backup folder in the application. If it is moved or deleted, the tool prompts you to recreate or locate it.

Fully exit the Codex desktop app before backup, deletion, restore, or log optimization. Normal scanning and log-growth checks are read-only.

## Scanning and General Cleanup

Select **Start Scan** to display total `.codex` usage, reclaimable space, history tasks, the log database, and cache categories. Scanning does not modify files.

Allowlisted cache, generated-image cache, visualization cache, and temporary files are moved to the Windows Recycle Bin. History tasks cannot be deleted from the general cleanup list; manage them individually on the history page.

## Correctly Delete a History Task

1. Fully exit Codex and make sure no Codex process is using the local databases.
2. Start the tool and verify the `.codex` data folder and persistent backup folder. The backup folder must be outside `.codex`.
3. Select **Start Scan**, open the history page, and select the tasks to delete.
4. Review the task titles, count, and estimated space. Deleting a parent task also backs up and deletes its related child tasks.
5. Select **Delete Selected** and review the confirmation again.
6. The tool first creates and validates a persistent backup containing session files, task database records, task relationships, local index entries, and logs with matching task IDs.
7. After validation, session files are moved to the Windows Recycle Bin. Matching rows are removed from the task database, index, logs, goals, memories, local catalog, timeline, queue, and thread-summary stores recognized by this version.
8. If any step fails, the tool attempts to roll back the databases, index, and session files. If rollback also fails, it retains rescue snapshots and displays their paths.
9. A successful deletion keeps the persistent backup. Restart Codex and check that the sidebar has updated.

Do not manually delete records from Codex databases, and do not use the Windows Recycle Bin as the only backup.

### Privacy purge

Privacy purge creates no backup and requires both a warning confirmation and the typed phrase `PERMANENT DELETE`. It binds an interrupted operation to the exact originally confirmed selection, removes references from recognized local stores, and verifies the same scope afterward.

This mode cannot be undone by this tool. It does not delete cloud or account history and cannot guarantee removal from system backups, volume snapshots, SSD remapping, or forensic disk recovery. If an unknown database, incompatible schema, protected file, or unmanaged reference contains a selected task ID, the operation stops before deletion.

## Correctly Restore a History Task

1. Fully exit Codex.
2. Start the tool and select the valid `.codex` folder that will receive the restored task.
3. Make sure the persistent backup folder is available, then select **Restore Backup** on the history page.
4. Select one task backup folder created by the tool. It must contain `manifest.json`; do not select a ZIP, the backup root, or an individual session file.
5. The tool validates file SHA-256 hashes, backup database integrity, task manifests, indexes, related logs, and `installation_id`. The data folder may move, but a backup cannot be restored into a different Codex data identity.
6. Restore is refused if the destination already contains the same task ID, index entry, or related log data.
7. After validation, the tool restores session files, task database records, relationships, local index entries, and related logs, and rewrites session paths for the current `.codex` location.
8. A failed restore is rolled back. If rollback also fails, rescue snapshots are retained and their paths are displayed.
9. Restart Codex and check the task, parent-child relationships, and history content.

Restoring only session files from the Windows Recycle Bin does not fully restore database relationships, indexes, or related logs. Use **Restore Backup** whenever possible.

## Log Diagnostics

Log diagnostics inspect the `.codex\logs_2.sqlite` runtime log database. They do not read or modify chat message content. After a scan, the page shows:

- Main database, WAL sidecar, and total storage size;
- Total log row count;
- Count and percentage of verbose `TRACE` rows;
- Quick database integrity-check result;
- Reclaimable free space inside the database.

**Check Log Growth** supports 10, 30, and 60-second sampling periods and defaults to 10 seconds. It performs two read-only samples, calculates new rows, new `TRACE` rows, and file growth per minute, then reports idle, active, or high-frequency growth. Longer periods produce more stable results. The check can be cancelled at any time and does not clean data.

**Safely Optimize Logs** keeps the latest 30 days by default and accepts a retention period from 1 to 365 days. Codex must be fully closed. The tool creates a temporary backup and checks integrity, deletes expired rows, truncates the WAL, runs `VACUUM`, and validates the result again. It restores the backup automatically on failure. It skips optimization when there are no expired rows and less than 1 MB is reclaimable.

Log optimization does not disable `TRACE` logging or fix the source of continuing writes. If high-frequency growth continues after optimization, investigate the Codex configuration, version, or runtime behavior.

## Safety Boundaries

- Nothing is selected for deletion by default. The tool shows estimated space and asks for confirmation.
- An allowlist limits which categories can be cleaned, and every target path is validated again before execution.
- The tool does not process `auth.json`, `config.toml`, `plugins`, `skills`, `vendor_imports`, or the entire `.codex` root.
- The backup folder must be outside the Codex data folder and cannot be a symbolic link or directory junction.
- Codex must be fully closed before task backup, restore, deletion, or log optimization.
- Backup integrity is validated before restore, and conflicting task IDs are not overwritten.
- Current known Codex stores are handled explicitly. Nested and unknown SQLite databases are scanned recursively; incompatible or unmanaged references block deletion instead of being silently left behind.
- “Privacy purge” means no application backup and no in-app restore. It is not a claim of forensic disk erasure.

## Test and Build from Source

The application uses only the Python standard library at runtime. Building the standalone package requires 64-bit Python 3.14, the pinned dependencies in `requirements-build.txt`, and PowerShell.

```powershell
python -B -m unittest discover -s tests
```

Build the Windows standalone folder and ZIP:

```powershell
python -m pip install -r .\requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\build_cleanup_package.ps1
```
