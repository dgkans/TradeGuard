"""Policy YAML loading and validation."""

from pathlib import Path

import pytest

from services.gateway.policy.model import PolicyError, load_policy

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    return path


def test_default_and_strict_policies_load() -> None:
    default = load_policy(REPO_ROOT / "policies/default.yaml")
    assert default.name == "default"
    assert default.max_order_notional_usd == 2000
    assert default.require_approval.notional_over_usd == 1000
    assert "get_clock" in default.tool_allowlist.read
    assert "advance_clock" not in default.tool_allowlist.read + default.tool_allowlist.write
    assert default.operator_tools == ["advance_clock"]
    strict = load_policy(REPO_ROOT / "policies/strict.yaml")
    assert strict.allowed_order_types == ["limit"]


def test_symbols_are_normalized_to_upper_case(tmp_path: Path) -> None:
    text = (REPO_ROOT / "policies/default.yaml").read_text().replace("[NVDA,", "[nvda,")
    assert "NVDA" in load_policy(write(tmp_path, text)).allowed_symbols


def test_error_names_the_nested_field(tmp_path: Path) -> None:
    text = (
        (REPO_ROOT / "policies/default.yaml")
        .read_text()
        .replace("notional_over_usd: 1000", "notional_over_usd: lots")
    )
    with pytest.raises(PolicyError, match=r"require_approval\.notional_over_usd"):
        load_policy(write(tmp_path, text))


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    text = (REPO_ROOT / "policies/default.yaml").read_text() + "max_order_notinal_usd: 5\n"
    with pytest.raises(PolicyError, match="max_order_notinal_usd"):
        load_policy(write(tmp_path, text))


def test_negative_limits_are_rejected(tmp_path: Path) -> None:
    text = (
        (REPO_ROOT / "policies/default.yaml")
        .read_text()
        .replace("max_order_notional_usd: 2000", "max_order_notional_usd: -1")
    )
    with pytest.raises(PolicyError, match="max_order_notional_usd"):
        load_policy(write(tmp_path, text))


def test_exposure_pct_must_be_a_percentage(tmp_path: Path) -> None:
    text = (
        (REPO_ROOT / "policies/default.yaml")
        .read_text()
        .replace("max_portfolio_exposure_pct: 60", "max_portfolio_exposure_pct: 160")
    )
    with pytest.raises(PolicyError, match="max_portfolio_exposure_pct"):
        load_policy(write(tmp_path, text))


def test_tool_in_both_read_and_write_is_rejected(tmp_path: Path) -> None:
    text = (
        (REPO_ROOT / "policies/default.yaml")
        .read_text()
        .replace(
            "write: [place_order, cancel_order]", "write: [place_order, cancel_order, get_quote]"
        )
    )
    with pytest.raises(PolicyError, match="get_quote"):
        load_policy(write(tmp_path, text))


def test_missing_file_and_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "nope.yaml")
    with pytest.raises(PolicyError, match="YAML"):
        load_policy(write(tmp_path, "name: [unclosed"))
