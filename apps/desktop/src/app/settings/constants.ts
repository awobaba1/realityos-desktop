import {
  Box,
  Brain,
  type IconComponent,
  Lock,
  MessageCircle,
  Mic,
  Monitor,
  Moon,
  Palette,
  Sun,
  Wrench
} from '@/lib/icons'
import type { ThemeMode } from '@/themes/context'

import { defineFieldCopy } from './field-copy'
import type { DesktopConfigSection } from './types'

// Provider group definitions used to fold raw env-var names like
// ``XAI_API_KEY`` into a single "xAI" card with a friendly label, short
// description, and signup URL. Membership is determined by longest
// prefix match (see ``providerGroup`` in helpers.ts) so more specific
// prefixes (``MINIMAX_CN_``) correctly beat their general parents
// (``MINIMAX_``). New providers should be added here so they get their
// own card in Settings → Keys instead of being lumped into "Other".
interface ProviderPrefix {
  prefix: string
  name: string
  /** Optional one-line tagline shown beneath the group name. */
  description?: string
  /** Optional canonical signup/console URL surfaced from the card header. */
  docsUrl?: string
  /** Lower numbers float to the top of the providers list. */
  priority: number
}

export const EMPTY_SELECT_VALUE = '__hermes_empty__'
export const CONTROL_TEXT = 'text-xs'

export const PROVIDER_GROUPS: ProviderPrefix[] = [
  {
    prefix: 'NOUS_',
    name: 'Nous Portal',
    description: '托管的 Hermes 与 Nous 训练模型',
    docsUrl: 'https://portal.nousresearch.com',
    priority: 0
  },
  {
    prefix: 'OPENROUTER_',
    name: 'OpenRouter',
    description: '聚合数百个前沿模型',
    docsUrl: 'https://openrouter.ai/keys',
    priority: 1
  },
  {
    prefix: 'ANTHROPIC_',
    name: 'Anthropic',
    description: 'Claude API 访问（Sonnet、Opus、Haiku）',
    docsUrl: 'https://console.anthropic.com/settings/keys',
    priority: 2
  },
  {
    prefix: 'XAI_',
    name: 'xAI',
    description: 'Grok 模型（SuperGrok / Premium+ 请使用 OAuth）',
    docsUrl: 'https://console.x.ai/',
    priority: 3
  },
  {
    prefix: 'GOOGLE_',
    name: 'Gemini',
    description: 'Google AI Studio（Gemini 1.5 / 2.0 / 2.5）',
    docsUrl: 'https://aistudio.google.com/app/apikey',
    priority: 4
  },
  { prefix: 'GEMINI_', name: 'Gemini', priority: 4 },
  {
    prefix: 'DEEPSEEK_',
    name: 'DeepSeek',
    description: 'DeepSeek 官方 API（V3.x、R1）',
    docsUrl: 'https://platform.deepseek.com/api_keys',
    priority: 5
  },
  {
    prefix: 'DASHSCOPE_',
    name: 'DashScope (Qwen)',
    description: '阿里云 DashScope — Qwen 及多家厂商模型',
    docsUrl: 'https://modelstudio.console.alibabacloud.com/',
    priority: 6
  },
  { prefix: 'HERMES_QWEN_', name: 'DashScope (Qwen)', priority: 6 },
  {
    prefix: 'GLM_',
    name: 'GLM / Z.AI',
    description: '智谱 GLM-4.6 与 Z.AI 托管端点',
    docsUrl: 'https://z.ai/',
    priority: 7
  },
  { prefix: 'ZAI_', name: 'GLM / Z.AI', priority: 7 },
  { prefix: 'Z_AI_', name: 'GLM / Z.AI', priority: 7 },
  {
    prefix: 'KIMI_',
    name: 'Kimi / Moonshot',
    description: 'Moonshot Kimi K2 / 编程端点',
    docsUrl: 'https://platform.moonshot.cn/',
    priority: 8
  },
  {
    prefix: 'KIMI_CN_',
    name: 'Kimi (China)',
    description: 'Moonshot 国内端点',
    docsUrl: 'https://platform.moonshot.cn/',
    priority: 9
  },
  {
    prefix: 'MINIMAX_',
    name: 'MiniMax',
    description: 'MiniMax-M2 与海螺国际端点',
    docsUrl: 'https://www.minimax.io/',
    priority: 10
  },
  {
    prefix: 'MINIMAX_CN_',
    name: 'MiniMax (China)',
    description: 'MiniMax 中国大陆端点',
    docsUrl: 'https://www.minimaxi.com/',
    priority: 11
  },
  {
    prefix: 'HF_',
    name: 'Hugging Face',
    description: 'Inference Providers — 经 router.huggingface.co 提供 20+ 开源模型',
    docsUrl: 'https://huggingface.co/settings/tokens',
    priority: 12
  },
  {
    prefix: 'OPENCODE_ZEN_',
    name: 'OpenCode Zen',
    description: '按量付费访问精选编程模型',
    docsUrl: 'https://opencode.ai/auth',
    priority: 13
  },
  {
    prefix: 'OPENCODE_GO_',
    name: 'OpenCode Go',
    description: '$10/月订阅，覆盖开源编程模型',
    docsUrl: 'https://opencode.ai/auth',
    priority: 14
  },
  {
    prefix: 'NVIDIA_',
    name: 'NVIDIA NIM',
    description: 'build.nvidia.com 或自建的本地 NIM 端点',
    docsUrl: 'https://build.nvidia.com/',
    priority: 15
  },
  {
    prefix: 'OLLAMA_',
    name: 'Ollama Cloud',
    description: 'ollama.com 提供的云托管开源模型',
    docsUrl: 'https://ollama.com/settings',
    priority: 16
  },
  {
    prefix: 'LM_',
    name: 'LM Studio',
    description: '本地 LM Studio 服务器（OpenAI 兼容）',
    docsUrl: 'https://lmstudio.ai/docs/local-server',
    priority: 17
  },
  {
    prefix: 'STEPFUN_',
    name: 'StepFun',
    description: 'StepFun Step Plan 编程模型',
    docsUrl: 'https://platform.stepfun.com/',
    priority: 18
  },
  {
    prefix: 'XIAOMI_',
    name: 'Xiaomi MiMo',
    description: 'MiMo-V2.5 与小米自研模型',
    docsUrl: 'https://platform.xiaomimimo.com',
    priority: 19
  },
  {
    prefix: 'ARCEEAI_',
    name: 'Arcee AI',
    description: 'Arcee 托管的小型 + 中型模型',
    docsUrl: 'https://chat.arcee.ai/',
    priority: 20
  },
  { prefix: 'ARCEE_', name: 'Arcee AI', priority: 20 },
  {
    prefix: 'GMI_',
    name: 'GMI Cloud',
    description: 'GMI Cloud GPU + 模型托管',
    docsUrl: 'https://www.gmicloud.ai/',
    priority: 21
  },
  {
    prefix: 'AZURE_FOUNDRY_',
    name: 'Azure Foundry',
    description: 'Azure AI Foundry 自定义端点（兼容 OpenAI / Anthropic）',
    docsUrl: 'https://ai.azure.com/',
    priority: 22
  },
  {
    prefix: 'AWS_',
    name: 'AWS Bedrock',
    description: '通过 AWS profile + region 鉴权',
    docsUrl: 'https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-regions.html',
    priority: 23
  }
]

