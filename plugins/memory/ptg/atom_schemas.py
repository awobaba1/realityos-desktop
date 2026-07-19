"""RealityOS V6 — HL-12 extraction atom schemas (C5 gate).

Verbatim port of V5 ``danao13/backend/app/schemas/extraction_schemas.py`` — the
11-version-iterated prompt's output contract. These pydantic models ARE the
schema-validation gate (C5: agent output must pass schema validation before
write): an atom that fails one of the ``pattern`` / ``ge`` / ``le`` constraints
is routed to the DLQ, never silently written (see ``confidence.py``).

Kept byte-for-byte faithful to V5 (field names, enums, confidence ``ge=0 le=1``)
so the v11 prompt's R1 84.8 / R3 85.2 / R2 89.2 extraction baseline transfers
intact. The only deliberate change vs the V5 source is dropping
``ExtractionResponse`` (a V5 HTTP-API wrapper unused in the desktop agent).
"""

from __future__ import annotations

from typing import Union

from pydantic import BaseModel, Field

# ── LLM Output Atom Types ──


class R3PersonAtom(BaseModel):
    """R3 Person — 人物关系原子"""

    type: str = "R3_Person"
    person_name: str = Field(max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=5,
                               description="ADR-044: 同一人物的其他称呼（昵称/职称等）")
    mention_context: str | None = None
    sentiment: str | None = Field(None, pattern="^(positive|neutral|negative)$")
    interaction_type: str | None = Field(None, pattern="^(meeting|communication|conflict|casual)$")
    confidence: float = Field(ge=0, le=1)
    # ADR-045: 场景分割 — 同一时间/地点/事件中的人同属一个 segment
    segment_id: int = Field(default=0, ge=0, description="场景分组 ID：同场景的人共享同一 ID")
    segment_label: str | None = Field(None, max_length=50, description="场景标签（如：上午会议、中午吃饭）")


class R2TaskAtom(BaseModel):
    """R2 Task — 待办事项原子"""

    type: str = "R2_Task"
    task_description: str
    urgency: str | None = Field(None, pattern="^(high|medium|low)$")
    deadline: str | None = None  # ISO 8601 or null
    confidence: float = Field(ge=0, le=1)


class R7ExpressionAtom(BaseModel):
    """R7 Expression — 意图表达原子"""

    type: str = "R7_Expression"
    intent_class: str = Field(
        pattern="^(Need_To_Do|Complaint|Health|Help|Evaluation|Conflict|Consumption|Other)$")
    content_summary: str
    confidence: float = Field(ge=0, le=1)


class R1SelfStateAtom(BaseModel):
    """R1 SelfState — 情绪状态原子"""

    type: str = "R1_SelfState"
    state_type: str = Field(pattern="^(stress|fatigue|energy|mood)$")
    direction: str = Field(pattern="^(up|down|stable)$")
    intensity: str = Field(pattern="^(high|medium|low)$")
    evidence: str | None = None
    confidence: float = Field(ge=0, le=1)


class R0EntityAtom(BaseModel):
    """R0 Entity — 专有名词原子（地名、公司/组织、专业术语）— ADR-048"""

    type: str = "R0_Entity"
    entity_name: str = Field(max_length=200, description="标准/官方完整名称")
    entity_category: str = Field(
        pattern="^(place|organization|term)$",
        description="place=地名, organization=公司/组织, term=专业术语",
    )
    aliases: list[str] = Field(
        default_factory=list, max_length=5,
        description="其他称呼/简称（如：字节=字节跳动, k8s=Kubernetes）",
    )
    mention_context: str | None = Field(None, description="提及场景")
    confidence: float = Field(ge=0, le=1)


# Union of all atom types
AtomType = Union[R0EntityAtom, R3PersonAtom, R2TaskAtom, R7ExpressionAtom, R1SelfStateAtom]


class ExtractionResult(BaseModel):
    """LLM 提取结果 — 严格对应 hl12_extract_v11 输出格式。"""

    summary: str = Field(max_length=50)
    atoms: list[AtomType] = Field(default_factory=list)


# type-string → model dispatch (the V6 granular gate uses this so ONE malformed
# atom no longer sinks its siblings — see confidence.validate_and_filter). V5
# relied on pydantic's discriminated-Union parse which rejects the whole list on
# a single bad atom; V6 validates each atom against its own model instead.
ATOM_MODELS = {
    "R3_Person": R3PersonAtom,
    "R2_Task": R2TaskAtom,
    "R7_Expression": R7ExpressionAtom,
    "R1_SelfState": R1SelfStateAtom,
    "R0_Entity": R0EntityAtom,
}
