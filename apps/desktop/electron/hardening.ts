import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const DEFAULT_FETCH_TIMEOUT_MS = 15_000
const DATA_URL_READ_MAX_BYTES = 16 * 1024 * 1024
const TEXT_PREVIEW_SOURCE_MAX_BYTES = 64 * 1024 * 1024

const SAFE_ENV_SUFFIXES = new Set(['dist', 'example', 'sample', 'template'])
const SENSITIVE_EXTENSIONS = new Set(['.kdbx', '.p12', '.pem', '.pfx'])

function resolveTimeoutMs(timeoutMs, fallbackMs = DEFAULT_FETCH_TIMEOUT_MS) {
  const fallback =
    Number.isFinite(fallbackMs) && Number(fallbackMs) > 0 ? Math.round(Number(fallbackMs)) : DEFAULT_FETCH_TIMEOUT_MS

  const parsed = Number(timeoutMs)

  if (Number.isFinite(parsed) && parsed > 0) {
    return Math.round(parsed)
  }

  return fallback
}

function encryptDesktopSecret(value, safeStorageApi) {
  const raw = String(value || '')

  if (!raw) {
    return null
  }

  let encryptionAvailable = false

  try {
    encryptionAvailable = Boolean(safeStorageApi?.isEncryptionAvailable?.())
  } catch {
    encryptionAvailable = false
  }

  if (!encryptionAvailable) {
    throw new Error(
      '安全令牌存储不可用，RealityOS 桌面无法保存远程网关令牌。' +
        '请在环境变量中设置 HERMES_DESKTOP_REMOTE_URL 与 HERMES_DESKTOP_REMOTE_TOKEN，或开启系统钥匙串后重试。'
    )
  }

  try {
    return {
      encoding: 'safeStorage',
      value: safeStorageApi.encryptString(raw).toString('base64')
    }
  } catch (error) {
    const detail = error instanceof Error && error.message ? ` (${error.message})` : ''
    throw new Error(
      `加密远程网关令牌失败${detail}。` + '可改用环境变量 HERMES_DESKTOP_REMOTE_URL 与 HERMES_DESKTOP_REMOTE_TOKEN。'
    )
  }
}

function sensitiveFileBlockReason(filePath) {
  const normalized = String(filePath || '')
    .replace(/\\/g, '/')
    .toLowerCase()

  const basename = path.basename(normalized)
  const ext = path.extname(basename)

  if (!basename) {
    return null
  }

  if (normalized.includes('/.ssh/')) {
    return '已阻止 SSH 密钥/配置文件。'
  }

  if (normalized.includes('/.gnupg/')) {
    return '已阻止 GPG 密钥文件。'
  }

  if (normalized.endsWith('/.aws/credentials')) {
    return '已阻止 AWS 凭证文件。'
  }

  if (basename === '.env') {
    return '已阻止 .env 文件（通常包含密钥）。'
  }

  if (basename.startsWith('.env.')) {
    const suffix = basename.slice('.env.'.length)

    if (!SAFE_ENV_SUFFIXES.has(suffix)) {
      return `${basename} 已阻止（疑似包含环境密钥）。`
    }
  }

  if (/^id_(rsa|dsa|ecdsa|ed25519)(?:\..+)?$/.test(basename) && !basename.endsWith('.pub')) {
    return '已阻止 SSH 私钥文件。'
  }

  if (SENSITIVE_EXTENSIONS.has(ext)) {
    return `${ext} 密钥/证书文件已阻止。`
  }

  if (basename === '.npmrc' || basename === '.netrc' || basename === '.pypirc') {
    return `${basename} 已阻止（可能包含认证凭证）。`
  }

  return null
}

function ipcPathError(code: any, message: string): Error & { code: any } {
  const error = new Error(message) as Error & { code: any }

  ;(error as any).code = code

  return error
}

function rejectUnsafePathSyntax(filePath, purpose = '文件读取') {
  if (typeof filePath !== 'string') {
    throw ipcPathError('invalid-path', `${purpose}失败：缺少文件路径。`)
  }

  const raw = filePath.trim()

  if (!raw) {
    throw ipcPathError('invalid-path', `${purpose}失败：缺少文件路径。`)
  }

  if (raw.includes('\0')) {
    throw ipcPathError('invalid-path', `${purpose}失败：路径无效。`)
  }

  const normalized = raw.replace(/\\/g, '/').toLowerCase()

  if (
    normalized.startsWith('//?/') ||
    normalized.startsWith('//./') ||
    normalized.startsWith('globalroot/device/') ||
    normalized.includes('/globalroot/device/')
  ) {
    throw ipcPathError('device-path', `${purpose}已阻止：不允许 Windows 设备路径。`)
  }

  return raw
}

