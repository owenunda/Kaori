<p align="center"><img src="https://raw.githubusercontent.com/riyaniii/Kaori/1a4a59ccb36fa2adab57c866f6e41623bd34de89/data/icons/hicolor/scalable/apps/com.rini.kaghez.svg">
<h1 align="center">Kaori</h1>

Kaori allows you to discover, download and read comics with a beautiful looking UI, and natural touchpad gestures.

## Features

- A beautiful libadwaita UI
- Browse and search comics from dozens of sources
- Download chapters for offline reading
- Built-in reader with single-page, double-page, and webtoon (continuous scroll) modes
- Natural touchpad gesture navigation (Pinch to zoom/two-finger scroll to move)

Kaori connects to/runs [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) as the backend.

### Installation

#### Linux

You can install Kaori as flatpak
<p>
  <a href="https://github.com/riyaniii/Kaori/releases">
    <img src="https://img.shields.io/badge/⬇%20Download%20Kaori-GitHub%20Releases-181717?style=for-the-badge&logo=github&logoColor=white" height="60">
  </a>
</p>

#### Windows

Download the installer (`Kaori-<version>-setup-x64.exe`) or the portable zip. Everything is bundled (GTK, a Java 21 runtime and the Suwayomi server), so nothing else needs to be installed.
<p>
  <a href="https://github.com/owenunda/Kaori/releases">
    <img src="https://img.shields.io/badge/⬇%20Download%20for%20Windows-GitHub%20Releases-0078D4?style=for-the-badge&logo=windows&logoColor=white" height="60">
  </a>
</p>

The installer isn't signed, so Windows SmartScreen may warn you the first time: click **More info** → **Run anyway**.

<details>
<summary>Building for Windows</summary>

The installer and the portable zip are built by the [Windows build workflow](.github/workflows/windows.yml). To build it yourself, install [MSYS2](https://www.msys2.org/), open the **UCRT64** shell in the repository (with `git lfs pull` done) and run:

```bash
bash build-aux/windows/build.sh
```

The app ends up in `_build-windows/dist/Kaori/Kaori.exe`. Logs go to `%LOCALAPPDATA%\kaghez\kaghez.log`.
</details>

## Screenshots

<table>
  <tr>
    <td align="center">
      <img src="https://github.com/user-attachments/assets/985faef7-77a9-4ede-b8e9-c1b8421c54b6" width="100%">
      <br>
      <strong>Home</strong>
    </td>
    <td align="center">
      <img src="https://github.com/user-attachments/assets/b937ceb0-4049-4733-a778-b565908b44bf" width="100%">
      <br>
      <strong>Search</strong>
    </td>
  </tr>
  <tr>
    <td align="center">
      <img src="https://github.com/user-attachments/assets/11405fb5-f572-4c72-b442-2be75cbb2c40" width="100%">
      <br>
      <strong>Reader</strong>
    </td>
    <td align="center">
      <img src="https://github.com/user-attachments/assets/09768716-077c-4f63-80d3-a29f62b5d03b" width="100%">
      <br>
      <strong>Comic Details</strong>
    </td>
  </tr>
  <tr>
    <td align="center" colspan="2">
      <img src="https://github.com/user-attachments/assets/7fa8d539-2b09-499e-a7fc-f95684a4904d" width="75%">
      <br>
      <strong>Downloads</strong>
    </td>
  </tr>


