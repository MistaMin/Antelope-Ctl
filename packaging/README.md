# Packages

Every package carries all device profiles, so one install serves every supported interface. The
system packages and the AppImage install the source tree unchanged (`/usr/share/antelope-ctl`, via
[`install-tree.sh`](install-tree.sh)) and share one WebUI launcher, [`antelope-webui`](antelope-webui),
which starts one server per connected interface (ports 8714, 8715, ...) and opens each in the browser.
The version lives in [`pyproject.toml`](../pyproject.toml) (`rpm/antelope-ctl.spec` repeats it).

Only the Arch, deb and rpm packages install the udev rule. With pipx, the AppImage, the Flatpak or a
checkout, install [`70-antelope.rules`](70-antelope.rules) once:
`sudo cp packaging/70-antelope.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger`.
With any package, replug an interface that was connected during the install. Run only one controller
per interface: do not start the systemd user units and the launcher for the same device.

| Way | For | Build | Install | Still to do | Status |
| --- | --- | --- | --- | --- | --- |
| pip / pipx | any distribution, per user | `python -m build` (needs `pip install build`) | `pipx install '.[webui]'` | udev rule; start `antelope-webui` (one interface; `ANTELOPE_PROFILE`/`ANTELOPE_PORT` choose) | wheel built and installed into a fresh venv; CLI, profiles, catalogue and WebUI files checked; server not started |
| Arch (AUR-style) | Arch and derivatives | `cd packaging/aur && makepkg` | `makepkg -si` | nothing | built with `makepkg --nodeps`; contents, units, udev rule and desktop entry validated; not installed, no `namcap` |
| Debian / Ubuntu `.deb` | Debian 13+, Ubuntu 24.04+ | `packaging/deb/build.sh` | `sudo apt install ./antelope-ctl_0.1.0_all.deb` | nothing | built without dpkg; archive, control, md5sums and modes checked; not installed, no `lintian` |
| Fedora `.rpm` | Fedora and similar | see the header of `rpm/antelope-ctl.spec` | `sudo dnf install ./antelope-ctl-0.1.0-1*.noarch.rpm` | nothing | not built (no `rpmbuild`); spec read and its install step simulated |
| AppImage | any x86_64 distribution | `packaging/appimage/build.sh PYTHON_TARBALL` (prints what it needs) | `chmod +x` the file and run it; `... cli --help` runs the CLI | udev rule | not built (no `appimagetool`); AppDir assembly and `AppRun cli` tried with a stand-in Python |
| Flatpak | Flathub-style desktops | `packaging/flatpak/build.sh` | `flatpak install --user antelope-ctl.flatpak` | udev rule | built with flatpak-builder (freedesktop 25.08, 7.5 MB bundle), installed for the user and run: with a Zen Go and a Discrete 4 connected it started one server per interface and both came online |

The distribution packages use the distribution's FastAPI and Uvicorn (Uvicorn 0.22 or newer); the pinned
[`webui/requirements.txt`](../webui/requirements.txt) is what pipx, the AppImage and the Flatpak install.
Debian 12 ships Uvicorn 0.17, too old for the WebUI (the CLI still works). The AppImage bundles its own
Python because FastAPI needs `pydantic-core`, a compiled module built for one Python version.
The systemd user units are installed but not enabled: `systemctl --user enable --now antelope-webui`
runs one interface at login instead of the launcher.
