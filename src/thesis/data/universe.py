"""The screening universe: a static, hand-maintained S&P 500 snapshot.

Index membership changes a few times a quarter, and there is no free, stable API
for it. Rather than scrape at runtime (fragile, and it would make every screen
non-reproducible), the list is a dated snapshot checked into the repo.

It is *approximately* the index — 494 symbols, not an authoritative constituent
feed — which is fine for generating research candidates and would not be fine
for index-tracking or backtesting.

Consequences, all of them handled rather than hidden:

* A name that has since been delisted or acquired returns no price history and
  is excluded with the reason "no price history" — visible in the screen's
  exclusion summary, not silently dropped.
* A name added to the index after `AS_OF` is simply not screened. Refresh the
  snapshot, or pass your own list with `--universe path/to/tickers.txt`.

Symbols use the Yahoo Finance convention (BRK-B, not BRK.B).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

#: How old the snapshot may get before the screen starts complaining. Index
#: changes and ticker renames accumulate; ninety days is roughly a quarter's
#: worth of them, and BK/MMC-style breakage should be caught by this warning
#: rather than by a wall of 404s.
STALE_AFTER_DAYS = 90

#: The snapshot date of the list below. Screens are reproducible against it.
AS_OF = "2026-07-01"

#: Ticker changes, verified 2026-07-28 by fetching both symbols: the old one
#: returns a 404 "Quote not found", the new one returns current bars. These are
#: applied by `load()`, so a hand-written watchlist using the old symbol works.
#:
#: This is the real reason a screen reports healthy companies as delisted. It is
#: not provider flakiness and no amount of retrying fixes it — the universe list
#: is simply out of date.
RENAMED: dict[str, str] = {
    "BK": "BNY",      # Bank of New York Mellon, trading as BNY after the rebrand
    "MMC": "MRSH",    # Marsh McLennan
    "FI": "FISV",     # Fiserv
    "PARA": "PSKY",   # Paramount Skydance, after the merger
}

#: Symbols dropped from the snapshot on 2026-07-28 because no data comes back
#: under them or any alternate symbol tried — consistent with a completed
#: acquisition or take-private. Kept here rather than deleted silently so the
#: change is auditable and a future refresh can re-check them.
RETIRED: dict[str, str] = {
    "CTRA": "no bars under any symbol tried (checked 2026-07-28)",
    "DAY": "no bars under any symbol tried (checked 2026-07-28)",
    "DFS": "no bars under any symbol tried (checked 2026-07-28)",
    "HES": "no bars under any symbol tried (checked 2026-07-28)",
    "HOLX": "no bars under any symbol tried (checked 2026-07-28)",
    "IPG": "no bars under any symbol tried (checked 2026-07-28)",
    "K": "no bars under any symbol tried (checked 2026-07-28)",
    "WBA": "no bars under any symbol tried (checked 2026-07-28)",
}

SP500: tuple[str, ...] = (
    "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI", "ADM",
    "ADP", "ADSK", "AEE", "AEP", "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM",
    "ALB", "ALGN", "ALL", "ALLE", "AMAT", "AMCR", "AMD", "AME", "AMGN", "AMP",
    "AMT", "AMZN", "ANET", "AON", "AOS", "APA", "APD", "APH", "APTV", "ARE",
    "ATO", "AVB", "AVGO", "AVY", "AWK", "AXON", "AXP", "AZO", "BA", "BAC",
    "BALL", "BAX", "BBY", "BDX", "BEN", "BF-B", "BG", "BIIB", "BK", "BKNG",
    "BKR", "BLDR", "BLK", "BMY", "BR", "BRK-B", "BRO", "BSX", "BX", "BXP",
    "C", "CAG", "CAH", "CARR", "CAT", "CB", "CBOE", "CBRE", "CCI", "CCL",
    "CDNS", "CDW", "CE", "CEG", "CF", "CFG", "CHD", "CHRW", "CHTR", "CI",
    "CINF", "CL", "CLX", "CMCSA", "CME", "CMG", "CMI", "CMS", "CNC", "CNP",
    "COF", "COO", "COP", "COR", "COST", "CPAY", "CPB", "CPRT", "CPT", "CRL",
    "CRM", "CRWD", "CSCO", "CSGP", "CSX", "CTAS", "CTRA", "CTSH", "CTVA", "CVS",
    "CVX", "CZR", "D", "DAL", "DAY", "DD", "DE", "DECK", "DELL", "DFS",
    "DG", "DGX", "DHI", "DHR", "DIS", "DLR", "DLTR", "DOC", "DOV", "DOW",
    "DPZ", "DRI", "DTE", "DUK", "DVA", "DVN", "DXCM", "EA", "EBAY", "ECL",
    "ED", "EFX", "EG", "EIX", "EL", "ELV", "EMN", "EMR", "ENPH", "EOG",
    "EPAM", "EQIX", "EQR", "EQT", "ES", "ESS", "ETN", "ETR", "EVRG", "EW",
    "EXC", "EXPD", "EXPE", "EXR", "F", "FANG", "FAST", "FCX", "FDS", "FDX",
    "FE", "FFIV", "FI", "FICO", "FIS", "FITB", "FOX", "FOXA", "FRT", "FSLR",
    "FTNT", "FTV", "GD", "GDDY", "GE", "GEHC", "GEN", "GEV", "GILD", "GIS",
    "GL", "GLW", "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN", "GRMN", "GS",
    "GWW", "HAL", "HAS", "HBAN", "HCA", "HD", "HES", "HIG", "HII", "HLT",
    "HOLX", "HON", "HPE", "HPQ", "HRL", "HSIC", "HST", "HSY", "HUBB", "HUM",
    "HWM", "IBM", "ICE", "IDXX", "IEX", "IFF", "INCY", "INTC", "INTU", "INVH",
    "IP", "IPG", "IQV", "IR", "IRM", "ISRG", "IT", "ITW", "IVZ", "J",
    "JBHT", "JBL", "JCI", "JKHY", "JNJ", "JPM", "K", "KDP", "KEY", "KEYS",
    "KHC", "KIM", "KKR", "KLAC", "KMB", "KMI", "KMX", "KO", "KR", "KVUE",
    "L", "LDOS", "LEN", "LH", "LHX", "LIN", "LKQ", "LLY", "LMT", "LNT",
    "LOW", "LRCX", "LULU", "LUV", "LVS", "LW", "LYB", "LYV", "MA", "MAA",
    "MAR", "MAS", "MCD", "MCHP", "MCK", "MCO", "MDLZ", "MDT", "MET", "META",
    "MGM", "MHK", "MKC", "MKTX", "MLM", "MMC", "MMM", "MNST", "MO", "MOH",
    "MOS", "MPC", "MPWR", "MRK", "MRNA", "MS", "MSCI", "MSFT", "MSI", "MTB",
    "MTCH", "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE", "NEM", "NFLX", "NI",
    "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS", "NUE", "NVDA", "NVR",
    "NWS", "NWSA", "NXPI", "O", "ODFL", "OKE", "OMC", "ON", "ORCL", "ORLY",
    "OTIS", "OXY", "PANW", "PARA", "PAYC", "PAYX", "PCAR", "PCG", "PEG", "PEP",
    "PFE", "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD", "PLTR", "PM",
    "PNC", "PNR", "PNW", "PODD", "POOL", "PPG", "PPL", "PRU", "PSA", "PSX",
    "PTC", "PWR", "PYPL", "QCOM", "RCL", "REG", "REGN", "RF", "RJF", "RL",
    "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "RVTY", "SBAC", "SBUX",
    "SCHW", "SHW", "SJM", "SLB", "SMCI", "SNA", "SNPS", "SO", "SOLV", "SPG",
    "SPGI", "SRE", "STE", "STLD", "STT", "STX", "STZ", "SW", "SWK", "SWKS",
    "SYF", "SYK", "SYY", "T", "TAP", "TDG", "TDY", "TECH", "TEL", "TER",
    "TFC", "TGT", "TJX", "TMO", "TMUS", "TPR", "TRGP", "TRMB", "TROW", "TRV",
    "TSCO", "TSLA", "TSN", "TT", "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER",
    "UDR", "UHS", "ULTA", "UNH", "UNP", "UPS", "URI", "USB", "V", "VICI",
    "VLO", "VLTO", "VMC", "VRSK", "VRSN", "VRTX", "VST", "VTR", "VTRS", "VZ",
    "WAB", "WAT", "WBA", "WBD", "WDC", "WEC", "WELL", "WFC", "WM", "WMB",
    "WMT", "WRB", "WSM", "WST", "WTW", "WY", "WYNN", "XEL", "XOM", "XYL",
    "YUM", "ZBH", "ZBRA", "ZTS",
)


@dataclass(frozen=True)
class Snapshot:
    """Where the screening universe came from and how old it is."""

    label: str
    as_of: str | None
    age_days: int | None
    size: int

    @property
    def stale(self) -> bool:
        return self.age_days is not None and self.age_days > STALE_AFTER_DAYS

    def describe(self) -> str:
        """One line of provenance, stamped on every screen."""
        if self.as_of is None:
            return f"{self.label} — {self.size} names, no snapshot date"
        return (
            f"{self.label} — {self.size} names, snapshot {self.as_of} "
            f"({self.age_days} days old)"
        )

    def warning(self) -> str | None:
        """The staleness warning, or None while the snapshot is current."""
        if not self.stale:
            return None
        return (
            f"Universe snapshot is {self.age_days} days old (limit {STALE_AFTER_DAYS}). "
            "Index changes and ticker renames since then are invisible to this screen — "
            "run `thesis universe --check` and refresh the snapshot."
        )


def describe(
    path: str | Path | None = None,
    tickers: Iterable[str] | None = None,
    today: date | None = None,
) -> Snapshot:
    """Provenance for the universe actually being screened. Pure given `today`."""
    names = tuple(tickers) if tickers is not None else load(path)
    if path is not None:
        return Snapshot(label=f"custom list {Path(path).name}", as_of=None,
                        age_days=None, size=len(names))
    age = ((today or date.today()) - date.fromisoformat(AS_OF)).days
    return Snapshot(label="S&P 500 snapshot", as_of=AS_OF, age_days=age, size=len(names))


def resolve(tickers: Iterable[str]) -> tuple[str, ...]:
    """Apply known ticker changes and drop retired symbols, preserving order. Pure."""
    out: list[str] = []
    for ticker in tickers:
        symbol = RENAMED.get(ticker, ticker)
        if symbol in RETIRED:
            continue
        out.append(symbol)
    return tuple(dict.fromkeys(out))


def load(path: str | Path | None = None) -> tuple[str, ...]:
    """The screening universe: the snapshot, or one ticker per line from `path`.

    Blank lines and `#` comments are ignored, so a hand-maintained watchlist
    works as a drop-in universe. Read as utf-8-sig: Notepad and PowerShell both
    write a BOM, and a leading BOM would otherwise turn AAPL into a bad symbol.

    Known ticker changes are applied to whatever list comes back, so an old
    watchlist keeps working after a rebrand.
    """
    if path is None:
        return resolve(SP500)
    text = Path(path).read_text(encoding="utf-8-sig")
    tickers = []
    for line in text.splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol:
            tickers.append(symbol)
    if not tickers:
        raise ValueError(f"no tickers found in {path}")
    return resolve(tickers)
