from __future__ import annotations


def bp_for_floor(p001: float, target_floor: float) -> float:
    """Propose a linear BP mapping luminance p0.1 toward a *lifted* target floor.

    The floor is an iterative placement proposal, not a demand to blacken the
    background. The engine still validates the exact Siril result and backs BP
    off until it creates zero newly clipped RGB pixels.
    """
    p001=float(p001); target_floor=float(target_floor)
    if p001 <= target_floor:
        return 0.0
    bp=(p001-target_floor)/max(1.0-target_floor,1e-12)
    bp=min(bp,p001*0.985)
    return max(0.0,min(0.35,bp))


def backoff_factors() -> tuple[float,...]:
    return (1.0,0.95,0.90,0.85,0.80,0.75,0.70,0.65,0.60,0.55,0.50,0.45,0.40,0.35,0.30,0.25,0.20,0.15,0.10,0.075,0.05,0.025,0.0)


def command(bp: float) -> str:
    return f"linstretch -BP={float(bp):.8f} -clipmode=rgbblend"
