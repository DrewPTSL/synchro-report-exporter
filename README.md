# Synchro Report Exporter

Batch-exports every scenario in an open Synchro 12 project to PDF and TXT reports, using UI automation (pywinauto).

## Features
- Exports all scenarios, including ones the dropdown hides
- PDF, TXT, or both
- Resume interrupted runs
- Stop any time with Esc
- Logs to %LOCALAPPDATA%\SynchroReportExporter\export.log

## Requirements
- Windows
- Synchro 12 open on the target project

## Usage
- Open your project in Synchro.
- Launch SynchroReportExporter.exe.
- Choose an output folder and format(s).
- Start the export and leave the mouse and keyboard alone until it finishes.

Files are named <name>_<period>.pdf / .txt, e.g. TT (10Y) PM → TT (10Y)_PM.pdf.

## Build from source

- Requires Python 3. On Windows, run:

` build_exe.bat `

This produces dist\SynchroReportExporter.exe. If Defender flags it, change --onefile to --onedir in the script.
