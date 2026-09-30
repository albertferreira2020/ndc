// NDC wrapper: sets the hook environment in Node so the registered command has no POSIX `VAR=x cmd` prefix
// and works the same in sh, cmd.exe and PowerShell. Args: NDC_HOOK=1 <profile> <data> <runtime> <hook args...>
const { spawnSync } = require('child_process');
const path = require('path');
const [, , , profile, data, rt, ...rest] = process.argv;
const env = { ...process.env, ECC_HOOK_PROFILE: profile, ECC_SKIP_LLM_SUMMARY: '1', ECC_AGENT_DATA_HOME: data,
  CLV2_HOMUNCULUS_DIR: path.join(data, 'homunculus'), CLAUDE_PLUGIN_ROOT: rt };
const r = spawnSync(process.execPath, [path.join(rt, 'scripts', 'hooks', 'run-with-flags.js'), ...rest], { stdio: 'inherit', env });
process.exit(r.status === null ? 1 : r.status);
