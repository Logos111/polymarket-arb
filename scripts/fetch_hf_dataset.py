"""下载 HF 数据集 btc/eth 子集到 runtime/hf/。"""
import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from huggingface_hub import hf_hub_download

REPO = "kachoio/polymarket-5-minute-crypto-up-down-markets"
FILES = [
    "btc_markets.parquet", "btc_ticks.parquet",
    "eth_markets.parquet", "eth_ticks.parquet",
]

for f in FILES:
    p = hf_hub_download(REPO, f, repo_type="dataset", local_dir="runtime/hf")
    print("ok", p)
