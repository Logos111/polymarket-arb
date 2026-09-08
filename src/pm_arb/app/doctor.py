"""环境自检：检查配置加载状态与必备凭证。

用法::

    uv run pm-doctor
"""

from __future__ import annotations

from pm_arb import __version__
from pm_arb.infra.config import get_settings
from pm_arb.infra.logging import setup_logging


def main() -> int:
    setup_logging()
    s = get_settings()

    print(f"pm-arb v{__version__} — environment doctor")
    print("=" * 68)

    def secret_status(value: str) -> tuple[str, bool]:
        return ("******** (set)", True) if value.strip() else ("(unset)", False)

    rows: list[tuple[str, str, bool]] = [
        ("Gamma API endpoint", s.gamma_api_url, True),
        ("CLOB API endpoint", s.clob_api_url, True),
        ("CLOB WS (market)", s.clob_ws_market_url, True),
        ("CLOB WS (user)", s.clob_ws_user_url, True),
        ("Polygon RPC", s.polygon_rpc_url or "(default public node)", bool(s.polygon_rpc_url)),
        ("Chain id", str(s.chain_id), True),
        ("Signature type", str(s.signature_type), True),
    ]
    for name, value, ok in rows:
        print(f"[{'OK ' if ok else 'MISS'}] {name:<22}{value}")

    pk, pk_ok = secret_status(s.private_key)
    k, k_ok = secret_status(s.clob_api_key)
    sec, sec_ok = secret_status(s.clob_api_secret)
    pp, pp_ok = secret_status(s.clob_api_passphrase)
    print(f"[{'OK ' if pk_ok else 'MISS'}] {'Private key':<22}{pk}")
    print(f"[{'OK ' if k_ok else 'MISS'}] {'CLOB api key':<22}{k}")
    print(f"[{'OK ' if sec_ok else 'MISS'}] {'CLOB api secret':<22}{sec}")
    print(f"[{'OK ' if pp_ok else 'MISS'}] {'CLOB passphrase':<22}{pp}")

    print("-" * 68)
    if s.trading_enabled:
        print("[ready] 交易功能配置齐备（私钥 + RPC）。")
    else:
        print("[info]  只读行情功能无需凭证即可运行。")
        print("        如需交易：在 .env 中配置 PM_PRIVATE_KEY 与 PM_POLYGON_RPC_URL。")
    if not s.has_clob_creds:
        print("[info]  CLOB L2 凭证未配置——后续可用私钥通过 L1 签名自动派生。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
