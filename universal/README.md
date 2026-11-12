# ZDM Migration Console

React + FastAPI console for Oracle ZDM 21.4, 21.5, and 26.1.
This is a custom administration application, not an Oracle-provided GUI.

The universal edition reads `zdmcli -build` and probes command help on every
installed host. Only switches observed in that patch are exposed or accepted;
the installer does not assume that two patches in the same release have
identical capabilities.

## Universal release

The repository root remains the original ZDM 26.1 edition. This directory is
the separate universal edition for these installed ZDM release families:

| ZDM release | Supported service-host OS |
| --- | --- |
| 21.4 | Oracle Linux 7/8 or RHEL 8, x86-64 |
| 21.5 | Oracle Linux 7/8 or RHEL 8, x86-64 |
| 26.1 | Oracle Linux 7/8/9 or RHEL 8/9, x86-64 |

Download the prebuilt standard installer:

- [`zdm-gui-universal-linux-x86_64.run`](portable/dist/zdm-gui-universal-linux-x86_64.run)
- [`zdm-gui-uninstall.run`](portable/dist/zdm-gui-uninstall.run)

Installer SHA-256:

```text
1242ccf7a0db9ae9da11d3a1901bebb9c0417145042b123d5716ddf4c8398b2e
```

The same installer was live-tested on ZDM 21.4.10, 21.5.6, and 26.1.0. It
fails closed for other release lines and hides unsupported commands and
options based on the installed CLI help.

## Features

- Live ZDM service/build information, job list, phase progress, bounded result-log tail, and original CLI output.
- Separate Logical / Physical response-file forms with conditional required fields, searchable optional
  parameters, custom name/value entries, generation preview, installed templates, and `.rsp` import/editor.
- Edit existing response files in place with private backups, conflict detection, a Save as new file option, and logical DB/GoldenGate wallet selection.
- Hostname resolution checks (system resolver and DNS separately), PRGZ-3900 error parsing, and managed
  `/etc/hosts` mappings for source/target database nodes.
- Database and GoldenGate auto-login wallet creation while building logical response files, with wallet paths and usernames written into the response file.
- Saved migration profiles with source/target SSH identity paths; OCI connection-check identity is inherited from the OCI profile embedded in the selected response file.
- Evaluate and start with a command preview. Real migrations require a typed confirmation.
- Suspend, resume, abort, and delete jobs with typed confirmation. Resume uses ZDM's documented brief job query so GoldenGate metrics collection cannot hold the dialog open; submission performs another fresh brief state check. Interrupting and destructive actions retain a fresh full ZDM state check.
- Abort is also available for FAILED jobs with typed confirmation; the installed ZDM CLI determines whether the request is accepted.
- Delete job uses `zdmcli delete job -jobids ID` for a single selected job. It is enabled for FAILED, SUCCEEDED, ABORTED, TERMINATED, or REVERTED jobs, retains GUI profiles/activity history, and never uses `-force` or deletion by status. Check Activity history for the CLI result.
- SQLite submission history, request-id deduplication, and explicit unknown-outcome handling after timeout/restart.
- Managed SSH / OCI private-key import with cryptographic validation, fingerprints, and private files (0600).
- Saved source/target connections, independently confirmed SSH host fingerprints, DNS/TCP/SSH/sudo checks,
  and optional database-listener TCP checks from the ZDM host.
- OCI CLI profiles with generated configuration files and a signed read-only availability-domain API check.
- Fresh profile connection checks are required before a real migration starts. Evaluation remains available
  to diagnose the broader ZDM prerequisites.

ZDM owns migration execution. Closing the browser or restarting this GUI does not stop submitted ZDM jobs.
No job is submitted merely by creating a profile. No migrations were launched during deployment validation.

## Server deployment

Application: `/u01/app/zdm-gui`

Service: `zdm-gui.service`

Configuration: `/etc/zdm-gui.env` (root only)

History: `/u01/app/zdm-gui/data/console.sqlite`

Browser URL: `https://144.24.37.222/`

