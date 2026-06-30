from getpass import getpass

from app.auth import create_password_hash


if __name__ == "__main__":
    print(create_password_hash(getpass("Password (minimum 12 characters): ")))
