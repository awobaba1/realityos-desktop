import { RowButton } from '@/components/ui/row-button'
import { translateNow, useI18n } from '@/i18n'
import { Check, ChevronRight, Terminal } from '@/lib/icons'
import type { OAuthProvider } from '@/types/hermes'

// 展示顺序仅此一处；标题走 i18n catalog（onboarding.providerTitles），
// 这样测试世界（DEFAULT_LOCALE=en）渲染英文、生产（zh）渲染中文（ADR-V6-077）。
// 纯品牌名（Nous Portal / MiniMax / …）各语言一致，真正本地化的是方法后缀
//（OAuth / API Key）。providerTitle 经 runtimeLocale 解析，调用方签名不变。
const PROVIDER_ORDER: Record<string, number> = {
  nous: 0,
  'openai-codex': 1,
  'minimax-oauth': 2,
  'qwen-oauth': 3,
  'xai-oauth': 4,
  // 两条 Anthropic 入口排在最后：先是 API 密钥方式，再是
  // 订阅授权方式（仅在有额外用量额度时可用）。
  anthropic: 5,
  'claude-code': 6
}

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

export const providerTitle = (p: OAuthProvider): string => {
  const key = `onboarding.providerTitles.${p.id}`
  const localized = translateNow(key)

  // translateNow 在无法解析时原样返回 key；此时回落到 provider 自报名称。
  return localized === key ? p.name : localized
}

const orderOf = (p: OAuthProvider) => PROVIDER_ORDER[p.id] ?? 99

export const sortProviders = (providers: OAuthProvider[]) =>
  [...providers].sort((a, b) => orderOf(a) - orderOf(b) || a.name.localeCompare(b.name))

export function FeaturedProviderRow({
  onSelect,
  provider
}: {
  onSelect: (provider: OAuthProvider) => void
  provider: OAuthProvider
}) {
  const { t } = useI18n()
  const loggedIn = provider.status?.logged_in

  return (
    <button
      className="group relative flex w-full items-center justify-between gap-4 rounded-[8px] bg-primary/[0.06] px-3 py-2.5 text-left transition-colors hover:bg-primary/10"
      onClick={() => onSelect(provider)}
      type="button"
    >
      <span aria-hidden className="arc-border arc-reverse arc-nous" />
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <img alt="" className="size-5 shrink-0 rounded" src={assetPath('apple-touch-icon.png')} />
          <span className="text-[length:var(--conversation-text-font-size)] font-semibold">
            {providerTitle(provider)}
          </span>
          {loggedIn ? (
            <ConnectedTag />
          ) : (
            <span className="inline-flex items-center gap-1.5 bg-primary px-2 py-0.5 text-[0.64rem] font-semibold uppercase tracking-[0.16em] text-primary-foreground">
              <span aria-hidden="true" className="dither inline-block size-2 shrink-0" />
              {t.onboarding.recommended}
            </span>
          )}
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{t.onboarding.featuredPitch}</p>
      </div>
      <ChevronRight className="size-4 shrink-0 text-primary transition group-hover:translate-x-0.5" />
    </button>
  )
}

function ConnectedTag() {
  const { t } = useI18n()

  return (
    <span className="inline-flex items-center gap-1 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
      <Check className="size-3" />
      {t.onboarding.connected}
    </span>
  )
}

const PROVIDER_ROW_CLASS =
  'group flex w-full items-center justify-between gap-3 rounded-[6px] px-3 py-2.5 text-left transition-colors hover:bg-(--ui-control-hover-background)'

export function KeyProviderRow({ onClick }: { onClick: () => void }) {
  const { t } = useI18n()

  return (
    <RowButton className={PROVIDER_ROW_CLASS} onClick={onClick}>
      <div className="min-w-0">
        <span className="text-[length:var(--conversation-text-font-size)] font-semibold">OpenRouter</span>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{t.onboarding.openRouterPitch}</p>
      </div>
      <ChevronRight className="size-4 text-muted-foreground transition group-hover:text-foreground" />
    </RowButton>
  )
}

export function ProviderRow({
  onSelect,
  provider
}: {
  onSelect: (provider: OAuthProvider) => void
  provider: OAuthProvider
}) {
  const { t } = useI18n()
  const loggedIn = provider.status?.logged_in
  const Trail = provider.flow === 'external' ? Terminal : ChevronRight

  return (
    <RowButton className={PROVIDER_ROW_CLASS} onClick={() => onSelect(provider)}>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-[length:var(--conversation-text-font-size)] font-semibold">
            {providerTitle(provider)}
          </span>
          {loggedIn ? <ConnectedTag /> : null}
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{t.onboarding.flowSubtitles[provider.flow]}</p>
      </div>
      <Trail className="size-4 text-muted-foreground transition group-hover:text-foreground" />
    </RowButton>
  )
}
