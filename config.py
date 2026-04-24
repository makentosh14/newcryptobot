"""
config.py
---------
Centralized configuration loaded from environment variables (.env).

All other modules import `settings` from here. No other source of truth.

Safety:
- TRADE_MODE defaults to "paper".
- Live trading requires BOTH ENABLE_LIVE_TRADING=True AND I_ACCEPT_LIVE_RISK=True.
- Missing/invalid values fail fast with a clear error at startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal

from pydantic import field_validator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """All runtime configuration. Loaded once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Mode ----------
    TRADE_MODE: Literal["paper", "live"] = "paper"
    ENABLE_LIVE_TRADING: bool = False
    I_ACCEPT_LIVE_RISK: bool = False
    BYBIT_TESTNET: bool = True

    # ---------- Bybit API ----------
    BYBIT_API_KEY: str = ""
    BYBIT_API_SECRET: str = ""

    # ---------- Telegram ----------
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ENABLED: bool = False

    # ---------- Risk ----------
    ACCOUNT_RISK_PER_TRADE_PCT: float = 0.5
    MAX_OPEN_POSITIONS: int = 3
    MAX_DAILY_LOSS_PCT: float = 3.0
    MAX_LOSS_STREAK: int = 4
    LOSS_STREAK_COOLDOWN_MIN: int = 60
    MIN_RR: float = 1.5
    MAX_SPREAD_BPS: float = 5.0
    DEFAULT_LEVERAGE: int = 5

    # ---------- Strategy ----------
    SCAN_INTERVAL_SEC: int = 15
    MIN_SCORE_TO_TRADE: float = 70.0
    TIMEFRAMES: str = "1,5,15,60,240"
    SYMBOL_REFRESH_HOURS: int = 6

    # ---------- Paper ----------
    PAPER_STARTING_BALANCE_USDT: float = 1000.0
    PAPER_SLIPPAGE_BPS: float = 2.0
    PAPER_TAKER_FEE_BPS: float = 6.0

    # ---------- Logging ----------
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_DIR: str = "logs"

    # ---------- Validators ----------
    @field_validator("ACCOUNT_RISK_PER_TRADE_PCT")
    @classmethod
    def _risk_pct(cls, v: float) -> float:
        if not 0 < v <= 5.0:
            raise ValueError("ACCOUNT_RISK_PER_TRADE_PCT must be in (0, 5]")
        return v

    @field_validator("MAX_DAILY_LOSS_PCT")
    @classmethod
    def _daily_loss(cls, v: float) -> float:
        if not 0 < v <= 50.0:
            raise ValueError("MAX_DAILY_LOSS_PCT must be in (0, 50]")
        return v

    @field_validator("MIN_RR")
    @classmethod
    def _min_rr(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("MIN_RR must be >= 1.0")
        return v

    @field_validator("DEFAULT_LEVERAGE")
    @classmethod
    def _leverage(cls, v: int) -> int:
        if not 1 <= v <= 25:
            raise ValueError("DEFAULT_LEVERAGE must be in [1, 25]")
        return v

    # ---------- Computed helpers ----------
    @computed_field  # type: ignore[misc]
    @property
    def timeframes_list(self) -> List[str]:
        return [tf.strip() for tf in self.TIMEFRAMES.split(",") if tf.strip()]

    @computed_field  # type: ignore[misc]
    @property
    def log_dir_path(self) -> Path:
        p = PROJECT_ROOT / self.LOG_DIR
        p.mkdir(parents=True, exist_ok=True)
        return p

    @computed_field  # type: ignore[misc]
    @property
    def is_live_armed(self) -> bool:
        """Live orders require mode=live AND both safety flags True."""
        return (
            self.TRADE_MODE == "live"
            and self.ENABLE_LIVE_TRADING
            and self.I_ACCEPT_LIVE_RISK
        )

    def summary(self) -> str:
        """Safe-to-log config summary. No secrets."""
        return (
            f"mode={self.TRADE_MODE} | live_armed={self.is_live_armed} | "
            f"testnet={self.BYBIT_TESTNET} | "
            f"risk/trade={self.ACCOUNT_RISK_PER_TRADE_PCT}% | "
            f"max_pos={self.MAX_OPEN_POSITIONS} | "
            f"timeframes={self.timeframes_list} | "
            f"min_score={self.MIN_SCORE_TO_TRADE} | "
            f"telegram={self.TELEGRAM_ENABLED}"
        )


# Single import point for the rest of the codebase.
settings = Settings()
