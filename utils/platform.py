"""for package download"""

import platform


def get_exe_ext() -> str:
    """Get exe ext
    Returns
    str
        if in window then return "exe" other return ""
    """
    if "windows" in platform.system().lower():
        return ".exe"
    return ""
