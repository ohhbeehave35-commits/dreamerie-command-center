from . import crm

_DEFAULT_ACCENT = "#b87ad9"


def _hex_to_rgb(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "212,175,55"
    try:
        return f"{int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)}"
    except ValueError:
        return "212,175,55"


def _lighten(hex_color: str, amt: float = 0.25) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#e6cf88"
    try:
        parts = [min(255, round(int(h[i:i+2], 16) + (255 - int(h[i:i+2], 16)) * amt)) for i in (0, 2, 4)]
        return "#" + "".join(f"{v:02x}" for v in parts)
    except ValueError:
        return "#e6cf88"


def _darken(hex_color: str, amt: float = 0.18) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#c79a2b"
    try:
        parts = [round(int(h[i:i+2], 16) * (1 - amt)) for i in (0, 2, 4)]
        return "#" + "".join(f"{v:02x}" for v in parts)
    except ValueError:
        return "#c79a2b"


def get_brand() -> dict:
    accent = crm.get_setting("brand_accent", _DEFAULT_ACCENT)
    logo_url = crm.get_setting("brand_logo_url", "")
    return {
        "accent": accent,
        "light": _lighten(accent),
        "dark": _darken(accent),
        "rgb": _hex_to_rgb(accent),
        "logo_url": logo_url,
    }


def compute_brand(accent: str = "", logo_url: str = "") -> dict:
    """Build brand vars from explicit values — does not read Airtable.
    Used by the /api/demo-brand/<slug> route to serve prospect branding."""
    accent = accent.strip() if accent else ""
    if not (accent.startswith("#") and len(accent) == 7):
        accent = _DEFAULT_ACCENT
    return {
        "accent": accent,
        "light": _lighten(accent),
        "dark": _darken(accent),
        "rgb": _hex_to_rgb(accent),
        "logo_url": logo_url.strip() if logo_url else "",
    }


def set_brand(accent: str | None = None, logo_url: str | None = None) -> dict:
    if accent is not None:
        accent = accent.strip().lower()
        if not (accent.startswith("#") and len(accent) == 7):
            accent = _DEFAULT_ACCENT
        crm.set_setting("brand_accent", accent)
    if logo_url is not None:
        crm.set_setting("brand_logo_url", logo_url.strip())
    return get_brand()