The service runs as `zdmuser` with one worker. Do not increase worker count: the command queue is in-process.
GUI access is limited to the configured client IP at NGINX, the application, host firewall, and OCI NSG.
HTTP Basic authentication protects every GUI page and API route. NGINX terminates HTTPS on port 443,
and forwards requests to Uvicorn listening only on `127.0.0.1:8080`. Uvicorn trusts forwarded headers only
from `127.0.0.1`; NGINX overwrites the forwarded client address and scheme. Keep these settings together
so client-IP restrictions, credential-upload checks, and request-origin validation remain effective.
The application's generated login is saved locally in `.credentials.json` (mode 0600, excluded from Git).
Read it locally; do not commit or publish it.

Use **Change password** at the bottom of the sidebar to update the GUI account. Enter the current password,
then enter and confirm a new password of 12–128 characters. The new password takes effect immediately;
the old password no longer authenticates, including in other browser sessions. The current page continues
with the new login in memory; reloading may ask you to sign in again. Update your password manager or local
saved login yourself: `.credentials.json` contains the initial password and is not updated by the server.

The account is stored as a salted PBKDF2-SHA256 hash in `data/console.sqlite`, not as plaintext. The configured
`ZDM_GUI_PASSWORD` is used only to initialize a missing account on first deployment; once initialized, the
stored account is authoritative and survives GUI restarts. Back up this database securely along with your
profiles and history. Password-change events record only username and timestamp, never password values.

```bash
sudo systemctl status zdm-gui
sudo journalctl -u zdm-gui -n 100 --no-pager
sudo systemctl restart zdm-gui
```

To change client IP, update `ZDM_GUI_ALLOWED_IPS` in `/etc/zdm-gui.env`, the NGINX `allow` rule,
the host firewall rich rule for port 443, and the `ZDM-GUI` OCI NSG rule. Test/reload NGINX and restart
`zdm-gui`. Preserve `127.0.0.1,::1` for local checks and SSH tunnels. Forwarded headers are trusted only
from the local NGINX proxy; direct external access to port 8080 is disabled.

```bash
ssh -i ~/.ssh/id_rsa -L 8080:127.0.0.1:8080 opc@144.24.37.222
# Open http://127.0.0.1:8080 and enter the GUI login.
```

The GUI service starts on OS boot. The vendor ZDM service itself is separately managed with
`sudo -iu zdmuser /u01/app/zdmhome/bin/zdmservice start` and `status`.

## HTTPS certificate and renewal

The direct-IP endpoint uses a publicly trusted Let's Encrypt `shortlived` certificate. IP certificates
last approximately six days. Certbot 5.8 runs in its own root-owned environment at `/opt/zdm-certbot`;
certificate files are under `/etc/letsencrypt/live/zdm-ip`. Keep private keys root-only.

Public TCP port 80 is required for HTTP-01 issuance and renewal. Only `/.well-known/acme-challenge/`
serves files, from `/var/www/zdm-acme`; all other HTTP requests redirect to HTTPS. GUI access on 443
remains limited to the administrator IP. No GUI authentication or private-key upload is handled on port 80.
The `zdm-certbot-renew.timer` checks for renewal twice daily, with a randomized delay and catch-up after
boot. Its deploy hook validates and reloads NGINX after renewal. If the server stays stopped beyond the
certificate lifetime, start it and run renewal over SSH before using the browser.

```bash
sudo systemctl list-timers zdm-certbot-renew.timer
sudo systemctl status nginx zdm-gui
sudo /opt/zdm-certbot/bin/certbot certificates
sudo /opt/zdm-certbot/bin/certbot renew --dry-run
sudo journalctl -u zdm-certbot-renew.service --no-pager
```

Deployment configurations are in `deploy/https/`. Root-only configuration backups on the host are in
`/var/lib/zdm-https-setup`. Certificate renewal must continue to reach the same public IP and port 80.
Oracle Linux SELinux remains enforcing; the HTTP proxy relay boolean allows NGINX to reach its backend.

## First migration

1. Open **Connections & keys → Private keys**. Upload an unencrypted OpenSSH/PEM private key, or register an
   existing file owned by `zdmuser`. The default generated ZDM identity is `/u01/app/zdmbase/zdm/admin/id_rsa`.
   SSH and OCI keys are separate types. SSH RSA keys of 1024 bits and above are accepted for legacy
   compatibility; the remote SSH server must also permit that key size. OCI keys must be RSA PEM keys
   with at least 2048 bits. SSH signature algorithms and host-key verification remain unchanged.
   Private files stay in `$ZDM_BASE/gui-credentials`, mode 0600, in managed directories of mode 0700.
   Private key contents are never returned by listing or detail APIs. **File uploads require HTTPS or a loopback SSH tunnel; they are rejected over public HTTP.**
   For the upload screen, run `ssh -i ~/.ssh/id_rsa -N -L 18080:127.0.0.1:8080 opc@144.24.37.222` and open
   `http://127.0.0.1:18080`. Registering an existing server file remains available over authenticated HTTP. Never distribute a private key to database hosts.
