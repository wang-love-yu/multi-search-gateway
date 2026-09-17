"""Offline recovery: docker compose exec backend python -m app.cli reset-password"""
import getpass
import sys
from .core import Admin, Crypto, Database, Settings


def main():
    if sys.argv[1:] != ['reset-password']:
        raise SystemExit('Usage: python -m app.cli reset-password')
    password = getpass.getpass('New administrator password (16-128 characters): ')
    confirmation = getpass.getpass('Confirm password: ')
    if password != confirmation or not 16 <= len(password) <= 128:
        raise SystemExit('Passwords differ or length is invalid; nothing changed.')
    settings = Settings()
    crypto = Crypto(settings.master_key.get_secret_value())
    database = Database(settings.database_url)
    with database.session() as db:
        admin = db.get(Admin, 1)
        if admin is None:
            raise SystemExit('Administrator not initialized; start the service first.')
        admin.password_hash = crypto.passwords.hash(password)
        admin.session_version += 1
    database.audit('password_reset_from_cli')
    print('Password changed. All previous sessions have been revoked.')


if __name__ == '__main__':
    main()
