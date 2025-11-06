import os
import sys
import logging
import time
import shutil
import threading
import asyncio
from pathlib import Path
from collections import deque
import gradio as gr
from dotenv import load_dotenv
from rich.traceback import install

# ========== 1. 内存日志缓冲区 ==========
# (这部分保持不变，它独立于日志系统)

# 使用 deque 作为高效的循环缓冲区
log_buffer = deque(maxlen=200)
# 添加一个锁，用于安全地从缓冲区读取
log_buffer_lock = threading.Lock()

class LogBufferHandler(logging.Handler):
    """一个将日志记录发送到 deque 缓冲区的处理器。"""
    def __init__(self, buffer: deque):
        super().__init__()
        self.buffer = buffer

    def emit(self, record):
        # 格式化日志消息
        # 即使 structlog 接管了, logging.Handler 仍然会调用 format()
        try:
            msg = self.format(record)
            # 线程安全地添加到缓冲区
            with log_buffer_lock:
                self.buffer.append(msg)
        except Exception:
            self.handleError(record)

# ========== 2. 环境与日志系统初始化 ==========

# --- 2.1. 环境加载 ---
env_path = Path(__file__).parent / ".env"
template_env_path = Path(__file__).parent / "template" / "template.env"
if env_path.exists():
    load_dotenv(str(env_path), override=True)
    print("✅ 成功加载环境变量配置")
else:
    if template_env_path.exists():
        shutil.copyfile(template_env_path, env_path)
        print("⚠️ 已从模板生成 .env 文件")
        load_dotenv(str(env_path), override=True)
    else:
        print("❌ .env 文件不存在，也未找到模板")

# --- 2.2. 初始化日志系统 ---
# rich 回溯
install(extra_lines=3)

try:
    # 尝试导入并初始化用户的日志系统 (structlog)
    # 这会设置好文件和控制台 handler
    from src.common.logger import initialize_logging, get_logger, shutdown_logging
    initialize_logging()
    logger = get_logger("main_panel") # 从 structlog 获取 logger
    logger.info("✅ 已加载 src.common.logger (structlog) 日志系统。")
    
    # --- 2.3. 附加 UI 缓冲区处理器 ---
    # 既然用户的日志系统已初始化 (它配置了 logging 根)，
    # 我们现在将我们的 LogBufferHandler 添加到根记录器。
    
    ui_handler = LogBufferHandler(log_buffer)
    # 使用一个简单的、无颜色的格式化器，因为Gradio Textbox不支持ANSI颜色
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)-5s [%(name)s] %(message)s',
        datefmt='%m-%d %H:%M:%S'
    )
    ui_handler.setFormatter(formatter)
    ui_handler.setLevel(logging.DEBUG) # 捕获所有
    
    # 添加到根日志记录器
    logging.getLogger().addHandler(ui_handler)
    logger.info("✅ UI 实时日志处理器已附加。")

except ImportError as e:
    print(f"⚠️ 无法导入 'src.common.logger' ({e})。")
    print("⚠️ 将回退到此脚本中的标准日志记录。")
    
    # --- 2.3. (回退) 日志系统设置 ---
    # 这是一个备用方案，如果 src.common.logger 导入失败
    
    def initialize_logging_fallback():
        """配置根日志记录器（回退模式）。"""
        log_handler = LogBufferHandler(log_buffer)
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)-5s [%(name)s] %(message)s',
            datefmt='%m-%d %H:%M:%S'
        )
        log_handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        
        if root_logger.hasHandlers():
            root_logger.handlers.clear()

        root_logger.addHandler(log_handler) # UI 缓冲区
        
        stream_handler = logging.StreamHandler(sys.stdout) # 控制台
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)
        
        log_file_path = Path("logs") / "app_fallback.log" # 文件
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file_path, mode="a", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    initialize_logging_fallback()
    logger = logging.getLogger("main_fallback")
    logger.info(" logging 系统已初始化 (回退模式)。")
    # (定义一个假的 shutdown_logging)
    def shutdown_logging():
        logger.info("日志系统关闭 (回退模式)。")


# ========== 3. 导入主系统 ==========
try:
    from src.main import MainSystem
    from src.manager.async_task_manager import async_task_manager
    from src.plugin_system.core.events_manager import events_manager
    from src.plugin_system.base.component_types import EventType
except ImportError as e:
    logger.critical(f"❌ 无法导入 MainSystem: {e}。系统启动/停止功能将不可用。")
    MainSystem = None
    async_task_manager = None
    events_manager = None
    EventType = None

# ========== 4. 真实系统功能（线程管理） ==========

system_thread: threading.Thread | None = None
loop: asyncio.AbstractEventLoop | None = None
main_system: MainSystem | None = None