2. Authorize the corresponding public SSH key on source and target hosts with the required OS privileges.
   Add connections under **Source & target servers**, with their role, OS user, SSH port, private key, and
   optional database-listener host/port. A database-only target can be registered for Autonomous Database;
   database-only source connections are also supported for logical response-file workflows such as ADB/RDS.
3. For SSH hosts, choose **Verify host key**, compare the scanned SHA256 fingerprint with a fingerprint obtained
   independently from the DBA, then paste that verified fingerprint to confirm. Checks use a per-host pinned
   known_hosts file and `StrictHostKeyChecking=yes`; they do not silently accept new or changed host identities.
4. If OCI API access is required, import the RSA API key, add an **OCI CLI profile**, and provide its user OCID,
   tenancy OCID, and region. The fingerprint is derived from the key and compared if supplied. Each profile gets
   its own config file; existing profiles are not overwritten. **Check OCI connection** makes a signed read-only
   `iam availability-domain list` request. A failure can indicate either network/credential issues or insufficient
   IAM permission for that operation. This is not a check of every migration permission. It does not upload an
   API public key to IAM or grant any permissions; register the public key with the intended OCI user yourself.
5. In **DB & GG wallets**, enter each existing database / GoldenGate username and password and choose its
   credential purpose. Use HTTPS or the loopback SSH tunnel above for credential entry. This does not change
   the service password. Oracle `orapki` creates an auto-login-only wallet; `mkstore` receives the password
   through non-echoing prompts and saves alias `store`. Wallet files are private (0600) under
   `$ZDM_BASE/gui-credentials/wallets`, in directories of mode 0700. The GUI database stores metadata only;
   entered passwords are not saved in history, logs, command arguments, or environment variables.
   **Verify wallet** checks the Oracle wallet structure; **Evaluate** checks actual migration authentication. Wallets can also be created directly from the response-file builder or editor.
   In **Response files → New response file**, choose Logical or Physical. Required fields adapt to the
   migration method, transfer medium, and database type. Search the optional parameter dropdown, add a
   parameter, and enter its value; custom names are also supported. Preview and generate the `.rsp`.
   **Import .rsp** and **Text editor** remain available. Platform-specific requirements still need ZDM
   evaluation. Plaintext secret assignments are rejected. Files stay on the server owned by `zdmuser`.
   Use **Response files → Edit** to load an existing file into the dropdown or text editor. For logical
   response files, select saved DB / GoldenGate wallets or create a wallet from the editor; saving writes
   the wallet directory and matching username parameters into that file. **Save changes** updates
   the same path and keeps the previous contents in `$ZDM_STAGE/gui-response-backups` (0700 directory,
   0600 files). Profiles referencing that path retain the reference; changed contents invalidate their
   earlier connection checks. Recheck and evaluate before migration. **Save as a new response file**
   creates a separate copy that you can select in a profile. Installed templates are protected from
   overwrite and must be saved as copies. A stale editor cannot overwrite changes made since it opened.
6. Create or edit a migration profile and select the saved source and target connections and configured
   response file. OCI API authentication is selected in the response-file builder/editor; Autonomous target
   checks use the profile association saved in that response file. Physical wallet command-line options are
   configured under Advanced. One DB / GoldenGate wallet per purpose is allowed in a logical response file.
7. Choose **Check connections** on the migration profile. Source/target checks run from the ZDM host and verify
   DNS, the SSH TCP port, pinned host identity, SSH key authentication, `sudo -n true` (or a root login), and any
   configured database listener TCP port. This does not verify database credentials, all commands required by ZDM,
   or network paths between other migration components. Run **Evaluate** for that broader validation.
8. **Start migration** requires a passing check from the last 15 minutes for the same profile, response file,
   connections, key files, and pinned host identities. Changes or expiry require new checks. Existing manual
   profiles must be edited to select managed connections before real migration submission. Phase counts are
   not elapsed-time estimates.

## Hostname resolution / PRGZ-3900

