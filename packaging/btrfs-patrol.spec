Name:           btrfs-patrol
Version:        0.6.0
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

%check
%pyproject_check_import
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
%{_mandir}/man8/btrfs-patrol.8*
%config(noreplace) %{_sysconfdir}/dnf/libdnf5-plugins/actions.d/btrfs-patrol.actions
%{_unitdir}/btrfs-patrol-snapshot.service
%{_unitdir}/btrfs-patrol-snapshot.timer

%changelog
%autochangelog
