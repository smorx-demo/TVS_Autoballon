"""
ISO 286-1/-2 limits and fits: standard tolerance grades (IT) and fundamental deviations.

limits("H", 7, 35.0) -> (0.025, 0.0)   # (upper, lower) deviations in mm
limits("g", 6, 42.0) -> (-0.009, -0.025)

Covers nominal sizes 0–500 mm, IT1–IT18, shafts d e f g h js k m n p r s and
holes D E F G H JS K M N P R S. Returns None when outside the tabulated scope.
"""

from __future__ import annotations

# Size steps: "above X up to and including Y" (mm)
_STEPS = [3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500]

# Standard tolerance grades, µm (ISO 286-1 Table 1)
_IT = {
    1: [0.8, 1, 1, 1.2, 1.5, 1.5, 2, 2.5, 3.5, 4.5, 6, 7, 8],
    2: [1.2, 1.5, 1.5, 2, 2.5, 2.5, 3, 4, 5, 7, 8, 9, 10],
    3: [2, 2.5, 2.5, 3, 4, 4, 5, 6, 8, 10, 12, 13, 15],
    4: [3, 4, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20],
    5: [4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27],
    6: [6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40],
    7: [10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63],
    8: [14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97],
    9: [25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155],
    10: [40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250],
    11: [60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400],
    12: [100, 120, 150, 180, 210, 250, 300, 350, 400, 460, 520, 570, 630],
    13: [140, 180, 220, 270, 330, 390, 460, 540, 630, 720, 810, 890, 970],
    14: [250, 300, 360, 430, 520, 620, 740, 870, 1000, 1150, 1300, 1400, 1550],
    15: [400, 480, 580, 700, 840, 1000, 1200, 1400, 1600, 1850, 2100, 2300, 2500],
    16: [600, 750, 900, 1100, 1300, 1600, 1900, 2200, 2500, 2900, 3200, 3600, 4000],
    17: [1000, 1200, 1500, 1800, 2100, 2500, 3000, 3500, 4000, 4600, 5200, 5700, 6300],
    18: [1400, 1800, 2200, 2700, 3300, 3900, 4600, 5400, 6300, 7200, 8100, 8900, 9700],
}

# Shaft fundamental deviations, µm (ISO 286-1 Table 2/3), per main size step.
# a–h: upper deviation es; k–s: lower deviation ei.
_SHAFT_ES = {
    "d": [-20, -30, -40, -50, -65, -80, -100, -120, -145, -170, -190, -210, -230],
    "e": [-14, -20, -25, -32, -40, -50, -60, -72, -85, -100, -110, -125, -135],
    "f": [-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62, -68],
    "g": [-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18, -20],
    "h": [0] * 13,
}
_SHAFT_EI = {
    "k": [0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5],  # IT4–IT7 only; 0 otherwise
    "m": [2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21, 23],
    "n": [4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37, 40],
    "p": [6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62, 68],
}
# r and s change inside the 50–120 steps: (upper bound mm, ei µm)
_SHAFT_EI_FINE = {
    "r": [(3, 10), (6, 15), (10, 19), (18, 23), (30, 28), (50, 34), (65, 41), (80, 43), (100, 51), (120, 54)],
    "s": [(3, 14), (6, 19), (10, 23), (18, 28), (30, 35), (50, 43), (65, 53), (80, 59), (100, 71), (120, 79)],
}


def _step(size: float) -> int | None:
    if size <= 0 or size > _STEPS[-1]:
        return None
    for i, top in enumerate(_STEPS):
        if size <= top:
            return i
    return None


def it_um(grade: int, size: float) -> float | None:
    i = _step(size)
    if i is None or grade not in _IT:
        return None
    return float(_IT[grade][i])


def _shaft_ei_fine(letter: str, size: float) -> float | None:
    for top, ei in _SHAFT_EI_FINE[letter]:
        if size <= top:
            return float(ei)
    return None


def _shaft(letter: str, grade: int, size: float) -> tuple[float, float] | None:
    i = _step(size)
    it = it_um(grade, size)
    if i is None or it is None:
        return None
    if letter in _SHAFT_ES:
        es = float(_SHAFT_ES[letter][i])
        return es, es - it
    if letter == "js":
        return it / 2, -it / 2
    if letter == "k":
        ei = float(_SHAFT_EI["k"][i]) if 4 <= grade <= 7 and size > 3 else 0.0
        return ei + it, ei
    if letter in _SHAFT_EI:
        ei = float(_SHAFT_EI[letter][i])
        return ei + it, ei
    if letter in _SHAFT_EI_FINE:
        ei = _shaft_ei_fine(letter, size)
        return None if ei is None else (ei + it, ei)
    return None


def _hole(letter: str, grade: int, size: float) -> tuple[float, float] | None:
    it = it_um(grade, size)
    if it is None:
        return None
    lo = letter.lower()
    if lo in _SHAFT_ES:  # D–H: EI = -es
        sh = _shaft(lo, grade, size)
        if sh is None:
            return None
        ei = -sh[0]
        return ei + it, ei
    if lo == "js":
        return it / 2, -it / 2
    if lo in ("k", "m", "n", "p", "r", "s"):
        sh = _shaft(lo, grade, size)
        if sh is None:
            return None
        ei_shaft = sh[1]
        # Special rule (ISO 286-1 §): K–N up to IT8, P–ZC up to IT7 get +Δ
        limit = 8 if lo in ("k", "m", "n") else 7
        if grade <= limit and grade >= 2:
            prev = it_um(grade - 1, size) or 0.0
            es = -ei_shaft + (it - prev)
        else:
            es = -ei_shaft
        if lo == "k" and grade > 8:
            es = 0.0
        return es, es - it
    return None


def limits(letter: str, grade: int, size: float) -> tuple[float, float] | None:
    """(upper, lower) deviations in mm for a fit designation, or None if not tabulated."""
    if not letter or not (1 <= int(grade) <= 18):
        return None
    grade = int(grade)
    if letter.isupper() or letter == "JS":
        dev = _hole(letter, grade, size)
    else:
        dev = _shaft(letter.lower(), grade, size)
    if dev is None:
        return None
    up, low = dev
    return round(up / 1000.0, 6), round(low / 1000.0, 6)


def parse_fit(code: str) -> tuple[str, int] | None:
    """'H7' -> ('H', 7); 'js6' -> ('js', 6). Validates IT grade 1..18."""
    import re

    m = re.fullmatch(r"([A-Za-z]{1,2})(\d{1,2})", (code or "").strip())
    if not m:
        return None
    g = int(m.group(2))
    if not 1 <= g <= 18:
        return None
    return m.group(1), g
