import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

// Process-local capability store: a restart revokes every issued key.
export class PortkeyStore {
  private readonly secret = randomBytes(32);
  private readonly issued = new Map<string, { principal: string; missionId: string; expiresAt: number }>();
  private readonly now: () => number;
  constructor(now: () => number = Date.now) { this.now = now; }
  issue(principal: string, missionId: string, ttlMs = 300000) {
    this.prune();
    if (this.issued.size >= 1000 || ttlMs <= 0 || ttlMs > 300000) throw new Error("移動キー発行上限です。");
    const id = randomBytes(24).toString("base64url");
    const expiresAt = this.now() + ttlMs;
    this.issued.set(id, { principal, missionId, expiresAt });
    return { token: `${id}.${this.sign(id)}`, expiresAt: new Date(expiresAt).toISOString() };
  }
  consume(token: string, principal: string): string {
    this.prune();
    const [id, sig, extra] = token.split(".");
    if (!id || !sig || extra || !/^[A-Za-z0-9_-]{32}$/.test(id)) throw new Error("無効な移動キーです。");
    const expected = Buffer.from(this.sign(id));
    const actual = Buffer.from(sig);
    if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) throw new Error("改ざんされた移動キーです。");
    const entry = this.issued.get(id);
    if (!entry || entry.principal !== principal || entry.expiresAt <= this.now()) throw new Error("期限切れ・使用済み・権限外の移動キーです。");
    this.issued.delete(id);
    return entry.missionId;
  }
  revokeMission(missionId: string) {
    for (const [id, value] of this.issued) if (value.missionId === missionId) this.issued.delete(id);
  }
  private sign(value: string) { return createHmac("sha256", this.secret).update(`mission-jump:${value}`).digest("base64url"); }
  private prune() { for (const [id, value] of this.issued) if (value.expiresAt <= this.now()) this.issued.delete(id); }
}