export const BUILTIN_PERSONALITIES = [
  'helpful',
  'concise',
  'technical',
  'creative',
  'teacher',
  'kawaii',
  'catgirl',
  'pirate',
  'shakespeare',
  'surfer',
  'noir',
  'uwu',
  'philosopher',
  'hype'
]

// Schema-side select overrides for desktop-relevant enum fields whose
// backend schema only declares a string type.
export const ENUM_OPTIONS: Record<string, string[]> = {
  'agent.image_input_mode': ['auto', 'native', 'text'],
  'approvals.mode': ['manual', 'smart', 'off'],
  'code_execution.mode': ['project', 'strict'],
  'context.engine': ['compressor', 'default', 'custom'],
  'delegation.reasoning_effort': ['', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  // RealityOS V6 (ADR-V6-010): ptg is the default memory provider — the
  // Personal Timeline Graph data brain. Listed first so the settings UI shows
  // the active V6 default as a selectable option.
  'memory.provider': ['ptg', '', 'builtin', 'hindsight', 'honcho'],
  // Terminal execution backends — kept in sync with the dispatch ladder in
  // tools/terminal_tool.py::_create_environment (local/docker/singularity/
  // modal/daytona/ssh). Remote backends need extra env (image, tokens, host).
  'terminal.backend': ['local', 'docker', 'singularity', 'modal', 'daytona', 'ssh'],
  'stt.elevenlabs.model_id': ['scribe_v2', 'scribe_v1'],
  'stt.local.model': ['tiny', 'base', 'small', 'medium', 'large-v3'],
  // Speech-to-text backends — kept in sync with the stt block in
  // hermes_cli/config.py (local/groq/openai/mistral/elevenlabs).
  'stt.provider': ['local', 'groq', 'openai', 'mistral', 'xai', 'elevenlabs'],
  'tts.openai.voice': ['alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer'],
  // Text-to-speech backends — kept in sync with the built-in source of truth
  // (agent/tts_registry.py::_BUILTIN_NAMES / tools/tts_tool.py::
  // BUILTIN_TTS_PROVIDERS). 'xai' is Grok TTS.
  'tts.provider': [
    'edge',
    'elevenlabs',
    'openai',
    'xai',
    'minimax',
    'mistral',
    'gemini',
    'neutts',
    'kittentts',
    'piper'
  ],
  'stt.openai.model': ['whisper-1', 'gpt-4o-mini-transcribe', 'gpt-4o-transcribe'],
  'stt.mistral.model': ['voxtral-mini-latest', 'voxtral-mini-2602'],
  'tts.openai.model': ['gpt-4o-mini-tts', 'tts-1', 'tts-1-hd'],
  'tts.elevenlabs.model_id': ['eleven_multilingual_v2', 'eleven_turbo_v2_5', 'eleven_flash_v2_5'],
  // NeuTTS local inference device.
  'tts.neutts.device': ['cpu', 'cuda', 'mps'],
  'updates.non_interactive_local_changes': ['stash', 'discard']
}

export const FIELD_LABELS: Record<string, string> = defineFieldCopy({
  model: '默认模型',
  modelContextLength: '上下文窗口',
  fallbackProviders: '备用模型',
  toolsets: '启用的工具集',
  timezone: '时区',
  display: {
    personality: '人格',
    showReasoning: '推理区块'
  },
  agent: {
    maxTurns: 'Agent 最大步数',
    imageInputMode: '图片附件',
    apiMaxRetries: 'API 重试次数',
    serviceTier: '服务等级',
    toolUseEnforcement: '工具调用强制策略'
  },
  terminal: {
    cwd: '工作目录',
    backend: '执行后端',
    timeout: '命令超时',
    persistentShell: '持久化 Shell',
    envPassthrough: '环境变量透传',
    dockerImage: 'Docker 镜像',
    singularityImage: 'Singularity 镜像',
    modalImage: 'Modal 镜像',
    daytonaImage: 'Daytona 镜像'
  },
  fileReadMaxChars: '文件读取上限',
  toolOutput: {
    maxBytes: '终端输出上限',
    maxLines: '文件分页上限',
    maxLineLength: '行宽上限'
  },
  codeExecution: {
    mode: '代码执行模式'
  },
  approvals: {
    mode: '审批模式',
    timeout: '审批超时',
    mcpReloadConfirm: '确认 MCP 重载'
  },
  commandAllowlist: '命令白名单',
  security: {
    redactSecrets: '隐藏密钥',
    allowPrivateUrls: '允许私有 URL'
  },
  browser: {
    allowPrivateUrls: '浏览器私有 URL',
    autoLocalForPrivateUrls: '私有 URL 使用本地浏览器'
  },
  checkpoints: {
    enabled: '文件检查点',
    maxSnapshots: '检查点数量上限'
  },
  voice: {
    recordKey: '语音快捷键',
    maxRecordingSeconds: '最长录音时长',
    autoTts: '朗读回复'
  },
  stt: {
    enabled: '语音转文字',
    echoTranscripts: '回显转写结果',
    provider: '语音转文字提供商',
    local: {
      model: '本地转写模型',
      language: '转写语言'
    },
    openai: {
      model: 'OpenAI STT 模型'
    },
    groq: {
      model: 'Groq STT 模型'
    },
    mistral: {
      model: 'Mistral STT 模型'
    },
    elevenlabs: {
      modelId: 'ElevenLabs STT 模型',
      languageCode: 'ElevenLabs 语言',
      tagAudioEvents: '标记音频事件',
      diarize: '说话人分离'
    }
  },
  tts: {
    provider: '文字转语音提供商',
    edge: {
      voice: 'Edge 嗓音'
    },
    openai: {
      model: 'OpenAI TTS 模型',
      voice: 'OpenAI 嗓音'
    },
    elevenlabs: {
      voiceId: 'ElevenLabs 嗓音',
      modelId: 'ElevenLabs 模型'
    },
    xai: {
      voiceId: 'xAI（Grok）嗓音',
      language: 'xAI 语言'
    },
    minimax: {
      model: 'MiniMax TTS 模型',
      voiceId: 'MiniMax 嗓音'
    },
    mistral: {
      model: 'Mistral TTS 模型',
      voiceId: 'Mistral 嗓音'
    },
    gemini: {
      model: 'Gemini TTS 模型',
      voice: 'Gemini 嗓音'
    },
    neutts: {
      model: 'NeuTTS 模型',
      device: 'NeuTTS 设备'
    },
    kittentts: {
      model: 'KittenTTS 模型',
      voice: 'KittenTTS 嗓音'
    },
    piper: {
      voice: 'Piper 嗓音'
    }
  },
  memory: {
    memoryEnabled: '持久化记忆',
    userProfileEnabled: '用户画像',
    memoryCharLimit: '记忆预算',
    userCharLimit: '画像预算',
    provider: '记忆提供商'
  },
  context: {
    engine: '上下文引擎'
  },
  compression: {
    enabled: '自动压缩',
    threshold: '压缩阈值',
    targetRatio: '压缩目标比',
    protectLastN: '受保护的最近消息数'
  },
  delegation: {
    model: '子 Agent 模型',
    provider: '子 Agent 提供商',
    maxIterations: '子 Agent 轮次上限',
    maxConcurrentChildren: '并行子 Agent 数',
    childTimeoutSeconds: '子 Agent 超时',
    reasoningEffort: '子 Agent 推理强度'
  },
  updates: {
    nonInteractiveLocalChanges: '应用内更新时的本地改动'
  }
})

export const FIELD_DESCRIPTIONS: Record<string, string> = defineFieldCopy({
  model: '用于新建对话，除非你在输入框另选其他模型。',
  modelContextLength: '留为 0 表示使用所选模型自动探测到的上下文窗口。',
  fallbackProviders: '默认模型失败时依次尝试的 provider:model 备用条目。',
  display: {
    personality: '新会话的默认助手风格。',
    showReasoning: '后端提供推理区块时显示。'
  },
  timezone: 'Hermes 需要本地时间上下文时使用。留空则使用系统时区。',
  agent: {
    imageInputMode: '控制图片附件发送给模型的方式。',
    maxTurns: 'Hermes 在停止一次运行前进行工具调用轮次的上限。'
  },
  terminal: {
    cwd: '工具与终端操作的默认项目目录。',
    persistentShell: '后端支持时在命令之间保留 shell 状态。',
    envPassthrough: '透传到工具执行环境的环境变量。',
    dockerImage: '执行后端为 Docker 时使用的容器镜像。',
    singularityImage: '执行后端为 Singularity 时使用的镜像。',
    modalImage: '执行后端为 Modal 时使用的镜像。',
    daytonaImage: '执行后端为 Daytona 时使用的镜像。'
  },
  codeExecution: {
    mode: '代码执行限定在当前项目内的严格程度。'
  },
  fileReadMaxChars: 'Hermes 单次文件读取的最大字符数。',
  approvals: {
    mode: 'Hermes 如何处理需要明确批准的命令。',
    timeout: '审批提示在超时前的等待时长。'
  },
  security: {
    redactSecrets: '尽可能从模型可见内容中隐藏检测到的密钥。'
  },
  checkpoints: {
    enabled: '在文件编辑前创建可回滚快照。'
  },
  memory: {
    memoryEnabled: '保存可辅助后续会话的持久记忆。',
    userProfileEnabled: '维护一份精简的用户偏好画像。'
  },
  context: {
    engine: '在接近上下文上限时管理长对话的策略。'
  },
  compression: {
    enabled: '对话变长时自动总结较早的上下文。'
  },
  voice: {
    autoTts: '自动朗读助手回复。'
  },
  tts: {
    xai: {
      voiceId: 'xAI 嗓音 ID（如 eve）或自定义嗓音 ID。',
      language: '口语语言代码，如 en。'
    },
    neutts: {
      device: 'NeuTTS 本地推理设备。'
    }
  },
  stt: {
    enabled: '启用本地或提供商语音转写。',
    echoTranscripts: '将语音消息的原始 🎙️ 转写文本发回对话。',
    elevenlabs: {
      languageCode: '可选 ISO-639-3 语言代码。留空由 ElevenLabs 自动检测。'
    }
  },
  updates: {
    nonInteractiveLocalChanges:
      'Hermes 从应用内自更新时（无终端提示），保留本地源码改动（stash）还是丢弃（discard）。终端更新始终会询问。'
  }
})

// Curated desktop config surface: only fields a user might tune from the app.
export const SECTIONS: DesktopConfigSection[] = [
  {
    id: 'model',
    label: '模型',
    icon: Box,
    keys: ['model_context_length', 'fallback_providers']
  },
  {
    id: 'chat',
    label: '聊天',
    icon: MessageCircle,
    keys: ['display.personality', 'timezone', 'display.show_reasoning', 'agent.image_input_mode']
  },
  {
    id: 'appearance',
    label: '外观',
    icon: Palette,
    keys: []
  },
  {
    id: 'workspace',
    label: '工作区',
    icon: Monitor,
    keys: [
      'terminal.cwd',
      'code_execution.mode',
      'terminal.persistent_shell',
      'terminal.env_passthrough',
      'file_read_max_chars'
    ]
  },
  {
    id: 'safety',
    label: '安全',
    icon: Lock,
    keys: [
      'approvals.mode',
      'approvals.timeout',
      'approvals.mcp_reload_confirm',
      'command_allowlist',
      'security.redact_secrets',
      'security.allow_private_urls',
      'browser.allow_private_urls',
      'browser.auto_local_for_private_urls',
      'checkpoints.enabled'
    ]
  },
  {
    id: 'memory',
    label: '记忆与上下文',
    icon: Brain,
    keys: [
      'memory.memory_enabled',
      'memory.user_profile_enabled',
      'memory.memory_char_limit',
      'memory.user_char_limit',
      'memory.provider',
      'context.engine',
      'compression.enabled',
      'compression.threshold',
      'compression.target_ratio',
      'compression.protect_last_n'
    ]
  },
  {
    id: 'voice',
    label: '语音',
    icon: Mic,
    keys: [
      'tts.provider',
      'stt.enabled',
      'stt.echo_transcripts',
      'stt.provider',
      'voice.auto_tts',
      'tts.edge.voice',
      'tts.openai.model',
      'tts.openai.voice',
      'tts.elevenlabs.voice_id',
      'tts.elevenlabs.model_id',
      'tts.xai.voice_id',
      'tts.xai.language',
      'tts.minimax.model',
      'tts.minimax.voice_id',
      'tts.mistral.model',
      'tts.mistral.voice_id',
      'tts.gemini.model',
      'tts.gemini.voice',
      'tts.neutts.model',
      'tts.neutts.device',
      'tts.kittentts.model',
      'tts.kittentts.voice',
      'tts.piper.voice',
      'stt.local.model',
      'stt.local.language',
      'stt.openai.model',
      'stt.groq.model',
      'stt.mistral.model',
      'stt.elevenlabs.model_id',
      'stt.elevenlabs.language_code',
      'stt.elevenlabs.tag_audio_events',
      'stt.elevenlabs.diarize',
      'voice.record_key',
      'voice.max_recording_seconds'
    ]
  },
  {
    id: 'advanced',
    label: '高级',
    icon: Wrench,
    keys: [
      'toolsets',
      'terminal.backend',
      'terminal.timeout',
      'terminal.docker_image',
      'terminal.singularity_image',
      'terminal.modal_image',
      'terminal.daytona_image',
      'tool_output.max_bytes',
      'tool_output.max_lines',
      'tool_output.max_line_length',
      'checkpoints.max_snapshots',
      'agent.max_turns',
      'agent.api_max_retries',
      'agent.service_tier',
      'agent.tool_use_enforcement',
      'delegation.model',
      'delegation.provider',
      'delegation.max_iterations',
      'delegation.max_concurrent_children',
      'delegation.child_timeout_seconds',
      'delegation.reasoning_effort',
      'updates.non_interactive_local_changes'
    ]
  }
]

export interface ModeOption {
  id: ThemeMode
  label: string
  icon: IconComponent
}

export const MODE_OPTIONS: ModeOption[] = [
  { id: 'light', label: '浅色', icon: Sun },
  { id: 'dark', label: '深色', icon: Moon },
  { id: 'system', label: '跟随系统', icon: Monitor }
]
