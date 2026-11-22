# RPM of antelope-ctl, same layout as the Arch and deb packages (packaging/install-tree.sh).
# Build from a checkout (keep Version in step with pyproject.toml):
#   git archive --prefix=antelope-ctl-0.1.0/ -o ~/rpmbuild/SOURCES/antelope-ctl-0.1.0.tar.gz HEAD
#   rpmbuild -bb packaging/rpm/antelope-ctl.spec
Name:           antelope-ctl
Version:        0.1.0
Release:        1%{?dist}
Summary:        CLI and local WebUI for Antelope Audio USB interfaces
License:        GPL-3.0-or-later AND MIT
URL:            https://github.com/MistaMin/Antelope-Ctl
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.10
Recommends:     python3-fastapi
Recommends:     python3-uvicorn >= 0.22
Recommends:     xdg-utils

%description
Profile-driven control of Antelope Audio interfaces over USB HID: a
standard-library Python CLI and a local browser UI (FastAPI) for mixer,
routing and effects, with device profiles for every supported interface,
the udev rule for desktop access and systemd user units for the WebUI.

%prep
%autosetup

%build

%install
packaging/install-tree.sh %{buildroot}
cp -p profiles/LICENSE LICENSE-profiles

%files
%license LICENSE LICENSE-profiles
%{_bindir}/antelope-ctl
%{_bindir}/antelope-webui
%{_datadir}/antelope-ctl/
%{_prefix}/lib/udev/rules.d/70-antelope.rules
%{_prefix}/lib/systemd/user/antelope-webui.service
%{_prefix}/lib/systemd/user/antelope-webui@.service
%{_datadir}/applications/io.github.mistamin.AntelopeCtl.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.mistamin.AntelopeCtl.svg
