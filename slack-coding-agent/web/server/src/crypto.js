// Token encryption at rest (AES-256-GCM) with a key kept in a 0600 file.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export function loadOrCreateKey(dataDir) {
  if (process.env.WEB_ENCRYPTION_KEY) {
    const key = Buffer.from(process.env.WEB_ENCRYPTION_KEY, 'base64');
    if (key.length !== 32) throw new Error('WEB_ENCRYPTION_KEY must be 32 bytes, base64-encoded');
    return key;
  }
  const file = path.join(dataDir, 'web_secret.key');
  if (fs.existsSync(file)) return Buffer.from(fs.readFileSync(file, 'utf8').trim(), 'base64');
  fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  const key = crypto.randomBytes(32);
  fs.writeFileSync(file, key.toString('base64'), { mode: 0o600, flag: 'wx' });
  return key;
}

export function encrypt(key, obj) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const data = Buffer.concat([cipher.update(JSON.stringify(obj), 'utf8'), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), data]).toString('base64');
}

export function decrypt(key, text) {
  try {
    const buf = Buffer.from(text, 'base64');
    const decipher = crypto.createDecipheriv('aes-256-gcm', key, buf.subarray(0, 12));
    decipher.setAuthTag(buf.subarray(12, 28));
    return JSON.parse(Buffer.concat([decipher.update(buf.subarray(28)), decipher.final()]).toString('utf8'));
  } catch {
    return null;
  }
}

export const randomToken = (bytes = 32) => crypto.randomBytes(bytes).toString('base64url');
export const sha256 = (s) => crypto.createHash('sha256').update(s).digest('hex');

export function safeEqual(a, b) {
  const x = Buffer.from(String(a ?? ''));
  const y = Buffer.from(String(b ?? ''));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}