Open **Hostname resolution** to check all required source and target node names from the ZDM host.
Paste the PRGZ-3900 message (including its quoted hostname list), or enter names separated by commas,
spaces, or new lines. **Check resolution** shows the system resolver result, DNS-only addresses,
existing hosts-file addresses, and whether each name resolves. It checks names, not SSH authentication.

If DNS is correct, no local mapping is needed. If DNS is missing, choose **Add unresolved names to entries**,
enter each node's correct IPv4/IPv6 address, and use **Save hosts entries**. A row supports a canonical
name and aliases, for example `node1.example.com node1`. The page does not change DNS server settings.
Rows can be edited or removed; changes apply when saved. Do not guess RAC node, VIP, or SCAN addresses.
Other servers may separately need DNS/hosts updates for their own migration network paths.

Only the block between `# BEGIN ZDM GUI MANAGED HOSTS` and `# END ZDM GUI MANAGED HOSTS` is modified.
Existing system entries and comments remain intact. Invalid addresses, duplicate aliases, localhost
overrides, and conflicts with protected system entries are rejected. Concurrent external edits require
a reload. Every actual change keeps a root-only backup under `/var/lib/zdm-hosts-manager`, preserves
file ownership/mode/SELinux labels, and replaces the file atomically.

The GUI still runs as `zdmuser` with `NoNewPrivileges=true` and no sudo privileges. The separate
`zdm-hosts-manager.service` runs a root-owned, standard-library-only helper installed at
`/usr/local/libexec/zdm-hosts-manager.py`. It accepts only a validated entries/revision JSON request
over `/run/zdm-hosts-manager/control.sock` (root:zdm 0660), checks the peer UID, and accepts no paths
or shell commands. The helper requires write access to `/etc` for atomic replacement; this access
is not granted to the GUI process. Its IP networking is disabled. Existing key/credential safeguards
and the GUI's HTTPS/IP authentication remain in force.

After a change, repeat **Check resolution**, **Check connections**, and **Evaluate**. Hosts-file and
resolver-configuration changes invalidate earlier profile connection-check signatures.

```bash
sudo systemctl status zdm-hosts-manager
sudo journalctl -u zdm-hosts-manager --no-pager
getent ahosts node1
getent -s dns ahosts node1
```

The OCI CLI runs in a separate Python environment so its dependencies do not conflict with the GUI. As
`zdmuser`, use the displayed config path explicitly:

```bash
oci --config-file /u01/app/zdmbase/gui-credentials/oci-profiles/PROFILE_ID/config \
    --profile ZDM iam availability-domain list --compartment-id YOUR_TENANCY_OCID
```

Back up `$ZDM_BASE/gui-credentials` securely along with `data/console.sqlite`. The latter contains references,
configuration metadata, public keys, fingerprints, and check reports, not private-key contents. Registered
existing keys retain their original paths; keep those files available for the duration of migration jobs.

The default authentication is `zdmauth` with `/usr/bin/sudo`. Alternative OS authentication and runtime
credential entry are described below. Do not place passwords in command arguments or user-action data.
The GUI does not automatically retry unknown submissions: check the ZDM repository before retrying.

## Complete migrate-database controls

The profile editor uses three sections: **Connections**, **Response file**, and **Advanced**.
Wallet creation and selection happen while creating a response file. Existing profiles retain their settings. Advanced options are grouped and searchable; common migrations
need only saved connections and a configured response file. The reference is Oracle ZDM 26.1 H.5,
checked against `zdmcli migrate database -help` from this installed server.

- Core: sourcedb/sourcesid (optional for response-file-only logical workflows), source/target node,
  response file, target Oracle home, and named source/target connections.
- Authentication: plugin (`zdmauth`, `dbuser`, `sudoasuser`, or a named custom plugin), root, named
  credential, privileged user, sudo user/path, or no OS authentication for logical connection-only
  workflows. Numbered source/target plugin arguments 1–15 can override generated defaults. Secret
  properties and private-key text are rejected. SSH-port arguments use arg14, or arg15 for sudoasuser.
- Advisor/checks: normal CPAT, ignore CPAT errors, skip CPAT, YES/NO fixups, ignored check categories,
  and missing-patch exceptions. `genfixup=YES` can apply automatic remedies on the source database.
