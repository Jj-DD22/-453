# 快手自动化剪辑工作台（胡文版 · 硬切配音 + 背景音乐双模式）

本项目包含快手短视频自动化混剪工作台的完整源码，支持 **第一版 · 原版智能混剪** 与 **第二版 · 硬切配音 + 背景音乐（免大模型 Key）** 一键切换。

## 目录结构

- `app/`：单机 / 局域网工作台完整前后端与剪辑渲染内核（默认监听 `8038` 端口，兼容 macOS Apple Silicon VideoToolbox 与 Windows NVIDIA NVENC / CPU）
  - `v16_web_app.py`：Web 工作台服务、素材池管理、9:16 画面框选挡字、配音与背景音乐试听及持久化制作队列
  - `run_v16_enhanced.py`：多轨视频混剪、贴纸叠加、防重裂变与音画合成主引擎
  - `hardcut_audio.py`：固定秒数一刀硬切排片、多音频去重分配、Edge-TTS / CosyVoice / 系统语音合成及背景音乐混流引擎
  - `opening_video.py`：片头视频与核心原片 100% 原画原声无损前置处理模块
  - `task_queue_store.py`：SQLite 持久化任务队列存储
  - `assets/`：滤镜纹理、人脸关键点模型与预览静态资源
  - `tests/`：核心配置、兼容性与队列单元测试
- `server_8020/`：多用户 GPU 服务器版（`8020`）扫码隔离网关、并发槽位调度与局域网直传扩展模块
- `启动快手硬切配音版_8038.command`：macOS 双击一键启动器
- `使用说明.md`：功能与操作说明

## 快速启动

1. 安装依赖（需系统已安装 `Python 3.10+` 与 `ffmpeg` / `ffprobe`）：
   ```bash
   pip install -r app/requirements_v16_web.txt
   ```
2. 启动本地工作台：
   ```bash
   python3 app/local_launcher.py --host 0.0.0.0 --port 8038
   ```
   浏览器将自动打开 `http://127.0.0.1:8038/#pools`。
