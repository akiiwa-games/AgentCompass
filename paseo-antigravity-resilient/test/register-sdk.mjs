import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";

const shimUrl = pathToFileURL(
  path.resolve(import.meta.dirname, "sdk-shim.ts"),
).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (
      specifier === "@getpaseo/plugin" ||
      specifier === "@getpaseo/plugin/server" ||
      specifier === "@getpaseo/plugin/server/provider" ||
      specifier === "@getpaseo/plugin/server/usage"
    ) {
      return {
        url: shimUrl,
        shortCircuit: true,
      };
    }
    return nextResolve(specifier, context);
  },
});