async def graceful_shutdown():
    """从你的 app.py 逻辑中获取的优雅关闭"""
    try:
        logger.info("🛑 正在优雅关闭系统...")
        
        if events_manager and EventType:
            await events_manager.handle_mai_events(event_type=EventType.ON_STOP)
        
        if async_task_manager:
            await async_task_manager.stop_and_wait_all_tasks()
        
        # (省略了取消剩余任务的复杂逻辑，假设 async_task_manager 处理了大部分)
        
        logger.info("✅ 优雅关闭完成。")
        
    except Exception as e:
        logger.error(f"关闭异常: {e}", exc_info=True)
    finally:
        # 确保日志系统最后关闭
        if shutdown_logging:
            shutdown_logging()


def run_async_system_thread():
    """在单独的线程中运行 MainSystem 的 asyncio 循环"""
    global loop, main_system
    
    if not MainSystem:
        logger.error("❌ MainSystem 未能导入，无法启动系统。")
        return
        
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        main_system = MainSystem()
        
        logger.info("系统线程：正在初始化 MainSystem...")
        loop.run_until_complete(main_system.initialize())
        
        logger.info("系统线程：初始化完成，启动任务调度...")
        loop.create_task(main_system.schedule_tasks())
        
        # 永远运行，直到被 stop_system 停止
        loop.run_forever()
        
    except Exception as e:
        logger.error(f"❌ 系统主循环异常退出: {e}", exc_info=True)
    finally:
        if loop and not loop.is_closed():
            loop.close()
        logger.info("系统线程：事件循环已关闭。")
        loop = None
        main_system = None


def start_system():
    """Gradio 按钮调用的启动函数（同步）"""
    global system_thread
    if system_thread and system_thread.is_alive():
        logger.warning("系统已在运行中，无需重复启动。")
        return "⚠️ 系统已运行，无需重复启动"

    logger.info("✅ 正在启动系统线程...")
    system_thread = threading.Thread(target=run_async_system_thread, daemon=True)
    system_thread.start()
    
    return "✅ 系统正在启动..."

def stop_system():
    """Gradio 按钮调用的停止函数（同步）"""
    global system_thread, loop, main_system
    
    if not (system_thread and system_thread.is_alive() and loop):
        logger.warning("系统未在运行中。")
        return "⚠️ 系统未启动"

    logger.info("🛑 正在请求优雅关闭...")
    try:
        # 必须在 loop 自己的线程上运行
        future = asyncio.run_coroutine_threadsafe(graceful_shutdown(), loop)
        # 等待关闭完成，设置超时
        future.result(timeout=15)
        logger.info("✅ 优雅关闭已完成。")
        
    except asyncio.TimeoutError:
        logger.error("❌ 优雅关闭超时！")
    except Exception as e:
        logger.error(f"❌ 优雅关闭时出错: {e}", exc_info=True)
    
    finally:
        # 无论如何，都尝试停止循环
        if loop:
            loop.call_soon_threadsafe(loop.stop)
            
        logger.info("正在等待系统线程退出...")
        system_thread.join(timeout=10)
        
        if system_thread.is_alive():
            logger.error("❌ 系统线程未能在 10 秒内停止！")
        else:
            logger.info("✅ 系统线程已退出。")

        system_thread = None
        loop = None
        main_system = None
        
    return "✅ 系统已停止。"

# ========== 5. 配置文件处理 ==========
# (这部分与你之前的代码相同, 只是用了 `logger`)

CONFIG_DIR = Path("config")
DUMMY_BOT_CONFIG = """
# Bot 配置示例
[bot]
name = "MaiBot"
prefix = "!"
""".strip()

DUMMY_MODEL_CONFIG = """
# 模型配置示例
[model.gpt-4]
api_key = "sk-..."
base_url = "https://api.openai.com/v1"
""".strip()

def initialize_configs():
    """确保 config 目录和示例文件存在。"""
    CONFIG_DIR.mkdir(exist_ok=True)
    bot_config_path = CONFIG_DIR / "bot_config.toml"
    model_config_path = CONFIG_DIR / "model_config.toml"
    
    if not bot_config_path.exists():
        with open(bot_config_path, "w", encoding="utf-8") as f:
            f.write(DUMMY_BOT_CONFIG)
        logger.info(f"创建了示例 bot_config.toml")
        
    if not model_config_path.exists():
        with open(model_config_path, "w", encoding="utf-8") as f:
            f.write(DUMMY_MODEL_CONFIG)
        logger.info(f"创建了示例 model_config.toml")

def load_config_content(config_name: str):
    """加载配置文件内容"""
    config_path = CONFIG_DIR / f"{config_name}.toml"
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_path}")
        return f"⚠️ 配置文件不存在: {config_path}"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"读取配置失败: {e}", exc_info=True)
        return f"❌ 读取配置失败: {str(e)}"