- Timing/phases: NOW, a scheduled ISO-8601 date/time, PAUSE scheduling, job tag, pauseafter, stopafter,
  and incremental RMAN interval. Scheduling preserves your local time as an offset-bearing timestamp.
  stopafter completes the workflow permanently at that phase; it cannot resume beyond that phase.
- Backup/encryption: backup/restore user, Database Vault owner, source SYS, source/target TDE,
  OSS backup/restore, Vault, and RMAN password wallet paths; password flags for source/target TDE,
  source/target OKV, RMAN backup, and TDE master key. Saved physical wallets populate these flags and
  matching user names; OKV wallets populate the physical response-file wallet parameters.
- User actions: an already registered image type and non-secret user-action data. Registering image
  types/user actions is a separate CLI administration task, not part of migrate database.

After saving a profile, use **Evaluate**, **Start migration**, or **List phases**. Logical profiles also
show **Advisor only** and **CPAT summary** directly; there is no More actions menu. The migration type
is read from the current response file, and the backend rejects CPAT-only actions for physical or
unconfigured methods. CPAT policy/fixup controls are hidden for physical workflows; previously saved
CPAT preferences are preserved but not passed to physical migration commands. Each action shows its exact command before submission. Real migrations retain
START confirmation and fresh connection checks. Commands are queued once and outcomes are recorded.
Phase lists and summary output are available in Activity history. Closing the browser does not stop ZDM.

For password-based authentication, set a saved SSH connection's **Authentication validation** to
**Network / pinned identity only; validate credentials with ZDM** if a GUI SSH-key login is unavailable.
The host fingerprint is still required. Reports explicitly mark OS authentication DEFERRED. This checks
network/identity, not the selected root/credential/custom authentication; use Evaluate for that validation.
A standard plugin still requires an imported SSH key. Database-only connections check DNS/listener TCP.

### Masked runtime credentials

When the CLI asks for a password, passphrase, secret, or TDE master key, a **ZDM is waiting for a
credential** banner appears. Choose **Enter credential**, provide the existing value in the masked form,
and submit through HTTPS or a loopback SSH tunnel. Each prompt expires in three minutes; the CLI
submission session is bounded to fifteen minutes. The process has terminal echo disabled and no PTY
logging. Values are kept only in memory, used for that prompt, redacted from captured output, and
removed when the command completes. They are never added to GUI profiles, argv, environment variables,
SQLite history, or error text. The underlying Oracle ZDM service may maintain its own encrypted
credential repository as documented by Oracle. Wallets remain preferable for repeatable unattended use.
If a session times out or the GUI restarts, inspect ZDM jobs before retrying; accepted server-side jobs
are not automatically stopped or retried.

### Reference and installed-version differences

The installed help includes `jobtag`, `schedule PAUSE`, `summary`, `DB_NAME_CHECK`, and OKV prompt flags;
these are exposed. The reference's `tdewallet`/`tgttdewallet` aliases normalize to the installed
`tdekeystorewallet`/`tgttdekeystorewallet` switches. The documented `srcred` source OS wallet switch is
in Compatibility because this installation does not advertise it in help; Evaluate reports whether it
is accepted. The reference repeats `tgtcred` with both credential-name and wallet descriptions;
the installed help calls it a named OS credential. The target credential field accepts either a name
or a validated Oracle wallet directory; ZDM determines compatibility. No arbitrary shell-command field
is provided, and unsupported option names are rejected.

## Development and verification

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..
python3.11 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Set `ZDM_HOME`, `ZDM_BASE`, `ZDM_STAGE`, `ZDM_GUI_USERNAME`, `ZDM_GUI_PASSWORD`, and
`ZDM_GUI_ALLOWED_IPS` before launching with Uvicorn. Use one worker.

Oracle references:
- https://docs.oracle.com/en/database/oracle/zero-downtime-migration/26.1/zdmug/provide-passwords-non-interactively-using-wallet.html
- https://docs.oracle.com/en/database/oracle/zero-downtime-migration/26.1/zdmug/zero-downtime-migration-zdmcli-command-reference.html
- https://docs.oracle.com/en/database/oracle/zero-downtime-migration/26.1/zdmug/installing-zero-downtime-migration-software.html

