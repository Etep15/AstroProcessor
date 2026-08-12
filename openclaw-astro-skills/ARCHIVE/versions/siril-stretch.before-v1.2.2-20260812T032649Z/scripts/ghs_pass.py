from __future__ import annotations
from dataclasses import dataclass,asdict

_VALID_MODES={"even":"-even","human":"-human","independent":"-independent"}

@dataclass(frozen=True)
class GHSParameters:
    D: float
    B: float
    SP: float
    LP: float
    HP: float
    color_mode: str = "even"

    def command(self) -> str:
        mode=_VALID_MODES.get(self.color_mode)
        if mode is None:
            raise ValueError(f"Unsupported GHS color mode: {self.color_mode}")
        return (
            f"ght -D={self.D:.6f} -B={self.B:.6f} "
            f"-SP={self.SP:.6f} -LP={self.LP:.6f} -HP={self.HP:.6f} "
            f"-clipmode=rgbblend {mode}"
        )

    def as_dict(self):
        return asdict(self)


def bounded(D: float, B: float, SP: float, LP: float, HP: float, color_mode: str="even") -> GHSParameters:
    if color_mode not in _VALID_MODES:
        raise ValueError(f"Unsupported GHS color mode: {color_mode}")
    return GHSParameters(
        D=max(0.05,min(9.5,float(D))),
        B=max(0.0,min(15.0,float(B))),
        SP=max(0.00001,min(0.80,float(SP))),
        LP=max(0.0,min(0.75,float(LP))),
        HP=max(0.20,min(1.0,float(HP))),
        color_mode=color_mode,
    )
