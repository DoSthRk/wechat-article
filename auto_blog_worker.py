"""Detached outbox worker; credentials/environment are inherited, never logged."""
from utils.auto_blog import drain

if __name__ == "__main__":
    drain()
