declare module "node:fs" {
  export function existsSync(path: string): boolean;
  export function readFileSync(path: string, encoding: "utf-8" | "utf8"): string;
  export function writeFileSync(path: string, data: string, encoding?: "utf-8" | "utf8" | { encoding: "utf-8"; flag: "wx" }): void;
  export function renameSync(from: string, to: string): void;
  export function rmSync(path: string, options?: { force?: boolean }): void;
  export function mkdirSync(path: string, options?: { recursive?: boolean }): string | undefined;
  const fsDefault: {
    existsSync: typeof existsSync;
    readFileSync: typeof readFileSync;
    writeFileSync: typeof writeFileSync;
    mkdirSync: typeof mkdirSync;
    renameSync: typeof renameSync;
    rmSync: typeof rmSync;
  };
  export default fsDefault;
}

declare module "node:crypto" {
  export function randomUUID(): string;
}

declare module "node:os" {
  export function homedir(): string;
  const osDefault: {
    homedir: typeof homedir;
  };
  export default osDefault;
}

declare module "node:path" {
  export function join(...paths: string[]): string;
  export function dirname(p: string): string;
  const pathDefault: {
    join: typeof join;
    dirname: typeof dirname;
  };
  export default pathDefault;
}
