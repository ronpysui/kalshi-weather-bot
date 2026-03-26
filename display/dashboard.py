"""
Rich terminal dashboard for live trading view and backtest results.
All strings use ASCII-safe characters for Windows cp1252 compatibility.
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box
from rich.rule import Rule

from predictor.probability import Bracket
from trader.edge import Signal
from trader.sizer import kelly_contracts, expected_value
import config

# Force UTF-8 output on Windows so box-drawing chars render correctly
console = Console(highlight=False)
ET = ZoneInfo("America/New_York")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct(v: float) -> str:
    return f"{v*100:.1f}%"

def _cents(v: float) -> str:
    return f"{v*100:.0f}c"

def _dollar(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.2f}"

def _edge_color(edge: float) -> str:
    if edge >= 0.15:  return "bright_green"
    if edge >= 0.07:  return "green"
    if edge >= 0.00:  return "yellow"
    return "red"

def _up(e: float) -> str:
    return "^" if e >= 0 else "v"


# ── Live trading dashboard ────────────────────────────────────────────────────

def render_live(
    brackets:   list[Bracket],
    signals:    list[Signal],
    forecast:   float,
    sigma:      float,
    bankroll:   float,
):
    console.clear()
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    dry = " [DRY RUN]" if config.DRY_RUN else " [LIVE]"
    console.print(Rule(f"[bold cyan]Kalshi NYC Temp Bot[/]  {now_et}{dry}"))

    # ── Forecast summary ──────────────────────────────────────────────────────
    forecast_panel = Panel(
        f"[bold]Forecast high:[/] [yellow]{forecast:.1f}F[/]   "
        f"[bold]sigma:[/] [cyan]{sigma:.1f}F[/]   "
        f"[bold]Bankroll:[/] [green]${bankroll:.2f}[/]",
        title="NWS / Model",
        border_style="cyan",
        expand=False,
    )
    console.print(forecast_panel)

    # ── Bracket probability table ─────────────────────────────────────────────
    tbl = Table(
        title="Bracket Probabilities vs Market",
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold white",
    )
    tbl.add_column("Bracket",        style="white",  width=18)
    tbl.add_column("Our Prob",       style="cyan",   justify="right")
    tbl.add_column("Mkt Yes Ask",    style="white",  justify="right")
    tbl.add_column("Mkt No Ask",     style="white",  justify="right")
    tbl.add_column("Yes Edge",       justify="right", width=12)
    tbl.add_column("No Edge",        justify="right", width=12)

    for b in brackets:
        yes_edge = b.our_prob - b.market_yes_price
        no_edge  = (1 - b.our_prob) - b.market_no_price

        def _fmt_edge(e):
            col = _edge_color(e)
            return Text(f"{_up(e)} {e*100:+.1f}c", style=col)

        tbl.add_row(
            b.label,
            _pct(b.our_prob),
            _cents(b.market_yes_price),
            _cents(b.market_no_price),
            _fmt_edge(yes_edge),
            _fmt_edge(no_edge),
        )

    console.print(tbl)

    # ── Signals / trade recommendations ──────────────────────────────────────
    if not signals:
        console.print(Panel(
            "[yellow]No actionable signals found "
            f"(min edge threshold: {config.MIN_EDGE*100:.0f}c)[/]",
            title="Signals",
            border_style="yellow",
        ))
    else:
        sig_tbl = Table(
            title=f"Actionable Signals  (edge > {config.MIN_EDGE*100:.0f}c)",
            box=box.SIMPLE_HEAD,
            header_style="bold white",
        )
        sig_tbl.add_column("Bracket",   width=18)
        sig_tbl.add_column("Side",      justify="center", width=6)
        sig_tbl.add_column("Our Prob",  justify="right")
        sig_tbl.add_column("Mkt Price", justify="right")
        sig_tbl.add_column("Edge",      justify="right")
        sig_tbl.add_column("EV/$",      justify="right")
        sig_tbl.add_column("Contracts", justify="right")
        sig_tbl.add_column("Risk",      justify="right")

        for s in signals:
            contracts = kelly_contracts(s, bankroll)
            ev        = expected_value(s)
            risk      = contracts * s.mkt_price
            side_col  = "green" if s.side == "yes" else "red"

            sig_tbl.add_row(
                s.label,
                Text(s.side.upper(), style=f"bold {side_col}"),
                _pct(s.our_prob),
                _cents(s.mkt_price),
                Text(f"{s.edge*100:+.1f}c", style=_edge_color(s.edge)),
                f"{ev*100:+.2f}c",
                str(contracts),
                f"${risk:.2f}",
            )

        console.print(sig_tbl)

    console.print(f"\n[dim]Next refresh in {config.POLL_INTERVAL_SECONDS//60} min  "
                  f"| Ctrl-C to quit[/]")


# ── Backtest dashboard ────────────────────────────────────────────────────────

def render_backtest(result) -> None:
    """Render the full 30-day backtest report."""
    console.print(Rule("[bold magenta]30-Day Backtest Results -- KXHIGHNY (NYC High Temp)[/]"))

    # ── Summary cards ─────────────────────────────────────────────────────────
    pnl_color    = "green"  if result.total_pnl >= 0  else "red"
    roi_color    = "green"  if result.roi >= 0         else "red"
    sharpe_color = "green"  if result.sharpe >= 1      else ("yellow" if result.sharpe >= 0 else "red")
    acc_color    = "green"  if result.accuracy >= 0.5  else "yellow"

    cards = [
        Panel(
            f"[{pnl_color}][bold]{_dollar(result.total_pnl)}[/][/]\n"
            f"[dim]avg {_dollar(result.avg_daily_pnl)}/day[/]",
            title="Total P&L", border_style=pnl_color, expand=True,
        ),
        Panel(
            f"[bold]{_pct(result.win_rate)}[/]\n"
            f"[dim]{sum(1 for d in result.days if d.pnl>0)}W / "
            f"{sum(1 for d in result.days if d.pnl<0)}L / "
            f"{sum(1 for d in result.days if d.pnl==0)}T[/]",
            title="Win Rate (days)", border_style="cyan", expand=True,
        ),
        Panel(
            f"[{roi_color}][bold]{_pct(result.roi)}[/][/]\n"
            f"[dim]${result.total_risked:.2f} risked[/]",
            title="ROI", border_style=roi_color, expand=True,
        ),
        Panel(
            f"[{sharpe_color}][bold]{result.sharpe:.2f}[/][/]\n"
            f"[dim]annualised[/]",
            title="Sharpe Ratio", border_style=sharpe_color, expand=True,
        ),
        Panel(
            f"[{acc_color}][bold]{_pct(result.accuracy)}[/][/]\n"
            f"[dim]Brier score: {result.avg_brier:.3f}[/]",
            title="Model Accuracy", border_style=acc_color, expand=True,
        ),
        Panel(
            f"[bold]{result.betting_days}[/] / {len(result.days)}\n"
            f"[dim]{result.no_bet_days} no-bet days[/]",
            title="Active Days", border_style="white", expand=True,
        ),
    ]
    console.print(Columns(cards))

    # ── Best / Worst day ──────────────────────────────────────────────────────
    console.print(
        Columns([
            Panel(
                f"[green]{result.best_day.date}[/]\n"
                f"Actual: [yellow]{result.best_day.actual_high:.0f}F[/]  "
                f"Forecast: {result.best_day.avg_forecast:.1f}F\n"
                f"P&L: [green bold]{_dollar(result.best_day.pnl)}[/]",
                title="Best Day", border_style="green",
            ),
            Panel(
                f"[red]{result.worst_day.date}[/]\n"
                f"Actual: [yellow]{result.worst_day.actual_high:.0f}F[/]  "
                f"Forecast: {result.worst_day.avg_forecast:.1f}F\n"
                f"P&L: [red bold]{_dollar(result.worst_day.pnl)}[/]",
                title="Worst Day", border_style="red",
            ),
        ])
    )

    # ── Per-day table ─────────────────────────────────────────────────────────
    tbl = Table(
        title="Day-by-Day Breakdown  (market baseline = seasonal climatology)",
        box=box.SIMPLE_HEAD,
        header_style="bold white",
        show_lines=False,
    )
    tbl.add_column("Date",            style="dim",     width=12)
    tbl.add_column("Actual",          justify="right", width=8)
    tbl.add_column("Sim Fcst",        justify="right", width=9)
    tbl.add_column("Seasonal",        justify="right", width=9)
    tbl.add_column("Correct Bracket", width=17)
    tbl.add_column("Mdl P(win)",      justify="right", width=10)
    tbl.add_column("Bets",            justify="right", width=5)
    tbl.add_column("W/L/NB",          justify="center",width=12)
    tbl.add_column("Day P&L",         justify="right", width=10)
    tbl.add_column("Cumul P&L",       justify="right", width=11)

    cumulative = 0.0
    for d in reversed(result.days):   # oldest -> newest
        cumulative += d.pnl
        pnl_col  = "green" if d.pnl > 0 else ("red" if d.pnl < 0 else "dim")
        cum_col  = "green" if cumulative > 0 else ("red" if cumulative < 0 else "dim")
        prob_col = "green" if d.model_prob_correct >= 0.35 else "yellow"

        tbl.add_row(
            str(d.date),
            f"{d.actual_high:.0f}F",
            f"{d.avg_forecast:.1f}F",
            f"{d.seasonal_center:.0f}F",
            d.correct_bracket or "--",
            Text(_pct(d.model_prob_correct), style=prob_col),
            str(d.bets_placed),
            f"{d.won}/{d.lost}/{d.no_bet}",
            Text(_dollar(d.pnl), style=pnl_col),
            Text(_dollar(cumulative), style=cum_col),
        )

    console.print(tbl)

    # ── Methodology note ──────────────────────────────────────────────────────
    console.print(Panel(
        "[dim][bold]What this backtest does:[/]\n"
        "1. Fetches real Kalshi settlement outcomes (actual highs) for each day.\n"
        "2. Simulates a forecast: actual_high + N(0, "
        f"{config.BACKTEST_FORECAST_SIGMA}F) — NOT a real historical NWS forecast.\n"
        "3. Uses a SYNTHETIC market price from seasonal climatology — NOT real Kalshi prices.\n"
        "4. Fixed sigma = SIGMA_MORNING ("
        f"{config.SIGMA_MORNING}F) on every day, same as the live morning lock.\n\n"
        "[bold]What it does NOT tell you:[/]\n"
        "- Real NWS forecasts from those mornings (unavailable retroactively).\n"
        "- Real Kalshi opening prices from those mornings (needs auth + candlestick API).\n\n"
        "Treat results as an upper bound on model quality, not an expected live return.[/]",
        title="[!] Methodology — Read Before Trading",
        border_style="yellow",
    ))