ZDM reboot startup is managed by `zdm-server.service`, installed with
`deploy/zdm-server.service` and `deploy/zdm-server-boot`. It runs as `zdmuser`,
uses Oracle `zdmservice start` / `stop`, and verifies `Running: true` on startup.
A stale PID file is preserved with a timestamp only if its numeric PID no longer
exists in `/proc`. The unit is enabled for `multi-user.target`; the GUI and NGINX
also remain enabled independently. Check with `systemctl status zdm-server` and
`sudo -iu zdmuser /u01/app/zdmhome/bin/zdmservice status`.

Public HTTPS access: set `ZDM_GUI_ALLOWED_IPS=*` in `/etc/zdm-gui.env` to allow
any client IP while retaining account authentication, same-origin checks, and
HTTPS-only credential uploads. NGINX and the host/OCI firewalls must also allow
TCP 443. Keep the application bound to `127.0.0.1:8080` and proxy trust restricted
to loopback. Never expose the backend port directly.

### Customer-configurable AI assistant

The AI assistant page and each job's Ask AI button provide read-only
troubleshooting and workflow planning. AI is disabled until configured. No model
or paid subscription is selected automatically. Configure provider name, complete
HTTPS chat-completions endpoint, model/deployment ID, API format, bearer/API-key
header authentication (or an approved gateway with none), and output-token limit.
OpenAI chat completions and compatible gateways are supported. OCI native signing,
Anthropic native Messages, and other native formats need a compatible gateway.
Settings apply to one installation/shared administrator account; this is not
multi-tenant isolation or separate customer accounts.

Approve endpoint hosts in a root-owned systemd drop-in, for example
`/etc/systemd/system/zdm-gui.service.d/ai.conf`:

```ini
[Service]
Environment=ZDM_AI_ALLOWED_HOSTS=api.openai.com,customer-gateway.example.com
```

Run `systemctl daemon-reload` and restart zdm-gui when no command submission is
pending. Private gateways additionally need `Environment=ZDM_AI_ALLOW_PRIVATE=true`.
HTTPS verification stays enabled. Loopback/link-local/metadata destinations are
blocked; port 443 is required. Redirects and ambient proxies are not followed.
Approve only hosts whose DNS and TLS configuration you trust. The default approved
host is api.openai.com; that neither selects a model nor enables AI.

Settings are in data/ai-settings.json (0600). API keys are encrypted using a
Fernet key in data/ai-encryption.key (0600). Protect/back up both together; this
is local encryption, not external KMS. Saved API keys are never returned. Changing
their destination requires re-entry/removal. Inputs, outputs, concurrency and
request duration are bounded. Connection tests send only a generic question and
may incur provider charges. No real provider has been configured or tested here.

Job context requires consent: structured status/phases and, separately, filtered
PRGZ/ORA error and ZDM phase lines from the result-log tail. Raw commands, wallet
contents and private-key files are not gathered. Secret-pattern filtering applies
to outgoing text but cannot detect all arbitrary unlabeled secrets. Do not paste
credentials. Chat includes up to six previous messages and stays in browser memory
only; provider retention policies apply. Context/configuration changes clear this
tab's conversation. Revision checks block stale tabs after provider changes.

The assistant receives concise reference material from the Oracle ZDM 26.1 command
reference and installation prerequisites. This is not live web search, MOS, or a
full documentation index. Suggestions require review. Workflow setup uses guidance
followed by the existing profile/response forms. AI has no execution tools and
cannot save files, upload keys or execute migration actions.

OpenAI endpoints automatically use `max_completion_tokens`, even when an older
saved configuration says compatible. Saving an api.openai.com endpoint normalizes
its format to OpenAI. Connection tests use the configured output-token budget
and only a generic OK question, so reasoning tokens are not restricted to 64.
Provider error codes are translated into safe explanations for authentication,
model access, unsupported parameters, rate limits, and exhausted API credits;
raw provider error messages are not echoed.

### Delete and restore inventory

DB & GG wallets and Connections & keys now have Delete buttons for wallets,
server connections, private keys and OCI profiles. Confirm using DELETE followed
by the exact item name. Deletion moves the database record to deleted_inventory;
Deleted items on the corresponding page supports RESTORE plus the exact name.
This is recoverable deletion from the GUI, not secure erasure or revocation.
Wallet/private-key/OCI configuration files remain at their original paths so
existing ZDM jobs and response files can continue using them. Existing server
files are never removed. GUI records in use by saved migration profiles or active
connections/OCI profiles cannot be deleted. References entered directly into
response files are not scanned; their files remain available.

