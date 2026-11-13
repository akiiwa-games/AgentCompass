"""Environment variables as the user has set them, including Windows user variables set after this process started."""
import os


def user_env(name):
    """Also see Windows user variables set after the parent process started."""
    value = os.environ.get(name)
    if not value and os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                value = winreg.QueryValueEx(key, name)[0]
        except OSError:
            pass
    return value if isinstance(value, str) and value else None
