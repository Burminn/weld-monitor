"""Ustawienia wrażliwe pobierane ze zmiennych środowiskowych (nie z plików w repozytorium)."""
import os

WEAK_PASSWORDS = {"", "aaaa", "password", "robot", "admin", "1234"}


def robot_password(robot):
    """Hasło do OPC UA robota: zmienna ROBOT_<ID>_PASSWORD (albo nazwa z pola password_env),
    awaryjnie pole `password` z config.json (niezalecane)."""
    env_name = robot.get("password_env") or f"ROBOT_{robot['id']}_PASSWORD"
    return os.environ.get(env_name) or robot.get("password", "")


def is_weak(password):
    return password.strip().lower() in WEAK_PASSWORDS


def panel_credentials():
    """(login, hasło) do panelu z MONITOR_USER / MONITOR_PASSWORD albo None, gdy logowanie wyłączone."""
    user, password = os.environ.get("MONITOR_USER", ""), os.environ.get("MONITOR_PASSWORD", "")
    if user and password:
        return user, password
    return None


def is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1")
