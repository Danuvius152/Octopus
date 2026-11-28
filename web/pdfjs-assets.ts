import { readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import type { Plugin } from "vite";

export function pdfjsAssets(): Plugin {
  const require = createRequire(import.meta.url);
  const manifest = require.resolve("pdfjs-dist/package.json");
  const { version } = JSON.parse(readFileSync(manifest, "utf8")) as { version: string };
  const root = dirname(manifest);
  const output = `assets/pdfjs-${version}`;
  const files = new Map<string, string>([[`${output}/LICENSE`, join(root, "LICENSE")]]);
  for (const directory of ["cmaps", "iccs", "standard_fonts", "wasm"]) {
    for (const name of readdirSync(join(root, directory))) {
      // The reader doesn't run PDF scripts.
      if (!name.startsWith("quickjs-")) {
        files.set(`${output}/${directory}/${name}`, join(root, directory, name));
      }
    }
  }
  return {
    name: "pdfjs-assets",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url?.split("?", 1)[0].slice(1);
        const file = url && files.get(url);
        if (!file) return next();
        res.setHeader("Content-Type", file.endsWith(".wasm") ? "application/wasm" :
          file.endsWith(".js") ? "text/javascript" : "application/octet-stream");
        res.end(readFileSync(file));
      });
    },
    generateBundle() {
      for (const [fileName, file] of files) {
        this.emitFile({ type: "asset", fileName, source: readFileSync(file) });
      }
    },
  };
}