Delete/restore requests are blocked during pending GUI CLI submissions. Link
validation is repeated inside profile/connection/OCI write transactions. Restore
requires original files and referenced keys to exist; restore keys first, then
connections/OCI profiles. Restored connection checks are cleared and preflight
checks invalidated. Re-run connection checks and Evaluate before migration use.
No permanent purge endpoint is provided.

### Users and roles

Administrators can open Users & roles to add named accounts, change roles,
enable/disable accounts, and reset another user's password. Roles apply to the
whole installation, not isolated customer tenants:

- Viewer: read jobs/phases/logs, profiles, responses and configuration; change
  their own password. No migration commands, probes, inventory edits or AI calls.
- Manager: Viewer access plus migration/configuration/credential management and
  AI chat. Cannot manage users or change/test AI-provider configuration.
- Administrator: Manager access plus user and AI-provider administration.

Permissions are enforced in HTTP middleware before handlers run, as well as in
the GUI. The legacy administrator is migrated once into console_users with its
existing salt/hash, retaining its password and Administrator access. Credentials
are checked against the enabled account and current role on each request. The
last enabled Administrator cannot be disabled or demoted. Disabling a user,
changing a role or resetting a password takes effect on subsequent requests;
requests already authorized can finish.

New/reset accounts must change their temporary password before any other API
activity. Usernames are case-insensitive (new names stored lowercase), 3–64
letters/numbers/dots/underscores/hyphens. Passwords require 12–128 characters;
PBKDF2-SHA256 uses 600,000 iterations and independent random salts. Passwords
are not returned. Administrators must share temporary passwords through their
own approved channel. User-management requests require HTTPS or a loopback SSH
tunnel. User/role/reset events are recorded in user_events without passwords.
The original console_account password hash is synchronized for rollback.

Each user signs in to the HTTPS GUI with their own username and password. The GUI uses the sign-in form and a server session. Use Logout to switch accounts. Account management does not create Linux,
Oracle database, GoldenGate or OCI IAM users. No additional production user
accounts are created automatically by deploying this feature.

### Logout and browser sessions

Logout is available in the sidebar for every role. It revokes the current server
session, clears its cookie and unmounts the GUI so drafts and displayed data are
removed from the screen. Other open GUI tabs are notified via BroadcastChannel.
Logout does not stop ZDM jobs or change any stored passwords, profiles or keys.

The public root page and static assets contain only the sign-in shell; all data
APIs still require authentication. Browser requests require a session and never
fall back to cached HTTP Basic credentials. Sign in again through the form after
upgrading. Explicit non-browser API clients can continue using Basic auth.

Random session tokens are stored as SHA-256 hashes in console_sessions with an
8-hour absolute expiry. Cookies are HttpOnly, SameSite=Strict and Secure over
HTTPS (loopback SSH-tunnel previews can use HTTP). Login/logout require the same
origin and custom request header as other writes. Login is available over HTTPS
or loopback only. Password changes revoke existing sessions and rotate the
current browser session; admin resets revoke all of that user's sessions.
Disabling an account revokes its sessions, and roles are checked on every request.
The GUI does not store passwords or session tokens in localStorage.

### Saved OCI profiles in response files and Autonomous targets

The response builder (form, import and text modes) and response editor have a
Saved OCI CLI profile selector. For logical migrations this copies the saved
region, user OCID, tenancy OCID, fingerprint and private-key file path into the
five OCIAUTHENTICATIONDETAILS_* parameters. Private-key contents are not copied.
The editor can apply the selection to its draft for review before saving. Saving
uses the current saved profile values, replaces duplicate OCI field entries,
and retains the existing backup/revision checks. PROFILE is not used as an OCI
CLI profile-name shortcut. A GUI_OCI_PROFILE_ID comment records the selection;
new migration profiles can infer the saved OCI profile for API checks from it.
This is a snapshot; later CLI-profile changes do not silently rewrite files.
Physical templates do not support these logical OCI API parameters and keep
their documented Object Storage/wallet settings instead.

When the current response file declares a logical migration and
TARGETDATABASE_DBTYPE=ADBS, ADBD or ADBCC, the GUI identifies an Autonomous target.
It hides target host/SSH/authentication fields, clears obsolete target connection
settings when saving a profile, and omits targetnode, target authentication and
targethome from generated commands. Logical source OS/SSH settings and GUI checks
are optional. Physical profiles still require saved source/target connections for preflight.

