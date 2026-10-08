"""人格提示词文件加载。

支持在 Persona 中配置一系列文件路径，运行时读取文件内容并注入到
系统提示词中，从而可以通过 git 仓库等方式管理人格提示词，而不是
仅存放在 AstrBot 数据库里。

文件在每次请求时重新读取（不缓存），因此仓库内容更新后无需重启即可生效。
任一文件存在问题（缺失、不可读、编码非法、超过大小上限）时抛出
:class:`PersonaPromptFileError`，由上层在会话中向用户报错。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

__all__ = [
    "MAX_PERSONA_PROMPT_FILE_BYTES",
    "PersonaPromptFileError",
    "load_persona_prompt_files",
    "resolve_persona_prompt_file_path",
]

MAX_PERSONA_PROMPT_FILE_BYTES = 512 * 1024
"""单个提示词文件的大小上限（字节）。"""

_PERSONA_PROMPT_FILES_HEADER = "# Persona Prompt Files"


class PersonaPromptFileError(Exception):
    """人格提示词文件读取失败。"""


def resolve_persona_prompt_file_path(
    raw: str, base_dir: str | Path | None = None
) -> Path:
    """把配置中的文件路径解析为绝对路径。

    绝对路径原样使用；相对路径基于 AstrBot 数据目录（或 ``base_dir``）解析；
    支持 ``~`` 开头的路径。
    """
    expanded = Path(raw.strip()).expanduser()
    if not expanded.is_absolute():
        root = base_dir if base_dir is not None else get_astrbot_data_path()
        expanded = Path(root) / expanded
    return expanded


def _read_prompt_file(path: Path, display: str) -> str:
    if not path.exists():
        raise PersonaPromptFileError(f"文件不存在: {display}")
    if not path.is_file():
        raise PersonaPromptFileError(f"不是普通文件: {display}")
    size = path.stat().st_size
    if size > MAX_PERSONA_PROMPT_FILE_BYTES:
        raise PersonaPromptFileError(
            f"文件过大: {display}"
            f"（{size} 字节，上限 {MAX_PERSONA_PROMPT_FILE_BYTES} 字节）"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise PersonaPromptFileError(f"文件不是合法的 UTF-8 文本: {display}") from None
    except OSError as exc:
        raise PersonaPromptFileError(f"文件读取失败: {display}（{exc}）") from exc


def load_persona_prompt_files(
    prompt_files: list[str] | None, base_dir: str | Path | None = None
) -> str:
    """读取人格配置的全部提示词文件，返回拼接后的提示词片段。

    按配置顺序拼接，空文件跳过；全部为空时返回空字符串。
    任一文件读取失败时抛出 :class:`PersonaPromptFileError`。
    """
    if not prompt_files:
        return ""
    sections: list[str] = []
    for raw in prompt_files:
        if not isinstance(raw, str):
            raise PersonaPromptFileError(f"配置的文件路径不是字符串: {raw!r}")
        display = raw.strip()
        if not display:
            continue
        path = resolve_persona_prompt_file_path(display, base_dir)
        content = _read_prompt_file(path, display)
        if not content.strip():
            continue
        sections.append(f"## File: {display}\n\n{content.rstrip()}")
    if not sections:
        return ""
    return f"{_PERSONA_PROMPT_FILES_HEADER}\n\n" + "\n\n".join(sections)