def save_config_content(config_name: str, content: str):
    """保存配置文件内容"""
    config_path = CONFIG_DIR / f"{config_name}.toml"
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        
        from src.config.config import reload_global_config, reload_model_config
        
        # 模拟热加载
        logger.info(f"✅ 配置 {config_name}.toml 已保存。")
        if config_name == "bot_config":
            logger.info("重新加载 Bot 配置...")
            reload_global_config()
        elif config_name == "model_config":
            logger.info("重新加载模型配置... ")
            reload_model_config()
        
        return f"✅ 配置 {config_name}.toml 已保存并重载"
    except Exception as e:
        logger.error(f"保存配置失败: {e}", exc_info=True)
        return f"❌ 保存配置失败: {str(e)}"

# ========== 6. 日志 UI 功能 ==========

def tail_log():
    """读取并格式化日志 - 仅从内存缓冲区读取"""
    with log_buffer_lock:
        # 复制列表以避免在迭代时发生更改
        logs = [line for line in log_buffer if "matplotlib" not in line]
    
    if not logs:
        return "暂无日志内容..."
    
    # 缓冲区是按时间顺序添加的，所以直接 join 即可
    return "\n".join(logs)

def clear_log_buffer():
    """清空内存日志缓冲区"""
    with log_buffer_lock:
        log_buffer.clear()
    logger.info("内存日志缓冲区已清空。")
    return "✅ 内存日志已清空"


# ========== 7. Gradio 界面 ==========
# (这部分与你之前的代码相同)

def get_background_css(bg_image_path: str | None = None):
    """生成背景CSS"""
    bg_css = ""
    if bg_image_path and Path(bg_image_path).exists():
        bg_css = f"""
        .gradio-container {{
            background: url('file/{bg_image_path}') no-repeat center center fixed !important;
            background-size: cover !important;
        }}
        .block {{
            background: rgba(255, 255, 255, 0.85) !important;
            backdrop-filter: blur(10px) !important;
            border-radius: 15px !important;
            padding: 20px !important;
        }}
        """
    return bg_css + """
    /* 导航按钮样式 */
    .nav-active {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
        color: white !important;
        font-weight: bold !important;
        box_shadow: 0 4px 12px rgba(102, 126, 234, 0.4) !important;
    }
    """

light_theme = gr.themes.Default(
    primary_hue="blue",
    secondary_hue="cyan",
    neutral_hue="slate"
)

dark_theme = gr.themes.Soft(
    primary_hue="cyan",
    secondary_hue="blue",
    neutral_hue="slate"
).set(
    body_background_fill="linear-gradient(135deg, #1e3a8a 0%, #1e293b 100%)",
    body_text_color="#e2e8f0",
    block_background_fill="#1e293b",
    block_border_color="#334155",
    input_background_fill="#0f172a",
    button_primary_background_fill="linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)",
    button_primary_text_color="#ffffff"
)

BG_IMAGE_PATH = Path("data/background.jpg")
BG_IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)

# 确保配置在启动时存在
initialize_configs()

