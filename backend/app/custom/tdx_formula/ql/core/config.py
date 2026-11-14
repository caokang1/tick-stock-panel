"""数据目录。行情放在 TSP 数据目录下的 freecn 文件夹里，由扩展在启动时指定。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.custom.tdx_formula.ql.core.errors import DataError

_PACKAGE = Path(__file__).resolve().parents[1]
_data_dir: Path | None = None


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    @property
    def market_dir(self) -> Path:
        return self.data_dir / "market"

    @property
    def daily_dir(self) -> Path:
        return self.market_dir / "daily"

    @property
    def index_dir(self) -> Path:
        return self.market_dir / "index"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "lock"

    @property
    def user_sources_config(self) -> Path:
        return self.data_dir / "config" / "data_sources.toml"

    @property
    def default_sources_config(self) -> Path:
        return _PACKAGE / "data" / "data_sources.default.toml"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.market_dir, self.daily_dir, self.index_dir,
                  self.user_sources_config.parent):
            d.mkdir(parents=True, exist_ok=True)


def set_data_dir(path: Path) -> None:
    global _data_dir
    _data_dir = Path(path)


def is_configured() -> bool:
    return _data_dir is not None


def get_settings() -> Settings:
    if _data_dir is None:
        raise DataError("行情数据目录还没有就绪。")
    return Settings(data_dir=_data_dir)
