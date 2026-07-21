import { Button } from '@/components/ui/button'
import { ExternalLink } from '@/lib/external-link'
import { PawPrint, Settings2 } from '@/lib/icons'

interface GenerateUnavailableProps {
  onSetup: () => void
}

// Shown when no reference-capable image backend is configured: generation is
// impossible, so we replace the prompt entirely with a friendly path to set one
// up (in-app) plus where to grab a key.
export function GenerateUnavailable({ onSetup }: GenerateUnavailableProps) {
  return (
    <div className="flex flex-col items-center gap-4 text-center">
      <span className="grid size-11 place-items-center rounded-full bg-primary/10 text-primary">
        <PawPrint className="size-5" />
      </span>
      <div className="space-y-1.5">
        <p className="text-[length:var(--conversation-text-font-size)] font-semibold">
          添加图像后端以生成
        </p>
        <p className="mx-auto max-w-[19rem] text-[length:var(--conversation-caption-font-size)] leading-relaxed text-(--ui-text-tertiary)">
          孵化自定义宠物需要一个能基于参考图像生成的服务商。
        </p>
      </div>
      <Button onClick={onSetup} size="sm">
        <Settings2 className="size-4" />
        设置图像生成
      </Button>
      <p className="flex flex-wrap items-center justify-center gap-x-1.5 text-[0.6875rem] text-(--ui-text-tertiary)">
        <span>从以下获取密钥：</span>
        <ExternalLink href="https://portal.nousresearch.com" showExternalIcon={false}>
          Nous Portal
        </ExternalLink>
        <span>·</span>
        <ExternalLink
          className="opacity-40 transition-opacity hover:opacity-100"
          href="https://openrouter.ai/keys"
          showExternalIcon={false}
        >
          OpenRouter
        </ExternalLink>
        <span>·</span>
        <ExternalLink
          className="opacity-40 transition-opacity hover:opacity-100"
          href="https://platform.openai.com/api-keys"
          showExternalIcon={false}
        >
          OpenAI
        </ExternalLink>
      </p>
    </div>
  )
}