Autonomous preflight uses a saved OCI profile matching the response-file fields
and TARGETDATABASE_OCID, calling the read-only `oci db autonomous-database get`.
It verifies access to that exact resource and lifecycle state AVAILABLE, without
SSH or modifying the database. It does not need an availability-domain-list API
call. API access does not prove SQL connectivity or credential validity; run ZDM
Evaluate. Logical GUI checks are advisory; failed/expired checks do not block
logical Start. Physical Start still requires fresh successful checks. No response
files, databases or migration jobs are modified by deploying this feature.

### Response-file deletion and editing

Managers and administrators can delete owned, non-template response files from
Response files. Oracle installation templates, template directories and standard
ZDM template filenames remain protected. Deletion requires the exact filename and
a matching file revision, and is blocked by saved profile references, pending GUI
commands or active/resumable ZDM job references. If the job repository cannot be
checked, deletion fails safely. Files move into a private archive and can be
restored from Deleted response files; restore never overwrites an existing file.
No permanent purge is exposed.

Edit offers Fields & dropdowns and Text editor for the same draft. Known choice
parameters have dropdowns and a Use a text value override; other parameters use
text fields. Logical/physical catalogs provide required-field guidance and an
optional/custom parameter picker. Switching modes preserves comments, custom
parameters, ordering and line endings. Existing backup and revision safeguards
still apply when saving; templates can only be saved as new copies. Viewers can
inspect both modes without saving or deleting. Run Evaluate to validate settings.

### Logical migration without source SSH

Oracle 26.1 documents logical migration without source SSH access:
https://docs.oracle.com/en/database/oracle/zero-downtime-migration/26.1/zdmug/migrating-with-zero-downtime-migration.html

For ONLINE_LOGICAL and OFFLINE_LOGICAL profiles, Source access mode offers
Response file only (no source SSH) or configured source access. The response-only
mode clears source OS connection/key/authentication and source database CLI
identifier references; commands omit -sourcenode, -sourcedb/-sourcesid and source
OS authentication arguments. Source database host/port/service and credentials
remain necessary in the response file/wallets. Configured SSH access remains
available; existing configured profiles are retained.

GUI connection checks are advisory for logical workflows, including Autonomous
targets. Unselected connections are explicitly marked skipped; selected endpoints
can still be checked. Missing, failed, expired or stale GUI checks do not block
logical Start. This does not bypass ZDM's own prerequisite and credential checks.
Run Evaluate before migration and configure the documented no-SSH prerequisites
for the selected transfer medium; remote CPAT requires RUNCPATREMOTELY=TRUE.
Physical/other workflows retain mandatory fresh GUI preflight. Authentication,
roles, typed Start confirmation, submission idempotency and concurrent-configuration
checks remain enforced. No migration is launched as part of deployment/testing.

### OCI connection-check output

Both signed availability-domain and Autonomous target probes request JSON
explicitly and keep stdout separate from stderr. Successful OCI API-key advisory
warnings no longer corrupt JSON parsing. Non-zero CLI exit status still returns
bounded, redacted OCI diagnostics; blank, malformed or unexpected JSON has a
clear CLI-output error. No signing key, IAM policy or database state is modified.
The ZDM service navigation item appears immediately below DB & GG wallets.

### Resume job controls

The job detail Resume action shows controls after it identifies the workflow from
the ZDM migration method, workflow phases, and scheduled response file. If that
evidence is missing or conflicts, only the common pause-after-phase option is
shown. Evaluation and aborted jobs cannot be resumed.

Both logical and physical workflows can add `-pauseafter`. Logical resume offers
an optional configured logical response file (`-rsp`) or one Data Pump error
choice (`-ignore IMPORT_ERRORS|EXPORT_ERRORS`); the error choice appears only
for failed jobs. Physical resume offers `-rerun BACKUP|RESTORE` and the supported
`-skip` choices. RESTORE is clearly marked and requires acknowledgement because
ZDM reruns restore by dropping and recreating the target. Each option is reviewed
as a command against fresh job state, then protected with typed confirmation. The
preview revision binds the selected options and response file bytes to the resumed
job metadata. This interface adds no XMode task switches; those do not apply to
logical or physical database workflow selection.
