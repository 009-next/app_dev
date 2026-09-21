import { isAbsolute, relative, resolve, sep } from "node:path";

export function projectRoot(): string {
  return resolve(process.cwd());
}

function configuredRoot(name: "CONNECT_FORCE_VAULT_ROOT" | "CONNECT_FORCE_CARD_DRAFT_ROOT" | "CONNECT_FORCE_MISSION_DRAFT_ROOT", fallback: string): string {
  const value = process.env[name]?.trim();
  // Deployment configuration is trusted at startup; request parameters never choose filesystem roots.
  if (!value) return fallback;
  if (!isAbsolute(value)) throw new Error(`${name}は絶対パスで指定してください。`);
  return resolve(value);
}

export function vaultRoot(): string {
  return configuredRoot("CONNECT_FORCE_VAULT_ROOT", resolve(projectRoot(), "your_folder", "vault"));
}

export function cardDraftRoot(): string {
  return configuredRoot("CONNECT_FORCE_CARD_DRAFT_ROOT", resolve(projectRoot(), "data", "card-drafts"));
}

export function missionDraftRoot(): string {
  return configuredRoot("CONNECT_FORCE_MISSION_DRAFT_ROOT", resolve(projectRoot(), "data", "mission-drafts"));
}

export function resolveInside(root: string, requestedPath: string): string {
  if (!requestedPath || requestedPath.includes("\0") || isAbsolute(requestedPath)) {
    throw new Error("相対パスを指定してください。");
  }

  const base = resolve(root);
  const candidate = resolve(base, requestedPath);
  const rel = relative(base, candidate);
  if (rel === "" || rel === ".") return candidate;
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error("許可されたディレクトリの外は参照できません。");
  }
  return candidate;
}

export function resolveVaultMarkdown(requestedPath: string): string {
  const target = resolveInside(vaultRoot(), requestedPath);
  if (!target.toLowerCase().endsWith(".md")) {
    throw new Error("VaultではMarkdownファイルだけを参照できます。");
  }
  return target;
}

export function safeOperationId(value: string): string {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{7,63}$/.test(value)) {
    throw new Error("operationIdは8〜64文字の英数字・ハイフン・アンダースコアにしてください。");
  }
  return value;
}
