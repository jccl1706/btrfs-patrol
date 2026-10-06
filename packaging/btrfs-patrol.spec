Name:           btrfs-patrol
Version:        0.8.0
Release:        %autorelease
Summary:        Btrfs snapshot manager and rollback tool for Fedora

License:        GPL-3.0-or-later
URL:            https://github.com/jccl1706/btrfs-patrol
Source:         %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros

Requires:       btrfs-progs
Requires:       util-linux
Recommends:     libdnf5-plugin-actions

%description
btrfs-patrol takes, lists and prunes snapshots of a btrfs root subvolume,
takes snapshots around dnf5 transactions, and rolls the system back to a
previous snapshot. Run "btrfs-patrol setup" after installing.

# A SUBPACKAGE, NOT A DEPENDENCY. The command line tool needs nothing but
# btrfs-progs, which is the point of it: it has to work on a system you are
# repairing. PySide6 and Kirigami are tens of megabytes of Qt, so the front end
# is something you ask for.
%package gui
Summary:        Plasma front end for btrfs-patrol
Requires:       %{name} = %{version}-%{release}
Requires:       python3-pyside6
Requires:       kf6-kirigami
# pkexec is how it asks for the privileges it does not have.
Requires:       polkit
# Without a graphical agent, pkexec falls back to a text one and dies on
# /dev/tty - with an error that sends people looking for the wrong problem.
Recommends:     polkit-kde
# Qt reports the icon theme as "hicolor" here, so Breeze has to be present for
# the window's icons to resolve at all.
Requires:       breeze-icon-theme

%description gui
A Kirigami interface for browsing btrfs-patrol's snapshots and taking new ones.
Rollback, delete and prune remain command line operations.

%prep
%autosetup -p1

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l btrfs_patrol

install -Dpm 0644 data/dnf5/btrfs-patrol.actions \
    %{buildroot}%{_sysconfdir}/dnf/libdnf5-plugins/actions.d/btrfs-patrol.actions
install -Dpm 0644 -t %{buildroot}%{_unitdir} \
    data/systemd/btrfs-patrol-snapshot.service \
    data/systemd/btrfs-patrol-snapshot.timer
install -Dpm 0644 -t %{buildroot}%{_mandir}/man8 man/btrfs-patrol.8
install -Dpm 0644 -t %{buildroot}%{_datadir}/applications \
    data/desktop/org.fd44.btrfspatrol.desktop
install -Dpm 0644 -t %{buildroot}%{_datadir}/polkit-1/actions \
    data/polkit/org.fd44.btrfspatrol.policy

%check
%pyproject_check_import -e '*.gui*'
%{py3_test_envvars} %{python3} -m unittest discover -s tests -v

%post
%systemd_post btrfs-patrol-snapshot.timer

%preun
%systemd_preun btrfs-patrol-snapshot.timer

%postun
%systemd_postun_with_restart btrfs-patrol-snapshot.timer

%files -f %{pyproject_files}
%doc README.md
%{_bindir}/btrfs-patrol
# The front end's own files belong to the subpackage, not here.
%exclude %{python3_sitelib}/btrfs_patrol/gui/
%exclude %{_bindir}/btrfs-patrol-gui-write-config
%exclude %{_bindir}/btrfs-patrol-gui-read
%{_mandir}/man8/btrfs-patrol.8*
%config(noreplace) %{_sysconfdir}/dnf/libdnf5-plugins/actions.d/btrfs-patrol.actions
%{_unitdir}/btrfs-patrol-snapshot.service
%{_unitdir}/btrfs-patrol-snapshot.timer

%files gui
%{python3_sitelib}/btrfs_patrol/gui/
%{_bindir}/btrfs-patrol-gui
%{_bindir}/btrfs-patrol-gui-write-config
%{_datadir}/applications/org.fd44.btrfspatrol.desktop
%{_datadir}/polkit-1/actions/org.fd44.btrfspatrol.policy

%changelog
%autochangelog
