#!/usr/bin/env python3
"""Generate a server key and CSR; send only the CSR to your private CA."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--name', required=True, help='DNS hostname or IP used to access the GUI.')
parser.add_argument('--out-dir', required=True)
args = parser.parse_args()
os.umask(0o077)
try:
    san = 'IP:' + str(ipaddress.ip_address(args.name))
except ValueError:
    if len(args.name) > 253 or not re.fullmatch(r'[A-Za-z0-9.-]+', args.name) or any(not p or len(p) > 63 or p.startswith('-') or p.endswith('-') for p in args.name.rstrip('.').split('.')):
        raise SystemExit('Enter a valid DNS hostname or IP address.')
    san = 'DNS:' + args.name
binary = shutil.which('openssl')
if not binary:
    raise SystemExit('OpenSSL is required.')
destination = Path(args.out_dir).resolve()
destination.mkdir(mode=0o700, parents=True, exist_ok=True)
if any(destination.iterdir()):
    raise SystemExit('Use an empty output directory; existing keys will never be overwritten.')
config = destination / 'request.cnf'
config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nreq_extensions=server\n[dn]\nCN=' + args.name + '\n[server]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=' + ','.join(dict.fromkeys([san, 'IP:127.0.0.1', 'IP:::1'])) + '\n')
subprocess.run([binary, 'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-sha256',
                '-keyout', str(destination / 'server.key'), '-out', str(destination / 'server.csr'),
                '-config', str(config)], check=True)
(destination / 'server.key').chmod(0o600)
print('Send only server.csr to the customer CA. Keep server.key on the GUI host.')
print('Request a serverAuth certificate with the requested SANs and CA:FALSE.')
