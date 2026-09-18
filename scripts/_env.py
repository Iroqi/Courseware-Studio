"""
统一环境变量加载模块。

查找优先级：
  1. CLI 显式值
  2. 系统环境变量（os.environ）
  3. 项目级 .env
  4. 用户级 ~/.config/courseware-studio/.env

本 skill 不读取其它 skill 的私有配置目录。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _script_utils import strip_env_comment  # noqa: E402


# 用户级 .env 路径
_USER_ENV_PATH = os.path.join(os.path.expanduser("~"), ".config", "courseware-studio", ".env")
DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"

_ENV_CACHE = {}  # path -> 解析结果（.env 在单次 CLI 进程内稳定，缓存避免每次调用重复 3 编码探测）


def _candidate_project_envs(project_dir=None, source_path=None):
    """返回项目级 .env 候选，优先离 source 最近的项目根目录。

    优先级：
      1. project_dir/.env（Create 明确传入的项目目录）
      2. source_path 所在目录及其父级目录直到当前工作目录边界
      3. 当前工作目录/.env

    仅读取文件，不把 .env 复制进 Artifact。
    """
    candidates = []
    seen = set()

    def add(path):
        path = os.path.abspath(path)
        env_path = os.path.join(path, ".env")
        if env_path not in seen:
            seen.add(env_path)
            candidates.append(env_path)

    roots = []
    if project_dir:
        roots.append(os.path.abspath(project_dir))
    if source_path:
        roots.append(os.path.dirname(os.path.abspath(source_path)))
    roots.append(os.getcwd())

    for root in roots:
        cur = root
        while True:
            add(cur)
            # 项目根边界：cur 里有 .git（目录，或 worktree/submodule 的 gitfile）
            # 就是项目根，继续向上会把无关目录的 .env 吸进来（盘根/家目录都拦不住）。
            if os.path.exists(os.path.join(cur, ".git")):
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            # 不无限向上吸收用户家目录之外的无关 .env；到 cwd/home 即停止。
            if parent == os.path.dirname(os.path.abspath(os.getcwd())) and root != os.getcwd():
                add(parent)
                break
            cur = parent
            if cur == os.path.expanduser("~"):
                add(cur)
                break

    return candidates


def find_project_env(project_dir=None, source_path=None):
    """返回第一个存在的项目级 .env 路径，否则 None。"""
    for path in _candidate_project_envs(project_dir=project_dir, source_path=source_path):
        if os.path.isfile(path):
            return path
    return None


def _parse_env_file(path):
    """解析一个 KEY=VALUE 格式的 .env 文件，返回 dict（带进程内缓存）。"""
    if path in _ENV_CACHE:
        return _ENV_CACHE[path]
    result = _parse_env_file_raw(path)
    _ENV_CACHE[path] = result
    return result


def _parse_env_file_raw(path):
    """解析一个 KEY=VALUE 格式的 .env 文件，返回 dict。

    编码探测链：utf-8-sig → utf-16 → gb18030，全部失败才放弃并明确指向
    编码问题。UnicodeDecodeError 是 ValueError 子类，不在这里接住就会从
    load_env()/get_key() 裸栈穿透——两个高概率触发路径：PowerShell 5.1
    重定向 `>` 产出 UTF-16LE 带 BOM；记事本把含中文注释的 .env 存成 ANSI
    (GBK)。爆炸点在 get_key() 内部意味着 命令行 dry-run 一起瘫痪，
    且报错完全不指向"编码问题"。
    """
    result = {}
    if not os.path.isfile(path):
        return result
    text = None
    for enc in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
            break
        except UnicodeDecodeError:
            continue
        except (OSError, IOError) as e:
            print(f"[warn] 无法读取 {path}: {e}", file=sys.stderr)
            return result
    if text is None:
        print(f"[warn] 无法读取 {path}: 不是可识别的文本编码"
              f"(尝试过 utf-8 / utf-16 / gb18030)。"
              f"请用 UTF-8 重新保存该文件（记事本另存为右下角选 UTF-8）",
              file=sys.stderr)
        return result
    if "\x00" in text:
        # 无 BOM 的 UTF-16 能被 utf-8 "成功"解码成夹 NUL 的字符串：解析不报错、
        # 但 key 全部带 \x00 查表静默 miss，报错还指向"没有 API key"而不是编码
        # 问题。宁可在这里放弃并提示，也不静默吞掉。
        print(f"[warn] {path} 内容含 NUL 字节——多半是无 BOM 的 UTF-16 编码"
              f"(PowerShell 5.1 重定向产物)。请用 UTF-8 重新保存该文件",
              file=sys.stderr)
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            # 兼容 shell 习惯写法（export KEY=VALUE）：
            # key 上的 export 前缀剥掉，否则查表永远 miss
            if k.startswith("export "):
                k = k[len("export "):].strip()
            v = v.strip()
            # 引号感知地剥行内注释与成对引号（"sk-xxx" / sk-xxx # 注释）：
            # 原样读入会把引号或注释一起带给 OpenAI SDK，得到 401 且
            # 报错不指向真正原因。规则见 _script_utils.strip_env_comment。
            v = strip_env_comment(v)
            if k:
                result[k] = v
    return result


def load_env(project_dir=None, source_path=None):
    """按优先级合并项目级、用户级与进程环境变量。

    对最终密钥解析而言，``get_key`` 使用 CLI > os.environ > project .env > user .env。
    这里主要用于模型/非敏感配置的统一解析。
    """
    merged = {}
    user_env = _parse_env_file(_USER_ENV_PATH)
    merged.update(user_env)
    project_env_path = find_project_env(project_dir=project_dir, source_path=source_path)
    if project_env_path:
        merged.update(_parse_env_file(project_env_path))
    for k, v in os.environ.items():
        if v:
            merged[k] = v
    return merged


def get_key(name, cli_value=None, project_dir=None, source_path=None):
    """获取单个密钥。

    优先级：CLI > os.environ > project .env > 用户级 .env。
    """
    if cli_value:
        return cli_value
    val = os.environ.get(name)
    if val:
        return val
    project_env_path = find_project_env(project_dir=project_dir, source_path=source_path)
    if project_env_path:
        val = _parse_env_file(project_env_path).get(name)
        if val:
            return val
    user_env = _parse_env_file(_USER_ENV_PATH)
    return user_env.get(name) or None


def resolve_model_config(cli_model, cli_base_url, model_env_name, default_model,
                         project_dir=None, source_path=None):
    """按 CLI > 环境变量 > 项目 .env > 用户 .env > 默认值解析模型配置。"""
    env = load_env(project_dir=project_dir, source_path=source_path)
    model = cli_model or env.get(model_env_name) or default_model
    base_url = cli_base_url or env.get("MIMO_BASE_URL") or DEFAULT_BASE_URL
    return model, base_url
