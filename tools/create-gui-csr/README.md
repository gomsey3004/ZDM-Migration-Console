# ZDM GUI private-CA CSR helper

`create-gui-csr.py` generates a 3072-bit RSA private key and a PKCS#10
certificate signing request (CSR) for the Universal ZDM GUI. Use it when the
GUI will use a certificate issued by an organization/private certificate
authority instead of the installer's generated self-signed certificate.

## Requirements

- Python 3
- OpenSSL available in `PATH`
- An empty or nonexistent output directory

Run the helper on the ZDM GUI host so the private key does not need to leave
that server.

## Generate a key and CSR

Use the exact DNS hostname or IP address that administrators will enter in
their browsers:

```bash
python3 create-gui-csr.py \
  --name 129.146.246.145 \
  --out-dir "$HOME/zdm-gui-csr-129.146.246.145"
```

For a DNS name:

```bash
python3 create-gui-csr.py \
  --name zdm-gui.example.com \
  --out-dir "$HOME/zdm-gui-csr-zdm-gui.example.com"
```

The helper creates:

| File | Purpose |
| --- | --- |
| `server.key` | Unencrypted server private key. It is created with mode `0600`; never send it to the CA or copy it to a client. |
| `server.csr` | CSR to submit to the private CA. This is the only generated file that should be sent to the CA. |
| `request.cnf` | OpenSSL request configuration recording the requested identity and extensions. |

The CSR requests `CA:FALSE`, `serverAuth`, digital-signature/key-encipherment
usage, the selected DNS/IP identity, and loopback IP SANs.

## Inspect the CSR

```bash
openssl req \
  -in "$HOME/zdm-gui-csr-129.146.246.145/server.csr" \
  -noout -verify -subject -text
```

Confirm that the Subject Alternative Name contains the browser identity before
submitting the request.

## Submit it to the private CA

Request a TLS/Web Server certificate and send only `server.csr`. The issued
leaf certificate must:

- retain the requested DNS/IP SAN;
- explicitly allow TLS Web Server Authentication (`serverAuth`);
- be an end-entity certificate (`CA:FALSE`);
- match the generated `server.key`;
- be returned in PEM format.

Obtain these public files from the CA:

- `server-fullchain.pem`: leaf certificate first, followed by any intermediate
  CA certificates. Do not add the root CA to this file.
- `root-ca-bundle.pem`: trusted public root CA certificate(s).

The CA private key must never be placed on the ZDM server.

## Set ownership and permissions

For a root-managed GUI installation, the server key must be owned by `root` or
by the selected ZDM service account. For example, if ZDM runs as `zdmuser`:

```bash
sudo chown zdmuser:zdm "$HOME/zdm-gui-csr-129.146.246.145/server.key"
sudo chmod 0600 "$HOME/zdm-gui-csr-129.146.246.145/server.key"
chmod 0644 \
  "$HOME/zdm-gui-csr-129.146.246.145/server-fullchain.pem" \
  "$HOME/zdm-gui-csr-129.146.246.145/root-ca-bundle.pem"
```

## Install the Universal GUI

From the directory containing the Universal installer:

```bash
sudo ./zdm-gui-universal-linux-x86_64.run \
  --tls-mode private-ca \
  --tls-cert "$HOME/zdm-gui-csr-129.146.246.145/server-fullchain.pem" \
  --tls-key "$HOME/zdm-gui-csr-129.146.246.145/server.key" \
  --tls-ca "$HOME/zdm-gui-csr-129.146.246.145/root-ca-bundle.pem"
```

When prompted for the HTTPS certificate name, enter the same DNS hostname or IP
address used with `--name`. Client browsers must trust the public root CA.

## Generate another request

The helper never overwrites an existing key. To generate another CSR, choose a
new empty output directory. Preserve the original private key when renewing a
certificate only if that matches your organization's key-rotation policy.