function resolveRequestedPathForIpc(filePath, options: { purpose?: string; baseDir?: fs.PathOrFileDescriptor } = {}) {
  const purpose = String(options.purpose || '文件读取')
  let raw = rejectUnsafePathSyntax(filePath, purpose)

  // Gateway-reported cwds (config `terminal.cwd`, remote sessions) routinely
  // arrive as `~/...`. Node's fs has no shell — without expansion the path
  // resolves under process.cwd() and every read "ENOENT"s forever.
  if (raw === '~' || raw.startsWith('~/') || raw.startsWith('~\\')) {
    raw = path.join(os.homedir(), raw.slice(1))
  }

  if (/^file:/i.test(raw)) {
    let resolvedPath

    try {
      const parsed = new URL(raw)

      if (parsed.protocol !== 'file:') {
        throw new Error('not a file URL')
      }

      resolvedPath = fileURLToPath(parsed)
    } catch {
      throw ipcPathError('invalid-path', `${purpose}失败：file URL 无效。`)
    }

    rejectUnsafePathSyntax(resolvedPath, purpose)

    return path.resolve(resolvedPath)
  }

  const baseInput = typeof options.baseDir === 'string' && options.baseDir.trim() ? options.baseDir : process.cwd()
  const safeBaseInput = rejectUnsafePathSyntax(baseInput, purpose)
  const resolvedBase = path.resolve(safeBaseInput)
  rejectUnsafePathSyntax(resolvedBase, purpose)
  const resolvedPath = path.resolve(resolvedBase, raw)
  rejectUnsafePathSyntax(resolvedPath, purpose)

  return resolvedPath
}

async function statForIpc(fsImpl: { promises: { stat: typeof fs.promises.stat } }, resolvedPath, purpose, typeLabel) {
  try {
    return await fsImpl.promises.stat(resolvedPath)
  } catch (error) {
    const code = error && typeof error === 'object' ? error.code : ''

    if (code === 'ENOENT' || code === 'ENOTDIR') {
      throw ipcPathError(code || 'ENOENT', `${purpose}失败：${typeLabel}不存在。`)
    }

    throw ipcPathError(
      code || 'read-error',
      `${purpose}失败：${error instanceof Error ? error.message : String(error)}`
    )
  }
}

async function realpathForIpc(fsImpl, resolvedPath, purpose) {
  if (typeof fsImpl.promises.realpath !== 'function') {
    return resolvedPath
  }

  try {
    const realPath = await fsImpl.promises.realpath(resolvedPath)
    rejectUnsafePathSyntax(realPath, purpose)

    return realPath
  } catch (error) {
    const code = error && typeof error === 'object' ? error.code : ''
    throw ipcPathError(
      code || 'read-error',
      `${purpose}失败：${error instanceof Error ? error.message : String(error)}`
    )
  }
}

function rejectSensitiveFilePath(filePath, purpose) {
  const blockReason = sensitiveFileBlockReason(filePath)

  if (blockReason) {
    throw ipcPathError('sensitive-file', `${purpose}已阻止（敏感文件）：${blockReason}`)
  }
}

async function resolveDirectoryForIpc(
  dirPath,
  options: {
    purpose?: string
    baseDir?: fs.PathOrFileDescriptor
    fs?: { promises: { stat: typeof fs.promises.stat } }
  } = {}
) {
  const purpose = String(options.purpose || '目录读取')
  const fsImpl = options.fs || fs
  const resolvedPath = resolveRequestedPathForIpc(dirPath, { baseDir: options.baseDir, purpose })
  const stat = await statForIpc(fsImpl, resolvedPath, purpose, '目录')

  if (!stat.isDirectory()) {
    throw ipcPathError('ENOTDIR', `${purpose}失败：路径不是目录。`)
  }

  const realPath = await realpathForIpc(fsImpl, resolvedPath, purpose)

  return { realPath, resolvedPath, stat }
}

async function resolveReadableFileForIpc(
  filePath,
  options: {
    purpose?: string
    baseDir?: fs.PathOrFileDescriptor
    fs?: typeof fs
    blockSensitive?: boolean
    maxBytes?: number
  } = {}
) {
  const purpose = String(options.purpose || '文件读取')
  const fsImpl = options.fs || fs
  const resolvedPath = resolveRequestedPathForIpc(filePath, { baseDir: options.baseDir, purpose })

  if (options.blockSensitive !== false) {
    rejectSensitiveFilePath(resolvedPath, purpose)
  }

  const stat = await statForIpc(fsImpl, resolvedPath, purpose, '文件')

  if (stat.isDirectory()) {
    throw ipcPathError('EISDIR', `${purpose}失败：路径指向目录。`)
  }

  if (!stat.isFile()) {
    throw ipcPathError('EINVAL', `${purpose}失败：仅支持普通文件。`)
  }

  const realPath = await realpathForIpc(fsImpl, resolvedPath, purpose)

  if (options.blockSensitive !== false) {
    rejectSensitiveFilePath(realPath, purpose)
  }

  const maxBytes = Number.isFinite(options.maxBytes) && Number(options.maxBytes) > 0 ? Number(options.maxBytes) : null

  if (maxBytes && stat.size > maxBytes) {
    throw ipcPathError('EFBIG', `${purpose}失败：文件过大（${stat.size} 字节，上限 ${maxBytes} 字节）。`)
  }

  try {
    await fsImpl.promises.access(resolvedPath, fs.constants.R_OK)
  } catch {
    throw ipcPathError('EACCES', `${purpose}失败：文件不可读。`)
  }

  return { realPath, resolvedPath, stat }
}

export {
  DATA_URL_READ_MAX_BYTES,
  DEFAULT_FETCH_TIMEOUT_MS,
  encryptDesktopSecret,
  rejectUnsafePathSyntax,
  resolveDirectoryForIpc,
  resolveReadableFileForIpc,
  resolveRequestedPathForIpc,
  resolveTimeoutMs,
  sensitiveFileBlockReason,
  TEXT_PREVIEW_SOURCE_MAX_BYTES
}
