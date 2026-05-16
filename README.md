# HWP Calculator

A desktop GUI to calculate and group HWBOT Hardware Points (HWP) for CPUs, GPUs, and Motherboards.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features
* **Sticky Table Headers:** View benchmark categories clearly even while scrolling.
* **Smart Filtering:** Filter by cooling type (Air, LN2, etc.) or HWP range.
* **Export Functionality:** Export your results directly to a CSV file.
* **One-Click Expansion:** Toggle all HWP buckets at once.
* **Copy Summary:** Instantly copy a formatted summary for sharing.

## Installation

1. **Requirement:** Ensure you have [Python 3.10+](https://www.python.org/downloads/) installed.
2. **Setup:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Run:**
   ```bash
   python main.py
   ```

## How to Build the EXE
To create a standalone Windows executable:
```bash
pip install pyinstaller customtkinter
pyinstaller --noconfirm --onefile --windowed --name "HWBOT HWP Calculator" --collect-all customtkinter --icon="pc.ico" main.py
```
The executable will be generated in the `dist/` folder.

## Credits
Designed & Developed by **@hxntxihero**.

*Disclaimer: This tool is not officially affiliated with HWBOT.org.*
