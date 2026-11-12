# ZDM Migration Console — Universal Installer

This directory contains the prebuilt universal GUI installer for existing
Oracle Zero Downtime Migration service hosts. The repository root retains the
original ZDM 26.1 edition.

The universal installer does not install or upgrade Oracle ZDM. It detects the
installed ZDM release through `zdmcli -build`, probes command help at runtime,
and exposes only the commands and options supported by that installed patch.

## Supported releases

| Oracle ZDM release | Supported service-host OS |
| --- | --- |
| 21.4 | Oracle Linux 7/8 or RHEL 8, x86-64 |
| 21.5 | Oracle Linux 7/8 or RHEL 8, x86-64 |
| 26.1 | Oracle Linux 7/8/9 or RHEL 8/9, x86-64 |

The same installer has been live-tested with ZDM 21.4.10, 21.5.6, and 26.1.0.
Other ZDM release lines are rejected.

## Files

- [`zdm-gui-universal-linux-x86_64.run`](zdm-gui-universal-linux-x86_64.run) — GUI installer
- [`zdm-gui-uninstall.run`](zdm-gui-uninstall.run) — standalone GUI uninstaller

SHA-256 checksums:

```text
1242ccf7a0db9ae9da11d3a1901bebb9c0417145042b123d5716ddf4c8398b2e  zdm-gui-universal-linux-x86_64.run
7de3c689d990b18df187f1c9f25f973da8ba87a603ab2dcf0434eb6b6e43a954  zdm-gui-uninstall.run
```

Verify the downloaded files before running them:

```bash
sha256sum zdm-gui-universal-linux-x86_64.run zdm-gui-uninstall.run
```

## Prerequisites

- Oracle ZDM 21.4, 21.5, or 26.1 is already installed.
- `ZDM_HOME`, `ZDM_BASE`, and `ZDM_STAGE` exist and are accessible to the ZDM
  operating-system account, normally `zdmuser`.
- The host can access its configured Linux package repositories and Python
  package index during installation.
- System installation requires root privileges. Owner-only installation
  requires Python 3.9 or newer with `venv`, SSL, and SQLite support.

Typical paths are:

```text
ZDM_HOME=/u01/app/zdmhome
ZDM_BASE=/u01/app/zdmbase
ZDM_STAGE=/u01/app/zdmstage
```

## Install with sudo privileges

```bash
chmod 0755 zdm-gui-universal-linux-x86_64.run
sudo ./zdm-gui-universal-linux-x86_64.run
```

The interactive installer asks for the GUI application/data directories, ZDM
paths and owner, optional ZDM service auto-start, HTTPS certificate hostname or
IP, and allowed browser client IPs/CIDRs.

System mode installs:

- GUI application under `/opt/zdm-gui` by default
- Persistent GUI data under `/var/lib/zdm-gui` by default
- `zdm-gui.service` and `zdm-hosts-manager.service`
- NGINX HTTPS access on TCP 443 with a self-signed certificate
- Optional `zdmservice-autostart.service`

Open `https://<hostname-or-IP>/` after installation. Trust the generated
certificate on the client or replace it with a certificate issued by your
approved certificate authority.

## Install without sudo

Run the installer as the operating-system account that owns ZDM:

```bash
chmod 0755 zdm-gui-universal-linux-x86_64.run
./zdm-gui-universal-linux-x86_64.run --user
```

User mode makes no system-wide changes. It uses private user directories and
HTTPS port 8443 by default. Display its options with:

```bash
./zdm-gui-universal-linux-x86_64.run --user --help
```

## Runtime compatibility

The console probes these installed commands:

```text
zdmcli migrate database -help
zdmcli resume job -help
zdmcli modify job -help
```

Unsupported migration switches are hidden and rejected. Resume fields are
filtered by the detected CLI. `modify job` is disabled when a patch does not
support it, including the tested ZDM 21.4.10 and 21.5.6 builds.

## Service management

```bash
sudo systemctl status zdm-gui zdm-hosts-manager nginx
sudo journalctl -u zdm-gui -n 100 --no-pager
sudo systemctl restart zdm-gui
```

The GUI service runs as the configured ZDM owner. The backend listens on
`127.0.0.1:8080`; NGINX provides external HTTPS access on port 443.

## Uninstall

Review the removal plan first:

```bash
chmod 0755 zdm-gui-uninstall.run
sudo ./zdm-gui-uninstall.run --dry-run
```

Then uninstall interactively:

```bash
sudo ./zdm-gui-uninstall.run
```

For unattended GUI removal that also removes the optional ZDM auto-start unit:

```bash
sudo ./zdm-gui-uninstall.run --yes --remove-zdm-autostart
```

The uninstaller removes the GUI application, GUI services, and GUI HTTPS
configuration. It preserves Oracle ZDM, migration files, GUI data, shared RPM
packages, firewall settings, and SELinux settings. Removing the optional
auto-start unit does not stop an already running ZDM service.

For an owner-only installation, run without sudo:

```bash
./zdm-gui-uninstall.run --user
```
