"""链上操作：USDC 余额/授权、CTF split/merge/redeem。

套利闭环的链上半环：
- ``split``：USDC → YES+NO（$1 铸造一对，用于"拆分卖出"套利）；
- ``merge``：YES+NO → USDC（即时锁利，无需等待结算）；
- ``redeem``：结算后赎回中奖头寸。

合约地址优先取 py-clob-client 内置配置（与交易所一致）。
所有写操作等待回执并校验 status；调用方负责风控与金额核对。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal

from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

# 二元市场：父集合为零、分区为 [1,2]
ZERO_BYTES32 = "0x" + "00" * 32
BINARY_PARTITION = [1, 2]
BINARY_INDEX_SETS = [1, 2]

_ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
]

# CTF（Conditional Tokens Framework）最小 ABI
_CTF_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "id", "type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "setApprovalForAll", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "outputs": []},
    {"name": "isApprovedForAll", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}],
     "outputs": [{"type": "bool"}]},
    {"name": "splitPosition", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "collateralToken", "type": "address"},
         {"name": "parentCollectionId", "type": "bytes32"},
         {"name": "conditionId", "type": "bytes32"},
         {"name": "partition", "type": "uint256[]"},
         {"name": "amount", "type": "uint256"}],
     "outputs": []},
    {"name": "mergePositions", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "collateralToken", "type": "address"},
         {"name": "parentCollectionId", "type": "bytes32"},
         {"name": "conditionId", "type": "bytes32"},
         {"name": "partition", "type": "uint256[]"},
         {"name": "amount", "type": "uint256"}],
     "outputs": []},
    {"name": "redeemPositions", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "collateralToken", "type": "address"},
         {"name": "parentCollectionId", "type": "bytes32"},
         {"name": "conditionId", "type": "bytes32"},
         {"name": "indexSets", "type": "uint256[]"}],
     "outputs": []},
]


@dataclass(frozen=True)
class ChainAddresses:
    exchange: str
    collateral: str  # USDC.e
    conditional_tokens: str
    neg_risk_adapter: str


def _load_addresses(chain_id: int, neg_risk_adapter: str) -> ChainAddresses:
    from py_clob_client.config import get_contract_config

    cc = get_contract_config(chain_id)
    return ChainAddresses(
        exchange=cc.exchange,
        collateral=cc.collateral,
        conditional_tokens=cc.conditional_tokens,
        neg_risk_adapter=neg_risk_adapter,
    )


class ChainClient:
    """Polygon 链上操作。同步 web3 调用，对外提供 async 方法（to_thread）。"""

    def __init__(self, settings: Settings | None = None):
        from web3 import Web3

        self._settings = settings or get_settings()
        if not self._settings.has_private_key:
            raise RuntimeError("链上操作需要 PM_PRIVATE_KEY")
        if self._settings.proxy_url:
            # web3 的 requests session 信任环境变量代理
            os.environ.setdefault("HTTPS_PROXY", self._settings.proxy_url)

        self.w3 = Web3(Web3.HTTPProvider(self._settings.polygon_rpc_url))
        self.account = self.w3.eth.account.from_key(self._settings.private_key)
        self.address = self.account.address
        self.addrs = _load_addresses(self._settings.chain_id, self._settings.neg_risk_adapter)

        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.addrs.collateral), abi=_ERC20_ABI
        )
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.addrs.conditional_tokens), abi=_CTF_ABI
        )
        self._usdc_decimals: int | None = None

    # ---- 基础工具 ----

    @property
    async def usdc_decimals(self) -> int:
        if self._usdc_decimals is None:
            self._usdc_decimals = await asyncio.to_thread(self.usdc.functions.decimals().call)
        return self._usdc_decimals

    async def _to_base_units(self, amount: Decimal) -> int:
        d = await self.usdc_decimals
        return int(amount * (10 ** d))

    async def _send(self, fn) -> str:
        """构建、签名、发送交易并等待回执；返回 tx hash。"""
        account = self.account

        def _do() -> str:
            w3 = self.w3
            nonce = w3.eth.get_transaction_count(account.address)
            base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
            priority = w3.eth.max_priority_fee
            tx = fn.build_transaction({
                "from": account.address,
                "nonce": nonce,
                "chainId": self._settings.chain_id,
                "maxPriorityFeePerGas": priority,
                "maxFeePerGas": base_fee * 2 + priority,
            })
            tx["gas"] = w3.eth.estimate_gas(tx)
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if rcpt.get("status") != 1:
                raise RuntimeError(f"交易失败 status={rcpt.get('status')} hash={tx_hash.hex()}")
            return tx_hash.hex()

        return await asyncio.to_thread(_do)

    # ---- 读操作 ----

    async def usdc_balance(self) -> Decimal:
        bal = await asyncio.to_thread(
            self.usdc.functions.balanceOf(self.address).call
        )
        return Decimal(bal) / (10 ** await self.usdc_decimals)

    async def usdc_allowance(self, spender: str) -> Decimal:
        amt = await asyncio.to_thread(
            self.usdc.functions.allowance(self.address, spender).call
        )
        return Decimal(amt) / (10 ** await self.usdc_decimals)

    async def ctf_balance(self, token_id: str | int) -> int:
        """某 outcome token 的持仓数量（整数份，1 份 = $1 面值）。"""
        return await asyncio.to_thread(
            self.ctf.functions.balanceOf(self.address, int(token_id)).call
        )

    # ---- 授权 ----

    async def ensure_allowance(
        self, spender: str, min_amount: Decimal = Decimal("1000000")
    ) -> str | None:
        """授权不足则发起 approve（默认大额授权）。返回 tx hash 或 None（已授权）。"""
        current = await self.usdc_allowance(spender)
        if current >= min_amount:
            return None
        log.info("chain_approve", spender=spender, current=str(current))
        amount = await self._to_base_units(min_amount)
        return await self._send(self.usdc.functions.approve(spender, amount))

    async def ensure_exchange_approval(self) -> str | None:
        return await self.ensure_allowance(self.addrs.exchange)

    async def ensure_ctf_approval(self) -> str | None:
        """split 需要 CTF 合约动用 USDC。"""
        return await self.ensure_allowance(self.addrs.conditional_tokens)

    # ---- split / merge / redeem ----

    async def split(self, condition_id: str, amount: Decimal) -> str:
        """存入 amount USDC，铸造 amount 份 YES + amount 份 NO。"""
        base = await self._to_base_units(amount)
        log.info("chain_split", condition=condition_id[:10], amount=str(amount))
        fn = self.ctf.functions.splitPosition(
            self.addrs.collateral,
            bytes.fromhex(ZERO_BYTES32[2:]),
            bytes.fromhex(condition_id[2:]),
            BINARY_PARTITION,
            base,
        )
        return await self._send(fn)

    async def merge(self, condition_id: str, amount: Decimal) -> str:
        """销毁 amount 份 YES + amount 份 NO，赎回 amount USDC（套利锁利）。"""
        base = await self._to_base_units(amount)
        log.info("chain_merge", condition=condition_id[:10], amount=str(amount))
        fn = self.ctf.functions.mergePositions(
            self.addrs.collateral,
            bytes.fromhex(ZERO_BYTES32[2:]),
            bytes.fromhex(condition_id[2:]),
            BINARY_PARTITION,
            base,
        )
        return await self._send(fn)

    async def redeem(self, condition_id: str, index_sets: list[int] | None = None) -> str:
        """结算后赎回。二元市场默认 [1,2]。"""
        log.info("chain_redeem", condition=condition_id[:10])
        fn = self.ctf.functions.redeemPositions(
            self.addrs.collateral,
            bytes.fromhex(ZERO_BYTES32[2:]),
            bytes.fromhex(condition_id[2:]),
            index_sets or BINARY_INDEX_SETS,
        )
        return await self._send(fn)
