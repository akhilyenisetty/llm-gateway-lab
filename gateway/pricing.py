"""Price table loading and per-call cost calculation.

Cost = sum over token categories of (tokens / 1,000,000) * price per MTok, then times
any data-residency multiplier. All arithmetic uses Decimal so stored costs are exact.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

MTOK = Decimal(1_000_000)
PRICE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


@dataclass(frozen=True)
class Usage:
    """Token counts from a Messages API response's `usage` object."""

    input_tokens: int = 0  # uncached input only
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Total prompt size: uncached input plus everything read from or written to cache."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )

    @classmethod
    def from_api(cls, usage: dict[str, Any]) -> "Usage":
        breakdown = usage.get("cache_creation") or {}
        write_total = usage.get("cache_creation_input_tokens") or 0
        write_5m = breakdown.get("ephemeral_5m_input_tokens") or 0
        write_1h = breakdown.get("ephemeral_1h_input_tokens") or 0
        # The total is authoritative. Writes it has that the TTL breakdown doesn't (no
        # breakdown sent, or a streamed total updated without one) are counted at the
        # 5-minute rate, the API's default cache TTL.
        write_5m += max(0, write_total - write_5m - write_1h)
        return cls(
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
        )


@dataclass(frozen=True)
class Rates:
    """USD per million tokens for each token category."""

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal


@dataclass(frozen=True)
class Tier:
    rates: Rates
    max_prompt_tokens: int | None  # None = no upper limit


@dataclass(frozen=True)
class CostResult:
    cost_usd: Decimal | None
    note: str | None = None  # why cost is missing or adjusted


class PriceTable:
    def __init__(
        self,
        models: dict[str, list[Tier]],
        geo_multipliers: dict[str, Decimal],
        checked_on: str,
        context_windows: dict[str, int] | None = None,
    ) -> None:
        self._models = models
        self._geo_multipliers = geo_multipliers
        self._context_windows = context_windows or {}
        self.checked_on = checked_on

    @classmethod
    def load(cls, path: Path) -> "PriceTable":
        data = yaml.safe_load(path.read_text())
        models: dict[str, list[Tier]] = {}
        windows: dict[str, int] = {}
        for model_id, entry in data["models"].items():
            raw_tiers = entry.get("tiers") or [entry]
            tiers = [
                Tier(
                    rates=Rates(**{f: Decimal(str(t[f])) for f in PRICE_FIELDS}),
                    max_prompt_tokens=t.get("max_prompt_tokens"),
                )
                for t in raw_tiers
            ]
            limits = [t.max_prompt_tokens for t in tiers]
            if None in limits[:-1] or limits[-1] is not None:
                raise ValueError(f"{model_id}: only the last tier must omit max_prompt_tokens")
            for name in [model_id, *entry.get("aliases", [])]:
                models[name] = tiers
                if "context_window" in entry:
                    windows[name] = int(entry["context_window"])
        geo = {k: Decimal(str(v)) for k, v in (data.get("inference_geo_multipliers") or {}).items()}
        return cls(models, geo, str(data.get("checked_on", "")), windows)

    def context_window(self, model: str | None) -> int | None:
        return self._context_windows.get(model or "")

    def cost(
        self,
        model: str | None,
        usage: Usage,
        *,
        inference_geo: str | None = None,
        speed: str | None = None,
    ) -> CostResult:
        if speed == "fast":
            return CostResult(None, "fast mode pricing not modeled")
        tiers = self._models.get(model or "")
        if tiers is None:
            return CostResult(None, f"no price entry for model {model!r}")

        rates = next(
            t.rates
            for t in tiers
            if t.max_prompt_tokens is None or usage.prompt_tokens <= t.max_prompt_tokens
        )
        total = (
            usage.input_tokens * rates.input
            + usage.output_tokens * rates.output
            + usage.cache_read_tokens * rates.cache_read
            + usage.cache_write_5m_tokens * rates.cache_write_5m
            + usage.cache_write_1h_tokens * rates.cache_write_1h
        ) / MTOK

        note = None
        if inference_geo and inference_geo in self._geo_multipliers:
            multiplier = self._geo_multipliers[inference_geo]
            total *= multiplier
            note = f"inference_geo={inference_geo} x{multiplier}"
        return CostResult(total, note)
