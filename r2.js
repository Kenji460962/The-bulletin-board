import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import 'dotenv/config';

const R2_ENDPOINT = process.env.R2_ENDPOINT;
const R2_ACCESS_KEY_ID = process.env.R2_ACCESS_KEY_ID;
const R2_SECRET_ACCESS_KEY = process.env.R2_SECRET_ACCESS_KEY;
const R2_BUCKET_NAME = process.env.R2_BUCKET_NAME;
const R2_PUBLIC_URL = process.env.R2_PUBLIC_URL;

const isR2Configured = Boolean(
  R2_ENDPOINT &&
  R2_ACCESS_KEY_ID &&
  R2_SECRET_ACCESS_KEY &&
  R2_BUCKET_NAME
);

console.log(`[Storage] Cloudflare R2 is ${isR2Configured ? 'Configured (Active)' : 'Not configured (using local static uploads fallback)'}`);

let s3Client = null;
if (isR2Configured) {
  try {
    s3Client = new S3Client({
      region: 'auto',
      endpoint: R2_ENDPOINT,
      credentials: {
        accessKeyId: R2_ACCESS_KEY_ID,
        secretAccessKey: R2_SECRET_ACCESS_KEY
      }
    });
  } catch (err) {
    console.warn('[Storage] S3Client initialization error:', err.message);
  }
}

export async function uploadImage(fileBuffer, originalFilename, mimeType = 'image/png') {
  const ext = path.extname(originalFilename) || '.png';
  const filename = `${crypto.randomUUID()}${ext}`;

  // If Cloudflare R2 is configured and active, upload directly to R2
  if (isR2Configured && s3Client) {
    try {
      const command = new PutObjectCommand({
        Bucket: R2_BUCKET_NAME,
        Key: filename,
        Body: fileBuffer,
        ContentType: mimeType
      });
      await s3Client.send(command);

      const baseUrl = R2_PUBLIC_URL ? R2_PUBLIC_URL.replace(/\/+$/, '') : '';
      if (baseUrl) {
        return `${baseUrl}/${filename}`;
      }
      return `${R2_ENDPOINT.replace(/\/+$/, '')}/${R2_BUCKET_NAME}/${filename}`;
    } catch (err) {
      console.warn('[R2 Upload Warning, falling back to local]:', err.message);
    }
  }

  // Fallback to local static upload
  const uploadsDir = path.join(process.cwd(), 'static', 'uploads');
  if (!fs.existsSync(uploadsDir)) {
    fs.mkdirSync(uploadsDir, { recursive: true });
  }
  const filePath = path.join(uploadsDir, filename);
  fs.writeFileSync(filePath, fileBuffer);
  return `/static/uploads/${filename}`;
}
