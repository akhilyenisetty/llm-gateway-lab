from decimal import Decimal

import pytest

from gateway.pricing import PriceTable, Usage
from gateway.settings import PROJECT_ROOT


@pytest.fixture(scope="module")
def prices() -> PriceTable:
    return PriceTable.load(PROJECT_ROOT / "config" / "prices.yaml")


def test_all_token_categories_are_priced(prices: PriceTable) -> None:
    # Sonnet 5.5: input $2, output $10, cache read $0.10, 5m write $2.50, 1h write $4 per MTok.
    usage = Usage(
        input_tokens=1_000,
        output_tokens=500,
        cache_read_tokens=2_000,
        cache_write_5m_tokens=4_000,
        cache_write_1h_tokens=1_000,
    )
    # 1000*2 + 500*10 + 2000*0.10 + 4000*2.50 + 1000*4 = 21,200 -> $0.0212
    assert prices.cost("claude-sonnet-5-5", usage).cost_usd == Decimal("0.0212")


def test_alias_resolves_to_dated_snapshot(prices: PriceTable) -> None:
    usage = Usage(input_tokens=1_000_000)
    alias = prices.cost("claude-haiku-4-5", usage).cost_usd
    dated = prices.cost("claude-haiku-4-5-20251001", usage).cost_usd
    assert alias == dated == Decimal("1")


def test_tiered_model_uses_low_tier_up_to_threshold(prices: PriceTable) -> None:
    # Haiku 5.5 at <= 100k prompt tokens: input $0.10, output $0.50.
    usage = Usage(input_tokens=100_000, output_tokens=1_000)
    assert prices.cost("claude-haiku-5-5", usage).cost_usd == Decimal("0.0105")


def test_tiered_model_uses_high_tier_above_threshold(prices: PriceTable) -> None:
    # Haiku 5.5 above 100k prompt tokens: input $0.50, output $2.50.
    usage = Usage(input_tokens=100_001, output_tokens=1_000)
    # 100001*0.50 + 1000*2.50 = 52,500.5 -> $0.0525005
    assert prices.cost("claude-haiku-5-5", usage).cost_usd == Decimal("0.0525005")


def test_tier_threshold_counts_cached_tokens(prices: PriceTable) -> None:
    # Only 10 uncached input tokens, but the prompt is 100,010 tokens with the cache read.
    usage = Usage(input_tokens=10, cache_read_tokens=100_000)
    # High tier: 10*0.50 + 100000*0.05 = 5,005 -> $0.005005
    assert prices.cost("claude-haiku-5-5", usage).cost_usd == Decimal("0.005005")


def test_us_inference_geo_multiplier(prices: PriceTable) -> None:
    usage = Usage(input_tokens=1_000_000)
    result = prices.cost("claude-sonnet-5-5", usage, inference_geo="us")
    assert result.cost_usd == Decimal("2.2")
    assert result.note == "inference_geo=us x1.1"


def test_global_inference_geo_is_standard_price(prices: PriceTable) -> None:
    usage = Usage(input_tokens=1_000_000)
    assert prices.cost("claude-sonnet-5-5", usage, inference_geo="global").cost_usd == 2


def test_unknown_model_has_no_cost(prices: PriceTable) -> None:
    result = prices.cost("claude-imaginary-9", Usage(input_tokens=10))
    assert result.cost_usd is None
    assert "no price entry" in (result.note or "")


def test_fast_mode_is_not_guessed(prices: PriceTable) -> None:
    result = prices.cost("claude-opus-5-5", Usage(input_tokens=10), speed="fast")
    assert result.cost_usd is None
    assert result.note == "fast mode pricing not modeled"


def test_usage_from_api_with_ttl_breakdown() -> None:
    usage = Usage.from_api(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 70,
            "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 30},
        }
    )
    assert usage == Usage(10, 20, 30, 40, 30)
    assert usage.prompt_tokens == 110


def test_usage_from_api_without_breakdown_counts_writes_as_5m() -> None:
    usage = Usage.from_api(
        {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 50}
    )
    assert usage.cache_write_5m_tokens == 50
    assert usage.cache_write_1h_tokens == 0


def test_usage_from_api_handles_nulls() -> None:
    usage = Usage.from_api(
        {
            "input_tokens": 5,
            "output_tokens": 6,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_creation": None,
        }
    )
    assert usage == Usage(5, 6, 0, 0, 0)


def test_usage_from_api_total_beyond_breakdown_counts_extra_as_5m() -> None:
    # A streamed message_delta can update the total without resending the TTL breakdown.
    usage = Usage.from_api(
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30},
        }
    )
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (70, 30)


def test_context_windows(prices: PriceTable) -> None:
    assert prices.context_window("claude-sonnet-5-5") == 1_000_000
    assert prices.context_window("claude-haiku-4-5") == 200_000  # alias
    assert prices.context_window("claude-haiku-4-5-20251001") == 200_000
    assert prices.context_window("claude-unknown") is None
