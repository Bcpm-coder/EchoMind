import asyncio
import json

from agents.agent_orchestrator import (
    BillingAgent,
    EscalationAgent,
    GeneralAgent,
    Request,
    TechnicalAgent,
)
from core.skill_loader import SkillManager


class FakeClient:
    def __init__(self):
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                return type("Response", (), {"content": []})()

        self.messages = Messages()


def _skill_manager(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_defs = [
        {
            "name": "技术支持处理规范",
            "description": "处理登录失败和错误码",
            "keywords": ["401", "登录失败"],
            "agents": ["technical"],
            "instructions": "TECH_ONLY_BODY：先核对 Token 是否过期。",
        },
        {
            "name": "账单退款处理规范",
            "description": "处理退款和重复扣款",
            "keywords": ["退款", "重复扣款"],
            "agents": ["billing"],
            "instructions": "BILLING_ONLY_BODY：任何退款都需先核验订单。",
        },
        {
            "name": "全局安全规范",
            "description": "所有 Agent 可用的安全规则",
            "keywords": ["密码"],
            "agents": [],
            "instructions": "GLOBAL_BODY：不得索要用户密码。",
        },
    ]
    for index, skill in enumerate(skill_defs):
        (skills_dir / f"skill_{index}.json").write_text(
            json.dumps(skill, ensure_ascii=False),
            encoding="utf-8",
        )
    manager = SkillManager(str(skills_dir))
    manager.load()
    return manager


def _request(message="登录时报 401"):
    return Request(message=message, user_id="u1", conv_id="c1")


def test_all_agents_expose_same_skill_tool_with_role_specific_catalog(tmp_path):
    manager = _skill_manager(tmp_path)
    agents = (
        GeneralAgent(FakeClient(), "test-model", skill_manager=manager),
        TechnicalAgent(FakeClient(), "test-model", skill_manager=manager),
        BillingAgent(FakeClient(), "test-model", skill_manager=manager),
        EscalationAgent(FakeClient(), "test-model", skill_manager=manager),
    )

    for agent in agents:
        assert "load_skills" in agent.get_tools()

    technical_prompt = agents[1]._build_system_prompt(_request())
    assert "技术支持处理规范" in technical_prompt
    assert "全局安全规范" in technical_prompt
    assert "账单退款处理规范" not in technical_prompt
    assert "TECH_ONLY_BODY" not in technical_prompt
    assert "GLOBAL_BODY" not in technical_prompt


def test_skill_tool_enforces_whitelist_even_for_explicit_name(tmp_path):
    manager = _skill_manager(tmp_path)
    agent = TechnicalAgent(FakeClient(), "test-model", skill_manager=manager)
    tool = agent.get_tools()["load_skills"]

    denied = tool.handler(
        _request("我想退款"),
        {"query": "我想退款", "skill_names": ["账单退款处理规范"]},
    )
    allowed = tool.handler(_request(), {"query": "登录时报 401"})

    assert denied["success"] is False
    assert denied["skills"] == []
    assert denied["denied_skill_names"] == ["账单退款处理规范"]
    assert "BILLING_ONLY_BODY" not in str(denied)
    assert allowed["success"] is True
    assert allowed["loaded_skill_names"] == ["技术支持处理规范"]
    assert "TECH_ONLY_BODY" in allowed["skills"][0]["instructions"]


def test_skill_tool_result_is_injected_into_current_llm_context(tmp_path):
    manager = _skill_manager(tmp_path)

    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_skill_1"
        name = "load_skills"
        input = {"query": "登录时报 401"}

    class TextBlock:
        type = "text"
        text = "请先检查 Token 是否过期。"

    class ToolClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    client = ToolClient()
    agent = TechnicalAgent(client, "test-model", skill_manager=manager)
    response = asyncio.run(agent.handle(_request()))

    assert response.success is True
    assert response.tools_used == ["load_skills"]
    assert response.tool_traces[0]["loaded_skills"] == ["技术支持处理规范"]
    assert len(client.calls) == 2
    second_call_context = str(client.calls[1]["messages"])
    assert "TECH_ONLY_BODY" in second_call_context
    assert "BILLING_ONLY_BODY" not in second_call_context


def test_skill_tool_schema_validates_array_arguments(tmp_path):
    manager = _skill_manager(tmp_path)
    agent = TechnicalAgent(FakeClient(), "test-model", skill_manager=manager)
    spec = agent.get_tools()["load_skills"]

    agent._validate_tool_input(
        spec,
        {"query": "401", "skill_names": ["技术支持处理规范"], "top_k": 1},
    )

    try:
        agent._validate_tool_input(spec, {"query": "401", "skill_names": "技术支持处理规范"})
    except ValueError as exc:
        assert "期望 array" in str(exc)
    else:
        raise AssertionError("skill_names must be an array")
