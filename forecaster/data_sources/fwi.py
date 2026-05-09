"""Canadian Fire Weather Index (FWI) system — Van Wagner (1987).

Computes FWI from a single observation of temperature, relative humidity,
wind speed, and 24h precipitation. Uses climatological starting values for
the moisture codes (FFMC=85, DMC=6, DC=15) since we don't persist state.
"""

import math


def calculate_fwi(
    temp_c: float,
    rh_pct: float,
    wind_kmh: float,
    precip_mm: float,
    prev_ffmc: float = 85.0,
    prev_dmc: float = 6.0,
    prev_dc: float = 15.0,
) -> float:
    """Return FWI index (0=low risk, ~20=high, 50+=extreme)."""
    T, H, W, ro = temp_c, rh_pct, wind_kmh, precip_mm
    H = max(1.0, min(H, 99.0))

    # --- FFMC (Fine Fuel Moisture Code) ---
    mo = 147.2 * (101 - prev_ffmc) / (59.5 + prev_ffmc)
    if ro > 0.5:
        rf = ro - 0.5
        if mo <= 150:
            mr = mo + 42.5 * rf * math.exp(-100 / (251 - mo)) * (1 - math.exp(-6.93 / rf))
        else:
            mr = (mo + 42.5 * rf * math.exp(-100 / (251 - mo)) * (1 - math.exp(-6.93 / rf))
                  + 0.0015 * (mo - 150) ** 2 * rf ** 0.5)
        mo = min(mr, 250.0)

    Ed = 0.942 * H ** 0.679 + 11 * math.exp((H - 100) / 10) + 0.18 * (21.1 - T) * (1 - math.exp(-0.115 * H))
    Ew = 0.618 * H ** 0.753 + 10 * math.exp((H - 100) / 10) + 0.18 * (21.1 - T) * (1 - math.exp(-0.115 * H))

    if mo > Ed:
        ko = 0.424 * (1 - (H / 100) ** 1.7) + 0.0694 * W ** 0.5 * (1 - (H / 100) ** 8)
        kd = ko * 0.581 * math.exp(0.0365 * T)
        m = Ed + (mo - Ed) * 10 ** (-kd)
    elif mo < Ew:
        k1 = 0.424 * (1 - ((100 - H) / 100) ** 1.7) + 0.0694 * W ** 0.5 * (1 - ((100 - H) / 100) ** 8)
        kw = k1 * 0.581 * math.exp(0.0365 * T)
        m = Ew - (Ew - mo) * 10 ** (-kw)
    else:
        m = mo

    ffmc = 59.5 * (250 - m) / (147.2 + m)

    # --- ISI (Initial Spread Index) ---
    fm = 147.2 * (101 - ffmc) / (59.5 + ffmc)
    sf = 19.115 * math.exp(-0.1386 * fm) * (1 + fm ** 5.31 / 4.93e7)
    isi = sf * math.exp(0.05039 * W)

    # --- DMC (Duff Moisture Code) ---
    dmc = prev_dmc
    if ro > 1.5:
        re = 0.92 * ro - 1.27
        Mo = 20 + math.exp(5.6348 - prev_dmc / 43.43)
        if prev_dmc <= 33:
            b = 100 / (0.5 + 0.3 * prev_dmc)
        elif prev_dmc <= 65:
            b = 14 - 1.3 * math.log(prev_dmc)
        else:
            b = 6.2 * math.log(prev_dmc) - 17.2
        Mr = Mo + 1000 * re / (48.77 + b * re)
        Pr = 244.72 - 43.43 * math.log(max(Mr - 20, 1e-6))
        dmc = max(Pr, 0.0)
    if T > -1.1:
        Le = 9.0  # day-length factor for SoCal (approx year-round)
        dmc += 100 * 1.894 * (T + 1.1) * (100 - H) * Le * 1e-6

    # --- DC (Drought Code) ---
    dc = prev_dc
    if ro > 2.8:
        rd = 0.83 * ro - 1.27
        Qo = 800 * math.exp(-prev_dc / 400)
        Qr = Qo + 3.937 * rd
        dc = 400 * math.log(800 / max(Qr, 1e-6))
        dc = max(dc, 0.0)
    if T > -2.8:
        Lf = 1.6  # SoCal approximate
        dc += 0.36 * (T + 2.8) + Lf

    # --- BUI (Buildup Index) ---
    if dmc <= 0.4 * dc:
        bui = 0.8 * dmc * dc / (dmc + 0.4 * dc) if (dmc + 0.4 * dc) > 0 else 0.0
    else:
        bui = dmc - (1 - 0.8 * dc / (dmc + 0.4 * dc)) * (0.92 + (0.0114 * dmc) ** 1.7)
    bui = max(bui, 0.0)

    # --- FWI (Fire Weather Index) ---
    if bui <= 80:
        fd = 0.626 * bui ** 0.809 + 2
    else:
        fd = 1000 / (25 + 108.64 * math.exp(-0.023 * bui))

    B = 0.1 * isi * fd
    if B > 1:
        fwi = math.exp(2.72 * (0.434 * math.log(B)) ** 0.647)
    else:
        fwi = B

    return round(fwi, 2)
