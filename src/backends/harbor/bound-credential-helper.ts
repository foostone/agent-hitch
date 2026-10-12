import { readBoundHelperCredentials } from "./evaluation-binding.js";

// This process speaks only the private host-helper pipe protocol. Never print errors or credentials to stderr.
try {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const value of process.stdin) {
    const chunk = Buffer.from(value); size += chunk.length;
    if (size > 64 * 1024) throw new Error("invalid input");
    chunks.push(chunk);
  }
  const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!input || typeof input !== "object" || Object.keys(input).sort().join(",") !== "credentialNames,minimumValidityMs,version"
    || input.version !== 1 || !Array.isArray(input.credentialNames) || input.credentialNames.some((name: unknown) => typeof name !== "string")
    || process.argv.length !== 4) throw new Error("invalid input");
  const value = await readBoundHelperCredentials(process.argv[2]!, process.argv[3]!, input.credentialNames, input.minimumValidityMs);
  process.stdout.write(JSON.stringify(value) + "\n");
} catch { process.exitCode = 1; }
