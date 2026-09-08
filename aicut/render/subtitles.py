"""ASS subtitle generation with externalised styling (10.3).

The format is fixed (ASS); the *look* is not. Font, size, colour and animation
live in a style profile under ``config/subtitle_styles`` so that 4.5's analysis of
how subtitles are actually being used on YouTube can update them. Baking one
house style into the renderer would contradict the whole point of the reference
learning loop, so the shipped profile is an initial value, not a specification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from aicut.errors import ConfigError
from aicut.resources import SUBTITLE_STYLE_DIR
from aicut.models import SubtitleLine

STYLE_DIR = SUBTITLE_STYLE_DIR

_FIELD_ORDER = [
    "fontname", "fontsize", "primary_colour", "secondary_colour", "outline_colour", "back_colour",
    "bold", "italic", "underline", "strike_out", "scale_x", "scale_y", "spacing", "angle",
    "border_style", "outline", "shadow", "alignment", "margin_l", "margin_r", "margin_v", "encoding",
]


class SubtitleStyleProfile:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.styles: dict[str, dict[str, Any]] = data.get("styles", {})
        if "default" not in self.styles:
            raise ConfigError("a subtitle style profile must define a 'default' style")

    @classmethod
    def load(cls, name_or_path: str = "default") -> "SubtitleStyleProfile":
        path = Path(name_or_path)
        if not path.exists():
            path = STYLE_DIR / f"{name_or_path}.json"
        if not path.exists():
            raise ConfigError(f"subtitle style profile not found: {name_or_path}")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def resolved(self, name: str) -> dict[str, Any]:
        style = self.styles.get(name)
        if style is None:
            return self.resolved("default")
        parent = style.get("inherits")
        base = dict(self.resolved(parent)) if parent else {}
        base.update({k: v for k, v in style.items() if k != "inherits"})
        return base

    @property
    def effects(self) -> dict[str, Any]:
        return self.data.get("effects", {})

    @property
    def fonts(self) -> list[str]:
        """Every font this profile names, in the order the styles are written."""
        seen: list[str] = []
        for name in self.styles:
            font = str(self.resolved(name).get("fontname", "")).strip()
            if font and font not in seen:
                seen.append(font)
        return seen

    def licence_problems(self) -> list[str]:
        """What 20.2 asks about this profile's fonts, and 10.3 requires of them.

        20.2 makes 자막 폰트의 임베딩·상업 사용 허용 여부 확인 a pre-start item,
        and 10.3 says only fonts whose licence permits both may be adopted. The
        shipped profile records its licence in `_meta.font_licence`; a profile
        written by the operator, or one that 4.5's subtitle-pattern analysis has
        updated with a new font, may not.

        This reports; it never refuses. Whether a font may be used is a fact
        about a licence the operator holds and this code cannot read - what it
        can say is that nothing in the file records the answer.
        """
        problems: list[str] = []
        licence = str((self.data.get("_meta") or {}).get("font_licence", "")).strip()
        fonts = self.fonts
        if fonts and not licence:
            problems.append(
                f"no _meta.font_licence recorded for {', '.join(fonts)}. 10.3 admits"
                " only fonts whose licence permits embedding and commercial use"
                " (SIL OFL and similar); 20.2 makes checking it a pre-start item"
            )
        # `is False`, not falsy: None means fontconfig could not be asked, and
        # reporting an unanswerable question as a missing font would send the
        # operator installing something they may already have.
        missing = [font for font in fonts if font_installed(font) is False]
        if missing:
            problems.append(
                f"not installed on this machine: {', '.join(missing)}."
                " libass substitutes silently, so the burned captions would not be"
                " the style this profile describes (10.3)"
            )
        return problems


def font_installed(name: str) -> bool | None:
    """Whether a font is on this machine. None when it cannot be determined.

    Asked through fontconfig, which is what libass uses to resolve a name - so
    this is the same question the renderer will ask. `fc-match` always answers
    with *something*, substituting when the request is unknown, so the family it
    returns has to be compared with the family that was asked for.

    Windows has no fontconfig. There the registry's font list is asked instead;
    its value names are families with the format appended - `Noto Sans KR
    (TrueType)` - so the family is what is compared.
    """
    import shutil
    import subprocess
    import sys

    if sys.platform == "win32":
        return _font_installed_windows(name)
    if not shutil.which("fc-match"):
        return None
    try:
        done = subprocess.run(
            ["fc-match", "--format=%{family}", name],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    families = {f.strip().casefold() for f in done.stdout.split(",") if f.strip()}
    return name.strip().casefold() in families


def _font_installed_windows(name: str) -> bool | None:
    """The Windows font list, machine-wide and per-user.

    Registry rather than a directory listing: a file name is not a family name,
    and the value names here are exactly the families a program asks for.
    """
    try:
        import winreg                                  # pragma: no cover - Windows only
    except ImportError:                                # pragma: no cover - elsewhere
        return None
    wanted = name.strip().casefold()
    found_any = False
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                count = winreg.QueryInfoKey(key)[1]
                found_any = found_any or count > 0
                for index in range(count):
                    value = winreg.EnumValue(key, index)[0]
                    # "Noto Sans KR (TrueType)" -> "noto sans kr"; a family with
                    # several weights is listed as "Family Bold (TrueType)", so
                    # the leading part is compared rather than the whole.
                    family = value.split("(")[0].strip().casefold()
                    if family == wanted or family.startswith(wanted + " "):
                        return True
        except OSError:
            continue
    # An empty or unreadable registry is not evidence the font is absent.
    return False if found_any else None


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _timestamp(seconds: float) -> str:
    """H:MM:SS.cc, the ASS time format.

    Rounded to whole centiseconds *first*, then decomposed. Rounding after the
    split carried into the seconds field without carrying on into minutes and
    hours, so 59.999 came out as `0:00:60.00` and 3599.999 as `0:59:60.00` -
    times a renderer either rejects or reads as something else, which loses or
    misplaces every caption landing on a minute boundary.
    """
    total = max(0, int(round(max(0.0, seconds) * 100)))
    centis = total % 100
    whole = total // 100
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def build_ass(
    lines: Sequence[SubtitleLine],
    profile: SubtitleStyleProfile,
    *,
    title: str = "aicut",
) -> str:
    """Render subtitle lines (already in output time) to an ASS document."""
    used = sorted({line.style or ("emphasis" if line.emphasis else "default") for line in lines} | {"default"})
    head = [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {profile.data.get('play_res_x', 1920)}",
        f"PlayResY: {profile.data.get('play_res_y', 1080)}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
        " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for name in used:
        style = profile.resolved(name)
        values = ",".join(_fmt(style.get(field, 0)) for field in _FIELD_ORDER)
        head.append(f"Style: {name},{values}")

    head += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    effects = profile.effects
    fade_in = int(effects.get("fade_in_ms", 0))
    fade_out = int(effects.get("fade_out_ms", 0))
    transform = effects.get("emphasis_transform", "")

    for line in sorted(lines, key=lambda l: l.start_sec):
        style_name = line.style or ("emphasis" if line.emphasis else "default")
        tags = ""
        if fade_in or fade_out:
            tags += f"\\fad({fade_in},{fade_out})"
        if line.emphasis and transform:
            tags += transform
        text = (f"{{{tags}}}" if tags else "") + _escape(line.text)
        head.append(
            f"Dialogue: 0,{_timestamp(line.start_sec)},{_timestamp(line.end_sec)},{style_name},"
            f"{_escape(line.speaker)},0,0,0,,{text}"
        )
    return "\n".join(head) + "\n"


def write_ass(
    lines: Sequence[SubtitleLine],
    path: str | Path,
    profile: SubtitleStyleProfile,
    *,
    title: str = "aicut",
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_ass(lines, profile, title=title), encoding="utf-8")
    return target
