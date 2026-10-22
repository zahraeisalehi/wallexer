"""Trading-credit account checks.

We trade only with the trading-credit (sub) account, which starts with ~5,000,000 TMN.
Shadow and trade refuse to start unless the key + sub-account id are set and the account's
equity is close to that. Equity (TMN + coins marked at best bid) is used rather than TMN alone,
so a restart while holding positions can still start and trade back to TMN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from wallexer.config import ConfigError, Mode, Settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.models import Balance, Book

TMN = "TMN"


class PreflightError(Exception):
    """Shadow/trade must not start. Message is shown to the user as-is."""


@dataclass
class CreditCheck:
    ok: bool
    tmn: Decimal
    equity_tmn: Decimal
    expected_tmn: Decimal
    tolerance_pct: Decimal
    assets_tmn: dict[str, Decimal] = field(default_factory=dict)  # non-TMN holdings valued in TMN
    unpriced: list[str] = field(default_factory=list)  # held assets we could not value
    locked: dict[str, Decimal] = field(default_factory=dict)
    message: str = ""


def _bid(books: dict[str, Book], symbol: str) -> Decimal | None:
    b = books.get(symbol)
    return b.best_bid if b else None


async def _books_for(client: WallexClient, assets: list[str]) -> dict[str, Book]:
    symbols = {"USDTTMN"}
    for a in assets:
        symbols.update({f"{a}TMN", f"{a}USDT"})
    books: dict[str, Book] = {}
    for s in sorted(symbols):
        try:
            books[s] = await client.depth(s)
        except (ApiError, TransportError):
            continue  # market may not exist for this pair
    return books


def value_balances(
    balances: dict[str, Balance], books: dict[str, Book]
) -> tuple[Decimal, dict[str, Decimal], list[str]]:
    """Return (TMN amount, {asset: value in TMN}, unpriced assets). Uses best bids (liquidation value)."""
    tmn = balances[TMN].value if TMN in balances else Decimal(0)
    usdt_bid = _bid(books, "USDTTMN")
    valued: dict[str, Decimal] = {}
    unpriced: list[str] = []
    for asset, bal in balances.items():
        if asset == TMN or bal.value <= 0:
            continue
        bid = _bid(books, f"{asset}TMN")
        if bid is None and asset != "USDT" and usdt_bid:
            ub = _bid(books, f"{asset}USDT")
            bid = ub * usdt_bid if ub else None
        if asset == "USDT":
            bid = usdt_bid
        if bid is None:
            unpriced.append(asset)
        else:
            valued[asset] = bal.value * bid
    return tmn, valued, unpriced


async def check_trading_credit(client: WallexClient, settings: Settings) -> CreditCheck:
    acfg = settings.cfg.account
    balances = await client.balances()  # always sent with the sub-account header
    held = [a for a, b in balances.items() if a != TMN and b.value > 0]
    books = await _books_for(client, held) if held else {}
    tmn, valued, unpriced = value_balances(balances, books)
    equity = tmn + sum(valued.values(), Decimal(0))
    lo = acfg.expected_equity_tmn * (1 - acfg.equity_tolerance_pct / 100)
    hi = acfg.expected_equity_tmn * (1 + acfg.equity_tolerance_pct / 100)
    ok = lo <= equity <= hi
    msg = (
        f"equity {equity:,.0f} TMN (TMN {tmn:,.0f}) "
        f"{'within' if ok else 'OUTSIDE'} {lo:,.0f}–{hi:,.0f}"
    )
    if unpriced:
        msg += f"; unpriced: {', '.join(unpriced)}"
    locked = {a: b.locked for a, b in balances.items() if b.locked > 0}
    return CreditCheck(
        ok,
        tmn,
        equity,
        acfg.expected_equity_tmn,
        acfg.equity_tolerance_pct,
        valued,
        unpriced,
        locked,
        msg,
    )


async def preflight_live(client: WallexClient, settings: Settings, mode: Mode) -> CreditCheck:
    """Gate for starting shadow/trade. The engine must call this before its first tick."""
    try:
        settings.require_private()
    except ConfigError as e:
        raise PreflightError(f"Cannot start {mode}: {e}") from e
    try:
        check = await check_trading_credit(client, settings)
    except (ApiError, TransportError) as e:
        raise PreflightError(f"Cannot start {mode}: balance check failed: {e}") from e
    if not check.ok:
        raise PreflightError(
            f"Cannot start {mode}: trading-credit account check failed: {check.message}. "
            "Is WALLEX_API_KEY the trading-credit account's key and WALLEX_SUBACCOUNT_ID correct?"
        )
    return check
