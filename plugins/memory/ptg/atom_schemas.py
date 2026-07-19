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


class R8CognitionAtom(BaseModel):
    """R8 Cognition — 认知/学习原子（Phase 1b，ADR-V6-016）。

    delta（架构 §4.2）：knowledge_density / question_count /
    learning_engagement / weakness_flag。Phase 1b 文字子集——knowledge_density
    连续值无来源，用 ``knowledge_tags`` 离散标签 + ``engagement`` 近似；连续值
    留 Phase 2 统计。
    """

    type: str = "R8_Cognition"
    topic: str = Field(max_length=200, description="学习/思考主题（学到的知识、思考的问题）")
    knowledge_tags: list[str] = Field(
        default_factory=list, max_length=8,
        description="知识点标签（如：React Hooks、k8s 调度、认知失调）")
    engagement: str = Field(pattern="^(high|medium|low)$",
                            description="投入程度：主动深究=high / 一般了解=medium / 顺带提及=low")
    is_question: bool = Field(
        default=False, description="是否是一个待解答的疑问（true）vs 已获得的知识（false）")
    confidence: float = Field(ge=0, le=1)


class R9EmotionAtom(BaseModel):
    """R9 Emotion — 共现情绪原子（Phase 1b，ADR-V6-016）。

    与 R1 自状态的差别：R1 是用户的「常态」压力/疲劳/精力；R9 是「在某个语境下
    表现出的情绪波动」（带 trigger）。delta（架构 §4.2）：emotion_score /
    shift_vs_baseline / valence / arousal / causal_confidence。Phase 1b 共现版——
    causal_confidence 固定 0.65 + co_occurrence_only=true（架构 §4.3D L361），
    valence/arousal 文字推断，声学连续值留 Phase 2.5。
    """

    type: str = "R9_Emotion"
    emotion_label: str = Field(max_length=50, description="情绪标签（如：开心、焦虑、愤怒、感动）")
    valence: str = Field(pattern="^(positive|negative|neutral)$",
                         description="效价：正向/负向/中性")
    arousal: str = Field(pattern="^(high|low)$",
                         description="唤醒度：激烈=high / 平静=low")
    trigger: str | None = Field(None, max_length=200, description="触发该情绪的具体因素/事件")
    intensity: str = Field(pattern="^(high|medium|low)$", description="情绪强度")
    confidence: float = Field(ge=0, le=1)


class R12OutcomeAtom(BaseModel):
    """R12 Outcome — 任务结果原子（Phase 1b，ADR-V6-016）。

    delta（架构 §4.2）：completion_flag / failure_flag / delay_flag /
    resolution_status，编码进 ``outcome`` + ``resolution_note``。来源：文字完成
    语气（搞定了/没成/拖到下周）+ 工具执行结果（post_tool_call 闭环，后续子项接）。
    """

    type: str = "R12_Outcome"
    task_ref: str = Field(max_length=200, description="关联的任务/待办描述")
    outcome: str = Field(pattern="^(completed|failed|delayed)$",
                         description="结果：完成 completed / 失败 failed / 延期 delayed")
    resolution_note: str | None = Field(None, max_length=300, description="结果备注（如何完成/为何失败）")
    confidence: float = Field(ge=0, le=1)


# Union of all atom types
AtomType = Union[
    R0EntityAtom, R3PersonAtom, R2TaskAtom, R7ExpressionAtom, R1SelfStateAtom,
    R8CognitionAtom, R9EmotionAtom, R12OutcomeAtom,
]


class ExtractionResult(BaseModel):
    """LLM 提取结果 — 严格对应 hl12_extract prompt 输出格式（v11: 5 类，v12: 8 类）。"""

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
    # Phase 1b (ADR-V6-016): the three atoms the v11 baseline lacked.
    "R8_Cognition": R8CognitionAtom,
    "R9_Emotion": R9EmotionAtom,
    "R12_Outcome": R12OutcomeAtom,
}
