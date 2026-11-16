"""Owner-only backend model configuration. Never return a key to the browser."""
import os
from pathlib import Path
import stat

class ModelConfigurationError(ValueError):
    pass

def key_path():
    root=Path(os.environ.get('V16_DATA_DIR',Path(__file__).resolve().parent.parent/'data'))
    return Path(os.environ.get('LOCAL8018_QWEN_KEY_FILE',str(root/'.local_model_qwen.key'))).expanduser()

def load_key():
    value=os.environ.get('V16_QWEN_API_KEY','').strip() or os.environ.get('DASHSCOPE_API_KEY','').strip()
    if not value:
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
    try:
        load_key()
        return {'configured':True,'provider':'qwen','message':'内置模型已配置，提交时自动校验连接'}
    except ModelConfigurationError as exc:
        return {'configured':False,'provider':'qwen','message':str(exc)}

def renderer_environment():
    # Reload on each task, including retries and cold restarts. No queue secrets.
    return {'V16_QWEN_API_KEY':load_key()}