with gr.Blocks(title="MaiBot Web 控制面板", theme=light_theme, css=get_background_css()) as demo:
    current_theme = gr.State("light")
    current_page = gr.State("main")
    current_bg = gr.State(str(BG_IMAGE_PATH) if BG_IMAGE_PATH.exists() else None)
    
    with gr.Row():
        with gr.Column(scale=8):
            gr.Markdown("# 🤖 MaiBot 控制面板 (Structlog 修复版)")
        with gr.Column(scale=2):
            with gr.Row():
                refresh_btn = gr.Button("🔄 刷新日志", size="sm", scale=1)
                theme_btn = gr.Button("🌗 切换主题", size="sm", scale=1)
    
    with gr.Row():
        nav_main = gr.Button("🏠 主控制台", variant="primary", elem_classes=["nav-active"])
        nav_bot = gr.Button("⚙️ Bot配置")
        nav_model = gr.Button("🤖 模型配置")
    
    # ========== 主控制台页面 ==========
    with gr.Column(visible=True) as page_main:
        with gr.Row():
            start_btn = gr.Button("▶️ 启动系统", variant="primary", scale=1)
            stop_btn = gr.Button("🛑 停止系统", variant="stop", scale=1)
            clear_log_btn = gr.Button("🗑️ 清空内存日志", scale=1)
        
        output_box = gr.Textbox(label="系统状态", lines=2, interactive=False)
        log_output = gr.Textbox(
            label="系统日志（实时更新 - 来自内存缓冲区）", 
            lines=20, 
            interactive=False, 
            max_lines=30,
            autoscroll=True
        )
        
        with gr.Accordion("🎨 自定义背景", open=False):
            bg_upload = gr.Image(label="上传背景图片", type="filepath", height=200)
            bg_status = gr.Textbox(label="状态", lines=1, interactive=False)
            with gr.Row():
                bg_apply = gr.Button("应用背景", variant="primary")
                bg_clear = gr.Button("清除背景")
    
    # ========== Bot配置页面 ==========
    with gr.Column(visible=False) as page_bot:
        gr.Markdown("## ⚙️ Bot 配置文件 (config/bot_config.toml)")
        bot_editor = gr.Code(
            label="Bot配置编辑器",
            lines=25,
            value=load_config_content("bot_config")
        )
        with gr.Row():
            bot_save = gr.Button("💾 保存配置", variant="primary")
            bot_reload = gr.Button("🔄 重新加载")
        bot_status = gr.Textbox(label="操作状态", lines=2, interactive=False)
    
    # ========== 模型配置页面 ==========
    with gr.Column(visible=False) as page_model:
        gr.Markdown("## 🤖 模型配置文件 (config/model_config.toml)")
        model_editor = gr.Code(
            label="模型配置编辑器",
            lines=25,
            value=load_config_content("model_config")
        )
        with gr.Row():
            model_save = gr.Button("💾 保存配置", variant="primary")
            model_reload = gr.Button("🔄 重新加载")
        model_status = gr.Textbox(label="操作状态", lines=2, interactive=False)
    
    # ========== 页面切换逻辑 ==========
    def show_page(page: str):
        return (
            gr.update(visible=page == "main"),
            gr.update(visible=page == "bot"),
            gr.update(visible=page == "model"),
            gr.update(variant="primary" if page == "main" else "secondary"),
            gr.update(variant="primary" if page == "bot" else "secondary"),
            gr.update(variant="primary" if page == "model" else "secondary"),
            page
        )
    
    nav_main.click(
        lambda: show_page("main"),
        outputs=[page_main, page_bot, page_model, nav_main, nav_bot, nav_model, current_page]
    )
    nav_bot.click(
        lambda: show_page("bot"),
        outputs=[page_main, page_bot, page_model, nav_main, nav_bot, nav_model, current_page]
    )
    nav_model.click(
        lambda: show_page("model"),
        outputs=[page_main, page_bot, page_model, nav_main, nav_bot, nav_model, current_page]
    )
    
    # ========== 主题切换 ==========
    def toggle_theme(current: str):
        new_theme = "dark" if current == "light" else "light"
        return new_theme, gr.update(theme=dark_theme if new_theme == "dark" else light_theme)
    
    theme_btn.click(
        toggle_theme,
        inputs=[current_theme],
        outputs=[current_theme, demo]
    )
    
    # ========== 系统控制 ==========
    start_btn.click(start_system, outputs=output_box)
    stop_btn.click(stop_system, outputs=output_box)
    clear_log_btn.click(clear_log_buffer, outputs=log_output)
    
    # ========== 日志刷新 ==========
    timer = gr.Timer(1.0)  # 每秒刷新，更实时
    timer.tick(tail_log, outputs=log_output)
    refresh_btn.click(tail_log, outputs=log_output)
    
    # ========== 背景图片处理 ==========
    def apply_background(image_path: str | None):
        if not image_path:
            return "⚠️ 请先上传图片", None
        try:
            shutil.copy(image_path, BG_IMAGE_PATH)
            logger.info(f"背景图片已应用: {image_path}")
            return f"✅ 背景已应用，请刷新页面查看", str(BG_IMAGE_PATH)
        except Exception as e:
            logger.error(f"应用背景失败: {e}", exc_info=True)
            return f"❌ 应用失败: {str(e)}", None
    
    def clear_background():
        try:
            if BG_IMAGE_PATH.exists():
                BG_IMAGE_PATH.unlink()
            logger.info("背景图片已清除。")
            return "✅ 背景已清除，请刷新页面查看", None
        except Exception as e:
            logger.error(f"清除背景失败: {e}", exc_info=True)
            return f"❌ 清除失败: {str(e)}", None
    
    bg_apply.click(apply_background, inputs=[bg_upload], outputs=[bg_status, current_bg])
    bg_clear.click(clear_background, outputs=[bg_status, current_bg])
    
    # ========== Bot配置操作 ==========
    bot_save.click(
        lambda content: save_config_content("bot_config", content),
        inputs=[bot_editor],
        outputs=[bot_status]
    )
    bot_reload.click(
        lambda: load_config_content("bot_config"),
        outputs=[bot_editor]
    )
    
    # ========== 模型配置操作 ==========
    model_save.click(
        lambda content: save_config_content("model_config", content),
        inputs=[model_editor],
        outputs=[model_status]
    )
    model_reload.click(
        lambda: load_config_content("model_config"),
        outputs=[model_editor]
    )
    
    gr.Markdown("---\n© 2025 MaiBot | Powered by Gradio")

if __name__ == "__main__":
    logger.info("Gradio 应用启动中...")
    demo.launch(server_name="0.0.0.0", server_port=7860, share=True)