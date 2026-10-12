"""v4 screener: daily data pipeline + Quality score for coins >= $100M market cap (docs/PRODUCT.md).

Isolated from the v2/v3 code: own database (`screener.db_path`), own HTTP client with call budgets and a disk cache.
Master id = CoinGecko coin id. Every stored data point carries a source URL.
"""
