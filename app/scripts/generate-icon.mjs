#!/usr/bin/env node
// Generate a simple placeholder icon without network calls.
// Creates a minimal valid PNG locally for use as the app icon (plain glyph).
import { mkdirSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "zlib";

const here = dirname(fileURLToPath(import.meta.url));
const APP_ROOT = join(here, "..");

function generatePlaceholderIcon() {
  const iconDir = join(APP_ROOT, "build", "icons");
  mkdirSync(iconDir, { recursive: true });

  // Generate a simple 512x512 PNG with a light blue background.
  // Electron-builder requires icons to be at least 512x512 pixels.
  // This is generated programmatically without any network calls or external dependencies.
  const width = 512;
  const height = 512;
  const bitDepth = 8;
  const colorType = 2; // RGB

  // PNG signature
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

  // IHDR chunk
  const ihdr = createChunk("IHDR", Buffer.concat([
    encodeU32BE(width),
    encodeU32BE(height),
    Buffer.from([bitDepth, colorType, 0, 0, 0]) // bit depth, color type, compression, filter, interlace
  ]));

  // IDAT chunk - pixel data
  // Create pixel data: light blue background (RGB: 173, 216, 230)
  const pixelData = Buffer.alloc(height * (1 + width * 3)); // 1 filter byte + RGB per pixel
  for (let y = 0; y < height; y++) {
    const rowStart = y * (1 + width * 3);
    pixelData[rowStart] = 0; // filter type: None
    for (let x = 0; x < width; x++) {
      const pixelStart = rowStart + 1 + x * 3;
      pixelData[pixelStart] = 173;     // R
      pixelData[pixelStart + 1] = 216; // G
      pixelData[pixelStart + 2] = 230; // B
    }
  }

  const compressedData = zlib.deflateSync(pixelData);
  const idat = createChunk("IDAT", compressedData);

  // IEND chunk (empty)
  const iend = createChunk("IEND", Buffer.alloc(0));

  // Combine all chunks
  const png = Buffer.concat([signature, ihdr, idat, iend]);

  const pngPath = join(iconDir, "icon.png");
  writeFileSync(pngPath, png);
  console.log(`Generated placeholder icon at ${pngPath}`);
}

function encodeU32BE(value) {
  const buf = Buffer.alloc(4);
  buf.writeUInt32BE(value, 0);
  return buf;
}

function crc32(data) {
  // Simple CRC32 implementation for PNG chunks
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < data.length; i++) {
    crc = crc ^ data[i];
    for (let j = 0; j < 8; j++) {
      crc = (crc >>> 1) ^ ((crc & 1) ? 0xEDB88320 : 0);
    }
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function createChunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);

  const typeBuffer = Buffer.from(type, "ascii");
  const chunkData = Buffer.concat([typeBuffer, data]);

  const crcValue = crc32(chunkData);
  const crcBuffer = Buffer.alloc(4);
  crcBuffer.writeUInt32BE(crcValue, 0);

  return Buffer.concat([length, chunkData, crcBuffer]);
}

function main() {
  try {
    generatePlaceholderIcon();
  } catch (error) {
    console.error("Icon generation failed:", error);
    process.exit(1);
  }
}

main();
