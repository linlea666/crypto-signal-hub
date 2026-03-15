"""配置文件管理器。

负责 YAML 配置文件的加载、保存和默认值生成。
所有配置变更通过此模块进行，确保单一事实来源。
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from config.schema import AppConfig

logger = logging.getLogger(__name__)

# 配置文件默认路径（项目根目录下）
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


class ConfigManager:
    """配置管理器：加载、验证、保存配置。"""

    def __init__(self, config_path: Path | None = None):
        self._path = config_path or DEFAULT_CONFIG_PATH
        self._config: AppConfig | None = None

    @property
    def config(self) -> AppConfig:
        if self._config is None:
            self._config = self.load()
        return self._config

    @property
    def is_first_run(self) -> bool:
        """配置文件不存在或未完成引导"""
        return not self._path.exists() or not self.config.setup_completed

    def load(self) -> AppConfig:
        """从 YAML 文件加载配置，文件不存在则使用默认值。"""
        if not self._path.exists():
            logger.info("配置文件不存在，使用默认配置: %s", self._path)
            self._config = AppConfig()
            return self._config

        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("配置文件内容非法，使用默认配置")
                self._config = AppConfig()
                return self._config

            self._config = AppConfig(**raw)
            logger.info("配置加载成功: %s", self._path)
            return self._config

        except ValidationError as e:
            logger.error("配置校验失败: %s", e)
            raise
        except yaml.YAMLError as e:
            logger.error("YAML 解析失败: %s", e)
            raise

    def save(self, config: AppConfig | None = None) -> None:
        """将配置保存为 YAML 文件。"""
        if config is not None:
            self._config = config

        if self._config is None:
            raise ValueError("没有可保存的配置")

        data = self._config.model_dump()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        logger.info("配置已保存: %s", self._path)

    def update(self, **kwargs) -> AppConfig:
        """部分更新配置字段。

        支持嵌套更新，如 update(email={"smtp_host": "smtp.qq.com"})
        """
        current_data = self.config.model_dump()
        _deep_merge(current_data, kwargs)
        self._config = AppConfig(**current_data)
        self.save()
        return self._config

    def generate_default(self) -> None:
        """生成带注释的默认配置文件"""
        self._config = AppConfig()
        self.save()
        logger.info("默认配置文件已生成: %s", self._path)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 中的值覆盖 base 中的对应值"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
