# Third-party code served as-is

| File | What | Version | Licence |
|---|---|---|---|
| `zxing.min.js` | ZXing "zebra crossing" barcode library, UMD build of `@zxing/library` | 0.23.0 | Apache-2.0 |

Copied from `web/node_modules/@zxing/library/umd/index.min.js` (already a dependency of the
Next.js app). The door page loads it only in browsers without the built-in `BarcodeDetector`.
