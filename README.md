# fx-never-sleeps Discord sandbox

Discord users @mention a bot in one channel. Each request gets its own Discord thread and its own OpenCode session, running on DeepSeek. Up to 16 sessions run in parallel. They all work in one shared clone of the repo. The agent can read and change the `fx-never-sleeps` repo, run shell commands, use the open web, and open PRs.

Everything runs as Docker containers on Docker Desktop, in the `fx-sandbox` compose project, so you can watch them, their logs and their resource use in the Docker Desktop dashboard. Nothing from Windows is mounted into them.

## Layout

| Container | Holds | Notes |
| --- | --- | --- |
| `discord-bridge` | Discord bot token | Watches one channel, opens threads, forwards text to the agent's API. |
| `opencode-agent` | Scoped GitHub token | `opencode serve` plus the repo. Users fully control this container. |
| `llm-proxy` | DeepSeek API key | Forwards only chat completions and the model list, adds the real key, rate-limits. |

Each container's secrets come from its own file in `secrets/`, so no container gets another one's keys.

| Path | What it is |
| --- | --- |
| `compose.yaml` | The three containers, their networks and resource limits |
| `start.ps1` | Deploy, status (including which commit is deployed), logs and stop |
| `discord-bridge/` | `bridge.py` (Discord side) and `opencode.py` (OpenCode API client). Python 3.13, discord.py and aiohttp. |
| `llm-proxy/server.py` | The proxy that holds the DeepSeek key. Python standard library only. |
| `opencode-agent/` | Agent image, egress firewall, startup scripts, OpenCode config. Node, because OpenCode is installed from npm. |
| `secrets.example/` | One env file per container with placeholders. Real values go in `secrets/`, which git ignores. |

### Isolation

- **Windows:** no Windows folder is mounted into any container. The images are built from this folder, but only the files each Dockerfile copies end up inside.
- **Network:** the agent's firewall is set up as root at startup, before it drops to the `node` user with no capabilities. It blocks every private range, which covers the Windows host, your LAN and the other containers. The only private destination it allows is port 8080 on the internal network shared with the proxy, so a recreated proxy with a new IP stays reachable. The open internet is allowed.
- **Internal networks:** `bridge-net` (bridge ↔ agent) and `llm-net` (agent ↔ proxy) have no route outside.
- **Limits:** CPU, memory and process limits on every container. `no-new-privileges` is set and all capabilities are dropped.

The remaining risk is a container escape or a Linux kernel exploit. Docker Desktop's VM can reach your Windows files (it shares them for bind mounts), and all WSL distros share one VM kernel, so an escape could reach `C:`.

In Docker Desktop's settings, keep "Expose daemon on tcp://localhost:2375 without TLS" turned off.

## Setup

1. **Docker Desktop:** install it with the WSL 2 backend. In Settings → General, turn on "Start Docker Desktop when you sign in", so the bot comes back after a reboot (the containers restart on their own once Docker is up).

2. **Secrets:** copy the examples, then fill in each file.

   ```powershell
   Copy-Item -Recurse secrets.example secrets
   ```

   | File | Set |
   | --- | --- |
   | `secrets/proxy.env` | `DEEPSEEK_API_KEY` |
   | `secrets/agent.env` | `GH_TOKEN` (see step 3). `REPO` if it isn't `fx-never-sleeps`. |
   | `secrets/bridge.env` | `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` (see step 5). Optionally `MAX_CONCURRENT` and `PROMPT_TIMEOUT_SECONDS`. |
   | `secrets/shared.env` | `OPENCODE_SERVER_PASSWORD`, any long random string. |

   One way to make the password:

   ```powershell
   $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
   ```

3. **GitHub token:** create a fine-grained personal access token with:
   - Repository access: only `fx-never-sleeps`
   - Contents: read and write
   - Pull requests: read and write
   - No other permissions. Leaving out Workflows means nobody can add Actions workflows.

4. **Protect `main`:** in the repo, add a ruleset on `main` that requires a pull request with your approval and blocks force pushes and deletion.

5. **Discord bot:** in the Developer Portal, turn Message Content Intent on and Public Bot off. Invite the bot with `https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot&permissions=309237746752`, which grants:
   - View Channel
   - Send Messages
   - Read Message History
   - Create Public Threads
   - Send Messages in Threads
   - Add Reactions (for the `!stop` reaction)
   - Attach Files

   To get the channel ID, turn on Developer Mode in Discord, right-click the channel and choose Copy Channel ID.

6. **Start it:**

   ```powershell
   .\start.ps1
   ```

   This builds the images from this folder and starts the containers. The bridge log should end with `watching #<channel>`, and it names any missing permissions.

Other commands:

```powershell
.\start.ps1 -Status   # container status, and which commit each container runs
.\start.ps1 -Logs     # follow logs
.\start.ps1 -Stop     # stop containers (repo clone, sessions and thread map are kept)
```

After editing any file here, run `.\start.ps1` again to redeploy.

### Which version is running

Each deploy stamps every container with an `fx.revision` label: the commit this folder was at, plus `-dirty` if it had uncommitted changes. `.\start.ps1 -Status` shows it next to the folder's current commit, with an `UpToDate` column. In Docker Desktop, open a container and look under its labels.

## Using it

- **New request:** @mention the bot in the channel. It opens a thread with a new OpenCode session working in `/work/repo`.
- **Branches:** the bridge doesn't create branches or worktrees. Ask the agent for one if a change needs it.
- **Follow-ups:** any message in that thread continues the same session. Only threads under `DISCORD_CHANNEL_ID` count: if you move the bot to another channel, its old threads stop responding.
- **Parallel threads:** up to `MAX_CONCURRENT` (16) threads run at once, and further requests wait in a queue. All sessions share one checkout, so two threads editing the same files at the same time can collide.
- **Cancel:** `!stop` in the thread aborts the current request and drops any of that thread's messages still waiting in the queue.
- **Time limit:** a request that runs longer than 30 minutes is stopped, and the thread is told. Set `PROMPT_TIMEOUT_SECONDS` in `secrets/bridge.env` to change it.
- **Idle threads:** a thread uses a slot only while the agent is answering it. Old threads cost a few KB of session history on disk, and any new message in one resumes its session.
- **Replies:** the bot never pings anyone, whatever the agent writes.

## Models

The default is `deepseek-flash`. `deepseek-v4-pro` is also configured. Change `model` in `opencode-agent/opencode.json` and redeploy.

## Not done yet

- A system prompt for the branch-and-PR workflow
- Tighter access control (a role or a user allowlist)
- Scheduled resets of the agent container and its volumes
