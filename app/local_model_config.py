"""Owner-only backend model configuration. Never return a key to the browser."""
import os
from pathlib import Path
import stat


class ModelConfigurationError(ValueError):
    pass

def key_path():
    root=Path(os.environ.get('V16_DATA_DIR',Path(__file__).resolve().parent.parent/'data'))
    override = os.environ.get('LOCAL8038_QWEN_KEY_FILE')
    if override:
        return Path(override).expanduser()
    local = root / '.local_model_qwen.key'
    protected = local.with_suffix('.dpapi') if os.name == 'nt' else local
    legacy = os.environ.get('LOCAL8018_QWEN_KEY_FILE') if os.name != 'nt' else ''
    # Honor explicitly configured older credentials for reads until this edition has its own.
    return Path(legacy).expanduser() if legacy and not protected.exists() else local

def load_key():
    value=os.environ.get('V16_QWEN_API_KEY','').strip() or os.environ.get('DASHSCOPE_API_KEY','').strip()
    if not value and os.name == 'nt':
        path = key_path().with_suffix('.dpapi')
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ModelConfigurationError('模型授权文件不能是链接或重解析点。')
            if not 0 < info.st_size <= 8192:
                raise ModelConfigurationError('内置模型授权文件格式异常。')
            import win32crypt
            value = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1].decode('utf-8').strip()
        except FileNotFoundError:
            raise ModelConfigurationError('第一版千问授权尚未配置，请设置 V16_QWEN_API_KEY 或使用本机 Codex 登录。') from None
        except ModelConfigurationError:
            raise
        except Exception:
            raise ModelConfigurationError('无法解密授权，请在当前 Windows 用户下重新配置模型授权。') from None
    if not value and os.name != 'nt':
        path=key_path()
        try:
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        except FileNotFoundError:
            raise ModelConfigurationError('内置模型授权尚未配置，暂时不能开始剪辑。') from None
        except OSError:
            raise ModelConfigurationError('内置模型授权文件无法安全读取。') from None
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                raise ModelConfigurationError('内置模型授权文件权限不正确。')
            if not 0<info.st_size<=1024:
                raise ModelConfigurationError('内置模型授权文件格式异常。')
            value=os.read(fd,1025).decode('utf-8').strip()
        except UnicodeError:
            raise ModelConfigurationError('内置模型授权文件格式异常。') from None
        finally:
            os.close(fd)
    if not value or len(value)>512 or any(c.isspace() for c in value):
        raise ModelConfigurationError('内置模型授权格式异常。')
    return value

def status():
    provider = selected_provider()
    if provider == 'codex':
        import codex_bridge
        configured = codex_bridge.logged_in()
        return {'configured': configured, 'provider': 'codex', 'model': codex_bridge.MODEL,
                'message': ('Codex 本机桥接已连接 · ' + codex_bridge.MODEL + '，无需千问 Key。'
                            if configured else 'Codex 本机桥接未登录，请在本机 Codex 中登录后刷新。')}
    try:
        load_key()
        return {'configured': True, 'provider': 'qwen', 'model': 'qwen3.8-max',
                'message': '千问授权已配置，提交时自动校验连接。'}
    except ModelConfigurationError as exc:
        return {'configured': False, 'provider': 'qwen', 'message': str(exc)}

def renderer_environment():
    provider, key = credentials()
    if provider == 'qwen':
        return {'V16_QWEN_API_KEY': key, 'V16_VISION_PROVIDER': 'qwen', 'V16_VISION_USE_CODEX': '0'}
    import codex_bridge
    return {'V16_VISION_PROVIDER': 'codex', 'V16_VISION_USE_CODEX': '1',
            'V16_CODEX_BIN': codex_bridge.executable(), 'V16_CODEX_MODEL': codex_bridge.MODEL,
            'V16_VISION_USE_OPENAI': '0', 'V16_VISION_USE_OPENCLAW': '0', 'V16_VISION_USE_GEMINI': '0'}


def selected_provider():
    """Prefer explicit project Qwen configuration; otherwise use the local CLI."""
    if (os.name != 'nt' or os.environ.get('LOCAL8038_QWEN_KEY_FILE', '').strip()
            or os.environ.get('V16_QWEN_API_KEY', '').strip() or os.environ.get('DASHSCOPE_API_KEY', '').strip()):
        return 'qwen'
    return 'qwen' if key_path().with_suffix('.dpapi').exists() else 'codex'


def credentials():
    provider = selected_provider()
    if provider == 'qwen':
        return provider, load_key()
    import codex_bridge
    codex_bridge.validate()
    return provider, ''
