"""Trading-credit package restrictions.

Our account runs on a prop (trading-credit) package. Its detail lists `unavailableMarkets`, which
we must not trade in addition to data/not_allowed.txt. Only two read-only GETs are used:
/v1/prop/packages (catalogue) and /v1/prop/packages/{id} (detail).

Package selection: the one with loan == account.expected_equity_tmn in TMN (on 2026-10-22: id 183,
"هکاتون استارکوچ | والکس"). warning_limit / liquidity_limit are plain TMN amounts (UI shows
"900 تومان"), so liquidation is effectively unreachable; our own 8% hard stop is what matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from wallexer.config import ConfigError, Settings
from wallexer.exchange.client import WallexClient


@dataclass
class PropPackage:
    id: int
    title: str
    loan: Decimal
    currency: str
    status: str
    warning_limit: str
    liquidity_limit: str
    unavailable: frozenset[str] = field(default_factory=frozenset)


def _dec(v: object) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


async def fetch_package(client: WallexClient, settings: Settings) -> PropPackage:
    loan = settings.cfg.account.expected_equity_tmn
    catalogue = await client.prop_packages()
    matches = [
        p
        for p in catalogue
        if isinstance(p, dict) and _dec(p.get("loan")) == loan and p.get("currency") == "TMN"
    ]
    if len(matches) != 1:
        raise ConfigError(
            f"expected exactly one TMN trading-credit package with loan {loan:,}, found "
            f"{len(matches)} (ids: {[p.get('id') for p in matches]})"
        )
    pkg = matches[0]
    detail = await client.prop_package(int(pkg["id"]))
    raw = detail.get("unavailableMarkets") or []
    unavailable = frozenset(str(s).replace("/", "").upper() for s in raw if isinstance(s, str))
    return PropPackage(
        id=int(pkg["id"]),
        title=str(pkg.get("title", "")),
        loan=loan,
        currency=str(pkg.get("currency")),
        status=str(pkg.get("status")),
        warning_limit=str(pkg.get("warning_limit")),
        liquidity_limit=str(pkg.get("liquidity_limit")),
        unavailable=unavailable,
    )


def save_unavailable(path: Path, pkg: PropPackage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# unavailableMarkets of trading-credit package {pkg.id} (written by wallexer)"]
    lines += sorted(pkg.unavailable)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_unavailable(path: Path) -> frozenset[str]:
    """Last saved list (used when the API is unreachable, and by the risk gate)."""
    if not path.exists():
        return frozenset()
    return frozenset(
        ln.strip().upper()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    )
