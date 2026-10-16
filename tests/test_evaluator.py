import asyncio
from types import SimpleNamespace

from agents.agent_orchestrator import (
    AgentOrchestrator,
    AgentResponse,
    AgentType,
    Request,
    RoutingDecision,
)
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from evaluation.evaluator import LLMJudge


class JudgeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                text = self.outputs.pop(0)
                return SimpleNamespace(content=[{"type": "text", "text": text}])

        self.messages = Messages()


def test_judge_retries_and_parses_json_inside_explanatory_text():
    client = JudgeClient([
        "这次输出不符合要求",
        '评分如下：```json\n{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.6}\n```',
    ])
    judge = LLMJudge(client, "test-model")

    scores = asyncio.run(judge.judge("如何退款？", "可以在购买后 7 天内申请。"))

    assert scores.judge_failed is False
    assert scores.relevance == 0.9
    assert scores.helpfulness == 0.6
    assert len(client.calls) == 2
    assert client.calls[0]["max_tokens"] == 1024
    assert client.calls[1]["max_tokens"] == 4096
    assert client.calls[0]["extra_body"] == {"reasoning": {"effort": "none"}}


def test_order_id_entity_accepts_natural_chinese_connector():
    recognizer = IntentRecognizer(api_key="test")

    assert recognizer._extract_entities("订单号是ABC123")["order_id"] == ["ABC123"]
    assert recognizer._extract_entities("订单号为XYZ-789")["order_id"] == ["XYZ-789"]


def test_orchestrator_writes_recognized_entities_back_to_request():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)

    class Recognizer:
        async def recognize(self, message, history=None):
            return SimpleNamespace(
                intent=IntentCategory.REFUND,
                intent_group="billing",
                urgency=UrgencyLevel.LOW,
                confidence=0.91,
                entities={"order_id": ["ABC123"]},
            )

    orchestrator._intent_recognizer = Recognizer()
    orchestrator._recent_tool_traces = []
    orchestrator._route_decision = lambda req: RoutingDecision(
        primary_agent=AgentType.BILLING,
        reason="test",
        confidence=0.9,
    )

    async def execute(req, agent_type):
        assert req.entities == {"order_id": ["ABC123"]}
        return AgentResponse(
            agent_type=agent_type,
            content="ok",
            success=True,
        )

    orchestrator._execute = execute
    orchestrator._record_tool_trace = lambda result: None
    request = Request(message="订单号是ABC123", user_id="u1", conv_id="c1")

    result = asyncio.run(orchestrator.run(request))

    assert result.intent == IntentCategory.REFUND
    assert request.entities == {"order_id": ["ABC123"]}
