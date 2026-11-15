import { compile } from "json-schema-to-typescript";
import { readFile, writeFile } from "node:fs/promises";
const schema = JSON.parse(
  await readFile(
    new URL("../../docs/contracts/schema.json", import.meta.url),
    "utf8",
  ),
);
const text = await compile(schema, "ContractBundle", {
  bannerComment:
    "/* Generated from Python contracts via JSON Schema. Do not edit. */",
  additionalProperties: false,
});
const file = new URL("../src/contracts/generated.ts", import.meta.url);
if (process.argv.includes("--check")) {
  if ((await readFile(file, "utf8")) !== text)
    throw new Error("TypeScript contracts drift; run npm run contracts");
} else await writeFile(file, text);
