#!/usr/bin/env python3
"""Create an initial private .env. Never overwrites existing encryption keys."""
import argparse
import base64
import os
import re
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--domain', required=True, help='Public DNS hostname, without scheme or path')
    parser.add_argument('--username', default='owner')
    parser.add_argument('--output', default='.env')
    args = parser.parse_args()
    if len(args.domain) > 253 or not re.fullmatch(r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}', args.domain):
        parser.error('Provide a valid DNS hostname, such as search.example.com')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', args.username):
        parser.error('Username must use 1-40 letters, digits, underscores or hyphens')
    password = secrets.token_urlsafe(24)
    values = {'DOMAIN': args.domain.lower(), 'ADMIN_USERNAME': args.username, 'BOOTSTRAP_PASSWORD': password,
              'MASTER_KEY': base64.b64encode(os.urandom(32)).decode(),
              'POSTGRES_PASSWORD': secrets.token_urlsafe(32), 'REDIS_PASSWORD': secrets.token_urlsafe(32)}
    try:
        fd = os.open(Path(args.output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error('Output already exists. Refusing to overwrite credentials or MASTER_KEY.')
    with os.fdopen(fd, 'w') as handle:
        handle.write('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
    print(f'Created {args.output} with mode 0600. Keep it private and back it up separately.')
    print(f'Administrator: {args.username}\nInitial password (save now): {password}')
    print(f'After deployment, open https://{args.domain}/admin/')


if __name__ == '__main__':
    main()
