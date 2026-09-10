"""一次性检查认证配置状态。"""
import sys

sys.path.insert(0, "src")

from pm_arb.infra.config import Settings

s = Settings()
print("has_private_key:", s.has_private_key)
print("has_clob_creds:", s.has_clob_creds)
print("chain_id:", s.chain_id)
print("signature_type:", s.signature_type)
print("funder:", (s.funder_address[:10] + "...") if s.funder_address else None)
print("clob_api_url:", s.clob_api_url)
print("proxy:", s.proxy_url)

# 钱包地址一致性：私钥派生地址（仅本地计算，不发请求）
if s.has_private_key:
    from eth_account import Account

    acct = Account.from_key(s.private_key)
    print("private_key_address:", acct.address)
