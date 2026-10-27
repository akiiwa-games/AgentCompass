import {
  closeSync,
  cpSync,
  existsSync,
  mkdirSync,
  openSync,
  readdirSync,
  readSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import path from "node:path";

const pluginRoot = path.resolve(import.meta.dirname, "..");
const requireFromPlugin = createRequire(path.join(pluginRoot, "package.json"));

function findCompleteTypeScriptDir() {
  const localAppData =
    process.env.LOCALAPPDATA ?? path.join(homedir(), "AppData", "Local");
  const vscodeRoots = [
    path.join(localAppData, "Programs", "Microsoft VS Code"),
    "C:/Program Files/Microsoft VS Code",
  ];
  for (const root of vscodeRoots) {
    if (!existsSync(root)) {
      continue;
    }
    const direct = path.join(
      root,
      "resources",
      "app",
      "extensions",
      "node_modules",
      "typescript",
    );
    if (existsSync(path.join(direct, "lib", "lib.es2023.d.ts"))) {
      return direct;
    }
    for (const entry of readdirSync(root)) {
      const sub = path.join(
        root,
        entry,
        "resources",
        "app",
        "extensions",
        "node_modules",
        "typescript",
      );
      if (existsSync(path.join(sub, "lib", "lib.es2023.d.ts"))) {
        return sub;
      }
    }
  }
  return null;
}

function findAsarPath() {
  const candidates = [
    process.env.PASEO_APP_ASAR,
    path.join(
      process.env.LOCALAPPDATA ?? path.join(homedir(), "AppData", "Local"),
      "Programs",
      "Paseo",
      "resources",
      "app.asar",
    ),
    "/Applications/Paseo.app/Contents/Resources/app.asar",
    "/opt/Paseo/resources/app.asar",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function extractTypeScriptFromAsar(asarPath, targetNodeModules) {
  const fd = openSync(asarPath, "r");
  try {
    const headerBuf = Buffer.alloc(16);
    readSync(fd, headerBuf, 0, 16, 0);
    const headerSize = headerBuf.readUInt32LE(4);
    const headerStrLen = headerBuf.readUInt32LE(12);
    const jsonBuf = Buffer.alloc(headerStrLen);
    readSync(fd, jsonBuf, 0, headerStrLen, 16);
    const header = JSON.parse(jsonBuf.toString("utf8").replace(/\0+$/u, ""));
    const baseOffset = 8 + headerSize;

    const tsNode = header?.files?.node_modules?.files?.typescript;
    if (!tsNode || !tsNode.files) {
      throw new Error(`typescript not found inside ${asarPath}`);
    }

    function extractTree(node, relDir) {
      const outDir = path.join(targetNodeModules, "typescript", relDir);
      mkdirSync(outDir, { recursive: true });
      for (const [name, child] of Object.entries(node.files ?? {})) {
        const nextRel = relDir ? path.join(relDir, name) : name;
        if (child && typeof child === "object" && "files" in child) {
          extractTree(child, nextRel);
        } else if (
          child &&
          typeof child === "object" &&
          "offset" in child &&
          "size" in child &&
          !child.unpacked
        ) {
          const size = Number(child.size);
          const offset = baseOffset + Number(child.offset);
          const buf = Buffer.alloc(size);
          readSync(fd, buf, 0, size, offset);
          writeFileSync(path.join(targetNodeModules, "typescript", nextRel), buf);
        }
      }
    }

    extractTree(tsNode, "");
  } finally {
    closeSync(fd);
  }
}

function loadTypeScript() {
  const localTsLib = path.join(
    pluginRoot,
    "node_modules",
    "typescript",
    "lib",
    "lib.es2023.d.ts",
  );
  if (existsSync(localTsLib)) {
    return requireFromPlugin("typescript");
  }

  const completeDir = findCompleteTypeScriptDir();
  const targetTsDir = path.join(pluginRoot, "node_modules", "typescript");
  if (completeDir) {
    mkdirSync(path.dirname(targetTsDir), { recursive: true });
    cpSync(completeDir, targetTsDir, { recursive: true, force: true });
    return requireFromPlugin("typescript");
  }

  const asarPath = findAsarPath();
  if (!asarPath) {
    throw new Error(
      "TypeScript is not installed in node_modules and Paseo app.asar was not found.",
    );
  }
  extractTypeScriptFromAsar(asarPath, path.join(pluginRoot, "node_modules"));
  if (!existsSync(localTsLib)) {
    throw new Error("Incomplete bundled TypeScript: this project requires ES2023 libraries. Run npm ci in the plugin directory.");
  }
  return requireFromPlugin("typescript");
}

const ts = loadTypeScript();
const configPath = path.join(pluginRoot, "tsconfig.json");
const configFile = ts.readConfigFile(configPath, ts.sys.readFile);
if (configFile.error) {
  console.error(
    ts.formatDiagnosticsWithColorAndContext([configFile.error], {
      getCanonicalFileName: (f) => f,
      getCurrentDirectory: () => pluginRoot,
      getNewLine: () => "\n",
    }),
  );
  process.exit(1);
}

const parsed = ts.parseJsonConfigFileContent(
  configFile.config,
  ts.sys,
  pluginRoot,
);
const program = ts.createProgram({
  rootNames: parsed.fileNames,
  options: {
    ...parsed.options,
    noEmit: true,
  },
});

const diagnostics = [
  ...parsed.errors,
  ...ts.getPreEmitDiagnostics(program),
];

if (diagnostics.length > 0) {
  console.error(
    ts.formatDiagnosticsWithColorAndContext(diagnostics, {
      getCanonicalFileName: (f) => f,
      getCurrentDirectory: () => pluginRoot,
      getNewLine: () => "\n",
    }),
  );
  process.exit(1);
}

console.log(
  `Typecheck passed (${parsed.fileNames.length} files, TypeScript ${ts.version}).`,
);
