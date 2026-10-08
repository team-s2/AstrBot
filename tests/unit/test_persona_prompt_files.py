"""人格提示词文件（persona prompt_files）相关测试。

覆盖：
1. astrbot.core.persona_prompt_files 加载器的路径解析与错误语义
2. astr_main_agent._ensure_persona_and_skills 的注入行为
3. PersonaManager / PersonaService 对 prompt_files 字段的透传与校验
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import astrbot.core.persona_prompt_files as ppf_module
from astrbot.core import astr_main_agent as ama
from astrbot.core.agent.tool import ToolSet
from astrbot.core.db.po import Persona
from astrbot.dashboard.services.persona_service import (
    PersonaService,
    PersonaServiceError,
)
from astrbot.core.persona_mgr import DEFAULT_PERSONALITY, PersonaManager
from astrbot.core.persona_prompt_files import (
    PersonaPromptFileError,
    load_persona_prompt_files,
    resolve_persona_prompt_file_path,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest


# ============================================================
# 加载器：路径解析
# ============================================================


def test_resolve_relative_path_against_base_dir(tmp_path):
    assert resolve_persona_prompt_file_path("a/b.md", tmp_path) == tmp_path / "a" / "b.md"


def test_resolve_absolute_path_untouched():
    assert resolve_persona_prompt_file_path("/abs/f.md", "/base") == Path("/abs/f.md")


def test_resolve_tilde_against_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_persona_prompt_file_path("~/x.md", "/other") == tmp_path / "x.md"


# ============================================================
# 加载器：内容加载
# ============================================================


def test_load_none_or_empty_returns_empty(tmp_path):
    assert load_persona_prompt_files(None, tmp_path) == ""
    assert load_persona_prompt_files([], tmp_path) == ""


def test_load_multiple_files_in_order(tmp_path):
    (tmp_path / "a.md").write_text("规则甲", encoding="utf-8")
    (tmp_path / "b.md").write_text("规则乙", encoding="utf-8")
    result = load_persona_prompt_files(["b.md", "a.md"], tmp_path)
    assert result.startswith("# Persona Prompt Files")
    assert result.index("规则乙") < result.index("规则甲")
    assert "## File: b.md" in result
    assert "## File: a.md" in result


def test_load_rejects_missing_file(tmp_path):
    with pytest.raises(PersonaPromptFileError, match="文件不存在"):
        load_persona_prompt_files(["missing.md"], tmp_path)


def test_load_rejects_directory(tmp_path):
    (tmp_path / "dir").mkdir()
    with pytest.raises(PersonaPromptFileError, match="不是普通文件"):
        load_persona_prompt_files(["dir"], tmp_path)


def test_load_rejects_invalid_utf8(tmp_path):
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(PersonaPromptFileError, match="UTF-8"):
        load_persona_prompt_files(["bin.md"], tmp_path)


def test_load_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(ppf_module, "MAX_PERSONA_PROMPT_FILE_BYTES", 4)
    (tmp_path / "big.md").write_text("aaaaaaaa", encoding="utf-8")
    with pytest.raises(PersonaPromptFileError, match="文件过大"):
        load_persona_prompt_files(["big.md"], tmp_path)


def test_load_skips_empty_files_and_blank_entries(tmp_path):
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    (tmp_path / "ok.md").write_text("内容", encoding="utf-8")
    result = load_persona_prompt_files(["", "empty.md", "ok.md"], tmp_path)
    assert "内容" in result
    assert "empty.md" not in result


def test_load_rejects_non_string_entry(tmp_path):
    with pytest.raises(PersonaPromptFileError, match="不是字符串"):
        load_persona_prompt_files([123], tmp_path)  # type: ignore[list-item]


def test_load_reads_files_fresh_on_every_call(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("v1", encoding="utf-8")
    assert "v1" in load_persona_prompt_files(["a.md"], tmp_path)
    target.write_text("v2", encoding="utf-8")
    assert "v2" in load_persona_prompt_files(["a.md"], tmp_path)


# ============================================================
# 注入：_ensure_persona_and_skills
# ============================================================


def _make_persona(**overrides):
    persona = {
        "prompt": "基础人格提示词",
        "name": "p1",
        "prompt_files": None,
        "begin_dialogs": [],
        "mood_imitation_dialogs": [],
        "tools": [],
        "skills": [],
        "custom_error_message": None,
        "_begin_dialogs_processed": [],
        "_mood_imitation_dialogs_processed": "",
    }
    persona.update(overrides)
    return persona


def _make_event():
    platform_meta = PlatformMetadata(
        id="test_platform",
        name="test_platform",
        description="Test platform",
    )
    message_obj = MagicMock()
    message_obj.message = []

    event = MagicMock(spec=AstrMessageEvent)
    event.message_str = "Hello"
    event.message_obj = message_obj
    event.platform_meta = platform_meta
    event.unified_msg_origin = "test_platform:private:session123"
    event.get_extra.return_value = None
    event.get_platform_name.return_value = "test_platform"
    return event


def _make_context(persona):
    ctx = MagicMock()
    ctx.get_config.return_value = {}
    ctx.persona_manager = MagicMock()
    ctx.persona_manager.resolve_selected_persona = AsyncMock(
        return_value=("p1", persona, None, False)
    )
    tool_mgr = MagicMock()
    tool_mgr.get_full_tool_set.return_value = ToolSet()
    ctx.get_llm_tool_manager.return_value = tool_mgr
    return ctx


async def _run_ensure(persona, req=None):
    event = _make_event()
    ctx = _make_context(persona)
    if req is None:
        req = ProviderRequest(prompt="hi", conversation=SimpleNamespace(persona_id="p1"))
    with patch.object(ama, "SkillManager") as skill_manager:
        skill_manager.return_value.list_skills.return_value = []
        await ama._ensure_persona_and_skills(req, {}, ctx, event)
    return req


@pytest.mark.asyncio
async def test_ensure_persona_appends_prompt_files_section(tmp_path):
    file_a = tmp_path / "a.md"
    file_b = tmp_path / "b.md"
    file_a.write_text("规则甲", encoding="utf-8")
    file_b.write_text("规则乙", encoding="utf-8")

    persona = _make_persona(prompt_files=[str(file_a), str(file_b)])
    req = await _run_ensure(persona)

    assert "# Persona Instructions" in req.system_prompt
    assert "# Persona Prompt Files" in req.system_prompt
    assert "规则甲" in req.system_prompt
    assert "规则乙" in req.system_prompt
    # 人格提示词在前，文件内容在后，文件按配置顺序拼接
    assert req.system_prompt.index("基础人格提示词") < req.system_prompt.index("规则甲")
    assert req.system_prompt.index("规则甲") < req.system_prompt.index("规则乙")


@pytest.mark.asyncio
async def test_ensure_persona_without_prompt_files_keeps_prompt(tmp_path):
    req = await _run_ensure(_make_persona(prompt_files=None))
    assert "基础人格提示词" in req.system_prompt
    assert "Persona Prompt Files" not in req.system_prompt


@pytest.mark.asyncio
async def test_ensure_persona_tolerates_missing_prompt_files_key(tmp_path):
    persona = _make_persona()
    del persona["prompt_files"]
    req = await _run_ensure(persona)
    assert "Persona Prompt Files" not in req.system_prompt


@pytest.mark.asyncio
async def test_ensure_persona_raises_on_broken_prompt_file(tmp_path):
    persona = _make_persona(prompt_files=[str(tmp_path / "missing.md")])
    with pytest.raises(PersonaPromptFileError, match="人格提示词文件读取失败"):
        await _run_ensure(persona)


# ============================================================
# PersonaManager：prompt_files 透传到 v3 结构
# ============================================================


def test_persona_manager_maps_prompt_files_to_v3():
    acm = MagicMock()
    acm.default_conf = {
        "agent_runner": {
            "runner_type": "local",
            "config": {"persona": {"persona_id": "default"}},
        }
    }
    manager = PersonaManager(db_helper=MagicMock(), acm=acm)
    manager.personas = [
        Persona(
            persona_id="p1",
            system_prompt="s",
            prompt_files=["a.md", "b.md"],
        ),
        Persona(persona_id="default", system_prompt="d"),
    ]

    v3_config, personas_v3, selected = manager.get_v3_persona_data()

    assert v3_config[0]["prompt_files"] == ["a.md", "b.md"]
    assert personas_v3[0]["prompt_files"] == ["a.md", "b.md"]
    assert selected["prompt_files"] is None
    assert manager.selected_default_persona.prompt_files is None
    assert DEFAULT_PERSONALITY["prompt_files"] is None


# ============================================================
# PersonaService：prompt_files 校验与创建约束
# ============================================================


def _make_service() -> tuple[PersonaService, MagicMock]:
    lifecycle = MagicMock()
    lifecycle.persona_mgr = MagicMock()
    lifecycle.persona_mgr.create_persona = AsyncMock(
        return_value=Persona(persona_id="x", system_prompt="")
    )
    return PersonaService(lifecycle), lifecycle


def test_normalize_prompt_files_rejects_non_list():
    with pytest.raises(PersonaServiceError):
        PersonaService._normalize_prompt_files("a.md")


def test_normalize_prompt_files_rejects_non_string_item():
    with pytest.raises(PersonaServiceError):
        PersonaService._normalize_prompt_files(["a.md", 1])


def test_normalize_prompt_files_strips_and_drops_empty():
    assert PersonaService._normalize_prompt_files([" a.md ", "", "  "]) == ["a.md"]
    assert PersonaService._normalize_prompt_files([]) is None
    assert PersonaService._normalize_prompt_files(None) is None


@pytest.mark.asyncio
async def test_service_create_requires_prompt_or_files():
    service, _ = _make_service()
    with pytest.raises(PersonaServiceError):
        await service.create_persona({"persona_id": "x", "system_prompt": ""})


@pytest.mark.asyncio
async def test_service_create_allows_empty_prompt_with_files():
    service, lifecycle = _make_service()
    await service.create_persona(
        {"persona_id": "x", "system_prompt": "", "prompt_files": [" a.md "]}
    )
    kwargs = lifecycle.persona_mgr.create_persona.await_args.kwargs
    assert kwargs["prompt_files"] == ["a.md"]
    assert kwargs["system_prompt"] == ""


@pytest.mark.asyncio
async def test_service_update_passes_prompt_files_only_when_present():
    service, lifecycle = _make_service()
    lifecycle.persona_mgr.update_persona = AsyncMock(return_value=None)

    await service.update_persona({"persona_id": "x", "system_prompt": "s"})
    assert "prompt_files" not in lifecycle.persona_mgr.update_persona.await_args.kwargs

    await service.update_persona(
        {"persona_id": "x", "prompt_files": ["a.md", ""]}
    )
    assert (
        lifecycle.persona_mgr.update_persona.await_args.kwargs["prompt_files"]
        == ["a.md"]
    )
