import sharp from "sharp";
import { z } from "zod";

export const MAX_IMAGE_BYTES = 5_000_000;
export const MAX_IMAGE_PIXELS = 20_000_000;
export const MAX_IMAGE_SIDE = 1_600;
export const MAX_MASK_RECTS = 20;

export const maskRectSchema = z.object({
  x: z.number().finite().min(0).max(1),
  y: z.number().finite().min(0).max(1),
  width: z.number().finite().min(0.005).max(1),
  height: z.number().finite().min(0.005).max(1),
}).strict().refine(rect => rect.x + rect.width <= 1.0001 && rect.y + rect.height <= 1.0001, "マスキング範囲が画像外です。");

export type MaskRect = z.infer<typeof maskRectSchema>;
export type ProcessedImage = { bytes: Buffer; width: number; height: number; sha256: string };

export async function processMaskedImage(raw: Buffer, rects: MaskRect[] = []): Promise<ProcessedImage> {
  if (raw.length === 0 || raw.length > MAX_IMAGE_BYTES) throw new Error("画像は5MB以下にしてください。");
  const masks = z.array(maskRectSchema).max(MAX_MASK_RECTS).parse(rects);
  const source = sharp(raw, { limitInputPixels: MAX_IMAGE_PIXELS, failOn: "warning", animated: false });
  const metadata = await source.metadata();
  if (!["jpeg", "png", "webp"].includes(metadata.format ?? "")) throw new Error("JPEG、PNG、WebP画像だけ利用できます。");
  if ((metadata.pages ?? 1) !== 1) throw new Error("アニメーション画像は利用できません。");
  let image = source.rotate().resize({ width: MAX_IMAGE_SIDE, height: MAX_IMAGE_SIDE, fit: "inside", withoutEnlargement: true }).removeAlpha();
  const resized = await image.clone().toBuffer({ resolveWithObject: true });
  image = sharp(resized.data);
  for (const rect of masks) {
    const left = Math.max(0, Math.floor(rect.x * resized.info.width));
    const top = Math.max(0, Math.floor(rect.y * resized.info.height));
    const width = Math.min(resized.info.width - left, Math.max(1, Math.ceil(rect.width * resized.info.width)));
    const height = Math.min(resized.info.height - top, Math.max(1, Math.ceil(rect.height * resized.info.height)));
    const blurred = await image.clone().extract({ left, top, width, height }).blur(Math.max(12, Math.floor(Math.min(width, height) / 6))).toBuffer();
    image = image.composite([{ input: blurred, left, top }]);
  }
  const bytes = await image.jpeg({ quality: 85, mozjpeg: true }).toBuffer();
  const clean = await sharp(bytes).metadata();
  if (clean.exif || clean.xmp || clean.iptc || clean.icc) throw new Error("画像メタデータを除去できませんでした。");
  const { createHash } = await import("node:crypto");
  return { bytes, width: clean.width ?? 0, height: clean.height ?? 0, sha256: createHash("sha256").update(bytes).digest("hex") };
}

export function decodeImageDataUrl(value: string): Buffer {
  const match = /^data:image\/(?:jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$/.exec(value);
  if (!match) throw new Error("画像データの形式が正しくありません。");
  const raw = Buffer.from(match[1], "base64");
  if (raw.length === 0 || raw.length > MAX_IMAGE_BYTES) throw new Error("画像は5MB以下にしてください。");
  return raw;
}
