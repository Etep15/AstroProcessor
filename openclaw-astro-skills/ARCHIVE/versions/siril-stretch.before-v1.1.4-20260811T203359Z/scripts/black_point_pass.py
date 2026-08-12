from __future__ import annotations


def bp_for_floor(p001: float, target_floor: float) -> float:
    """Propose a linear BP that maps luminance p0.1 toward a small positive floor.

    This is only a proposal. The engine must validate the actual Siril result and
    automatically back the BP away from the data if clipping/headroom is unsafe.
    """
    p001=float(p001); target_floor=float(target_floor)
    if p001 <= target_floor:
        return 0.0
    bp=(p001-target_floor)/max(1.0-target_floor,1e-12)
    bp=min(bp,p001*0.985)
    return max(0.0,min(0.35,bp))


def backoff_factors() -> tuple[float,...]:
    return (1.0,0.80,0.60,0.40,0.20,0.10,0.0)


def command(bp: float) -> str:
    return f"linstretch -BP={float(bp):.8f} -clipmode=rgbblend"
