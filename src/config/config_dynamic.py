# src/config/config_dynamic.py
import os
import time
import tomlkit
import threading
from pathlib import Path
from src.common.logger import get_logger

logger = get_logger("config_dynamic")

CONFIG_DIR = Path(__file__).parent
BOT_CONFIG_PATH = CONFIG_DIR / "bot_config.toml"
MODEL_CONFIG_PATH = CONFIG_DIR / "model_config.toml"

# ---------------------------
# 加载函数
# ---------------------------

def load_toml(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return tomlkit.parse(f.read())

class DynamicConfig:
    """动态配置管理器"""

    def __init__(self, path: Path):
        self.path = path
        self._config = load_toml(path)
        self._last_modified = path.stat().st_mtime
        self._lock = threading.Lock()

    def get(self):
        """返回当前配置（线程安全）"""
        with self._lock:
            return self._config

    def reload_if_changed(self):
        """如果文件修改时间变化，则重新加载"""
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._last_modified:
                with self._lock:
                    self._config = load_toml(self.path)
                    self._last_modified = mtime
                logger.info(f"[配置] 检测到 {self.path.name} 变更，已自动热加载")
        except Exception as e:
            logger.error(f"[配置] 热加载失败: {e}")

# ---------------------------
# 启动时初始化两个配置
# ---------------------------
bot_config = DynamicConfig(BOT_CONFIG_PATH)
model_config = DynamicConfig(MODEL_CONFIG_PATH)

def get_bot_config():
    logger.info("重新加载 Bot 配置...")
    bot_config.reload_if_changed()
    return bot_config.get()

def get_model_config():
    logger.info("重新加载 模型 配置...")
    model_config.reload_if_changed()
    return model_config.get()
