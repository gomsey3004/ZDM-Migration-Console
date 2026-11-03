# ZDM Migration Console Linux Installer

This release installs the ZDM Migration Console on an existing Oracle Zero Downtime Migration 26.1 Linux service host. It does not include or install Oracle ZDM itself; it uses that host's existing `zdmcli` and `zdmservice` commands.

## Supported host

Use an Oracle Linux or Red Hat Enterprise Linux x86-64 host that is already running ZDM. Oracle lists Oracle Linux 7, 8, and 9 and RHEL 8 and 9 as ZDM 26.1 service-host platforms ([platform requirements](https://docs.oracle.com/en/database/oracle/zero-downtime-migration/26.1/zdmug/introduction-to-zero-downtime-migration.html), [installation prerequisites](https://docs.oracle.com/en/database/oracle/zero-downtime-migration/26.1/zdmug/installing-zero-downtime-migration-software.html)). The installer accepts exactly OL 7/8/9 and RHEL 8/9. It chooses Python 3.8 or newer and matching development headers: OL7 uses Oracle's `rh-python38` software collection; OL8 and RHEL8 use an available Python 3.8–3.11 runtime; OL9 and RHEL9 use an available Python 3.9–3.11 runtime. The GUI installs into isolated virtual environments and does not replace the OS Python or ZDM's runtime.

## Install

On a new ZDM installation, create the stage directory before starting the GUI installer. Replace `zdmuser` if ZDM runs under a different operating-system account:

```bash
ZDM_OS_USER=zdmuser
ZDM_STAGE=/u01/app/zdmstage
sudo install -d -o "$ZDM_OS_USER" -g "$(id -gn "$ZDM_OS_USER")" -m 0750 "$ZDM_STAGE"
```

The stage directory must be an existing absolute path owned by, or writable by, the ZDM operating-system account. The GUI creates a `gui-responses` directory inside it for generated response files and related working content.

Copy `zdm-gui-linux-x86_64.run` to the ZDM host and run:

```bash
chmod 755 zdm-gui-linux-x86_64.run
sudo ./zdm-gui-linux-x86_64.run
```

The installer detects the Linux distribution, displays missing system packages, and asks before using `dnf` or `yum` to install them. It prompts for the ZDM home, base, and stage directories and the existing ZDM operating-system account. Confirm that the ZDM home contains executable `bin/zdmcli` and `bin/zdmservice`.

On a new GUI database, the installer generates a random initial `zdmadmin` password and displays it once after a successful install. The app receives it through a temporary root-only file under `/run`; the installer removes that file after account initialization and the database stores only a password hash. Sign in with the generated password and change it when prompted on first run. Existing GUI accounts, password hashes, profiles, and audit history are preserved on upgrade.

The installer creates or updates `zdm-gui.service` and `zdm-hosts-manager.service`, enables both at boot, installs isolated Python environments for the GUI and OCI CLI, and keeps the web app bound to `127.0.0.1:8080`. The environments remain root-owned and non-writable by the service account, with read/execute permissions so the configured ZDM OS account can run them. HTTPS through NGINX is mandatory. The installer asks for the hostname or IP address users will visit, the browser client IP/CIDR allowlist, and whether to open TCP 443 in an active `firewalld` zone. It generates a self-signed certificate and enables NGINX at boot.

The installer also asks whether to start `zdmservice` automatically after each reboot. If enabled, it installs and enables a separate `zdmservice-autostart.service` systemd unit that runs the existing `$ZDM_HOME/bin/zdmservice start` and `stop` commands as the configured ZDM OS account. The default is No. Declining this option disables and removes only that installer-managed unit. The installer does not install or upgrade Oracle ZDM itself.

## System packages

The installer detects the OS release, checks and offers to install the matching RPM packages, and enables any required Oracle Linux repositories before installing their contents:

- Python 3.8 or newer and matching development headers (OL7 uses `rh-python38`)
- `gcc`, `openssl-devel`, `libffi-devel`, `glibc-devel`
- `expect`, `unzip`, `libaio`, `sqlite`; `libnsl` on OL8/9 and RHEL8/9
- `nginx` for the mandatory HTTPS reverse proxy
- OL7: `oraclelinux-developer-release-el7`, `oracle-softwarecollection-release-el7`, `scl-utils`, `rh-python38`, and the Oracle EPEL release package when NGINX is missing
- OL8: `oraclelinux-developer-release-el8` and `ncurses-compat-libs`
- OL9 and RHEL: available Python runtime/development packages; no Oracle Linux release RPMs

Oracle's ZDM prerequisites are separate from the GUI. Follow the Oracle ZDM installation documentation for the selected OS and ZDM build. Oracle Linux 8 minimal installations may need `unzip`, `libaio`, `libnsl`, `oraclelinux-developer-release-el8`, and `ncurses-compat-libs`.

The installer downloads pinned Python packages into isolated environments: FastAPI, Uvicorn, Pydantic, HTTPX, Cryptography, Pexpect, and OCI CLI. Pip is kept on a release compatible with Python 3.8. Access to configured RPM and Python package repositories is needed during installation. The prebuilt web interface is bundled, so Node.js and pnpm are not required on the ZDM host. EL7 repository availability depends on the customer's enabled Oracle Linux 7 channels and current mirror access.

## Open the GUI over HTTPS

The installer generates and configures a self-signed certificate automatically. No certificate files need to be copied to the client. Open the URL shown at the end of installation:

```text
https://<host-name>
```

The browser will report that the certificate is not trusted. Use its advanced or continue option to accept the warning and open the console, then sign in as `zdmadmin`. Use the exact hostname or IP address entered during installation because that value is included in the certificate. The connection is encrypted, but the self-signed certificate does not independently verify the server's identity.

For a private SSH tunnel, map the NGINX HTTPS listener rather than the backend:

```bash
ssh -N -L 18443:127.0.0.1:443 <ssh-user>@<zdm-host>
```

Then browse to `https://127.0.0.1:18443`, accept the self-signed certificate warning, and sign in. The generated certificate includes the loopback IPs for this tunnel use.

The certificate and private key remain on the server at `/etc/pki/tls/certs/zdm-gui.crt` and `/etc/pki/tls/private/zdm-gui.key`. Importing the certificate into client trust stores is optional and is only needed to remove the browser warning.

The allowlist is enforced both by NGINX and the application. The default allows only loopback clients, which works with the SSH tunnel. To enable direct access, add the administrator's public IP/CIDR during installation. Entering `0.0.0.0/0,::/0` permits every client that can reach the host. For OCI, also add an ingress rule for TCP 443 in the instance subnet's security list or network security group; the installer cannot change OCI network rules. Do not expose port 8080. The certificate is self-signed and does not provide public CA validation.

## Service and data

```bash
sudo systemctl status zdm-gui zdm-hosts-manager
sudo journalctl -u zdm-gui -n 100 --no-pager
sudo systemctl restart zdm-gui
```

If ZDM auto-start was enabled during installation, check it with `sudo systemctl status zdmservice-autostart`.

The default application directory is `/opt/zdm-gui`; the default data directory is `/var/lib/zdm-gui`. The installer prompts for both and reuses an existing GUI database when you select its current data directory. Before replacing an existing GUI code directory, it saves a protected backup. Back up the GUI database and ZDM credential/key storage using your normal secure backup process.

## Build the executable release

On the build computer, with Node.js and pnpm installed:

```bash
./portable/build-linux.sh
```

The output defaults to `portable/dist/zdm-gui-linux-x86_64.run`. It is a self-extracting shell installer, not a native ELF binary, so it can run on compatible x86-64 Linux hosts without matching the build computer's OS. It is Linux-only and targets ZDM 26.1.
