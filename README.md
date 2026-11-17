# fx-never-sleeps Discord sandbox

Discord users @mention a bot in one channel. Each request gets its own Discord thread and its own OpenCode session, running on DeepSeek. The agent can read and change the `fx-never-sleeps` repo, run shell commands, use the open web and open PRs. Slash commands set each thread's model, thinking effort and agent, and add session tools such as undo, diff and fork.

Everything runs as three Docker containers on Docker Desktop, in the `fx-sandbox` compose project. Nothing from Windows is mounted into them.

## How it fits together

| Container | Holds | Does |
| --- | --- | --- |
| `discord-bridge` | Discord bot token | Watches one channel, opens threads, and passes messages and slash commands to the agent's API. Follows the agent's event stream for live progress and questions. |
| `opencode-agent` | Scoped GitHub token | Runs `opencode serve` in a clone of the repo. Anyone in the channel effectively controls this container. |
| `llm-proxy` | DeepSeek API key | Forwards only chat completions and the model list to DeepSeek, adds the real key, and limits the agent to 240 requests a minute. |

Each container reads its own file in `secrets/`, so none of them sees another's keys. The one shared value is the OpenCode server password in `shared.env`, which the bridge uses to call the agent.

State lives in three volumes, which survive `-Stop` and redeploys: `work` (the repo clone), `agent-data` (OpenCode sessions) and `bridge-data` (thread map, per-thread settings and open questions).

| Path | What it is |
| --- | --- |
| `compose.yaml` | The containers, networks, volumes and resource limits |
| `start.ps1` | Deploy, status, logs and stop |
| `discord-bridge/` | The bot, with its tests. Python 3.13, discord.py and aiohttp. |
| `llm-proxy/` | The proxy that holds the DeepSeek key. Python standard library only. |
| `opencode-agent/` | Agent image (OpenCode pinned to 1.19.5), egress firewall, startup scripts and `opencode.json` |
| `secrets.example/` | One env file per container with placeholders. Real values go in `secrets/`, which git ignores. |

### Isolation

- **Windows:** no Windows folder is mounted into any container. The images are built from this folder, but only the files each Dockerfile copies end up inside.
- **Network:** the agent's firewall is set up as root at startup, before the agent drops to the `node` user with no capabilities. It blocks every private range, which covers the Windows host, your LAN and the other containers. The only private destination it allows is port 8080 on the network the agent shares with the proxy. The open internet is allowed.
- **Internal networks:** `bridge-net` (bridge ↔ agent) and `llm-net` (agent ↔ proxy) have no route outside.
- **Limits:** every container has CPU, memory and process limits, drops all capabilities and sets `no-new-privileges`. The bridge and proxy have read-only root filesystems.

The remaining risk is a container escape or a Linux kernel exploit. Docker Desktop's VM can reach your Windows files (it shares them for bind mounts), and all WSL distros share one VM kernel, so an escape could reach `C:`. In Docker Desktop's settings, keep "Expose daemon on tcp://localhost:2375 without TLS" turned off.

## Setup

1. **Docker Desktop:** install it with the WSL 2 backend. In Settings → General, turn on "Start Docker Desktop when you sign in", so the bot comes back after a reboot. The containers restart on their own once Docker is up.

2. **Secrets:** copy the examples, then fill in each file.

   ```powershell
   Copy-Item -Recurse secrets.example secrets
   ```

   | File | Set |
   | --- | --- |
   | `secrets/proxy.env` | `DEEPSEEK_API_KEY` |
   | `secrets/agent.env` | `GH_TOKEN` (step 3). Change `REPO` (`owner/name`) to use a different repo. |
   | `secrets/bridge.env` | `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` (step 5). Optionally `MAX_CONCURRENT` (default 16) and the time limits under [Using it](#using-it). |
   | `secrets/shared.env` | `OPENCODE_SERVER_PASSWORD`, any long random string |

   One way to make the password:

   ```powershell
   $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
   ```

3. **GitHub token:** create a fine-grained personal access token with access to `fx-never-sleeps` only, Contents and Pull requests set to read and write, and no other permissions. Leaving out Workflows means nobody can add Actions workflows.

4. **Protect `main`:** in the repo, add a ruleset on `main` that requires a pull request with your approval and blocks force pushes and deletion.

5. **Discord bot:** in the Developer Portal, turn Message Content Intent on and Public Bot off. Invite the bot with this link:

   ```
   https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot&permissions=309237746752
   ```

   It grants View Channel, Send Messages, Read Message History, Create Public Threads, Send Messages in Threads, Add Reactions and Attach Files. The `bot` scope also covers the slash commands.

   To get the channel ID, turn on Developer Mode in Discord, right-click the channel and choose Copy Channel ID.

6. **Start it:** run `.\start.ps1`. It warns about any secret that's still a placeholder, then builds the images and starts the containers. In the bridge log, look for `watching #<channel>`, which also names any missing permissions, followed by `slash commands ready: ...`.

## Running it

```powershell
.\start.ps1           # build and (re)start everything; run it again after editing any file here
.\start.ps1 -Status   # container state, and which commit each container runs
.\start.ps1 -Logs     # follow the logs
.\start.ps1 -Stop     # stop the containers (volumes are kept)
```

Each deploy labels every container `fx.revision` with this folder's commit, plus `-dirty` if there were uncommitted changes. `-Status` compares it with the folder's current commit in its `UpToDate` column. Docker Desktop shows the same label on each container under the `fx-sandbox` project, along with its logs and resource use.

## Using it

- **New request:** @mention the bot in the channel. It opens a thread with a new OpenCode session.
- **Follow-ups:** any message in that thread continues the same session. Only threads under `DISCORD_CHANNEL_ID` count, so moving the bot to another channel leaves its old threads silent.
- **One checkout:** every thread works in the same clone, `/work/repo`. Two threads editing the same files at the same time can collide. The bridge makes no branches, so ask the agent for one when a change needs it.
- **Parallel threads:** up to `MAX_CONCURRENT` threads run at once, and further requests wait in a queue. Within a thread, messages run one at a time, in order.
- **Attachments:** text files go to the agent as their content, up to 100 KB each and 200 KB per message. Other files, such as images, and larger ones are passed as links the agent can try to fetch.
- **Progress:** while the agent works, one status message shows the current tool call and the agent's todo list. When the work ends, it shrinks to one line with the time taken, the number of tool calls and the context size. The answer follows as a normal message.
- **Questions:** each question from the agent appears with a button per option (or a menu) and "Type an answer". Anyone in the thread can answer. When only one question is open, a plain message in the thread counts as the answer. The request waits for every answer, and its time limit is paused meanwhile.
- **Stop:** `!stop` or `/stop` aborts the running request, dismisses its questions and drops that thread's queued messages.
- **Restarts:** if the bridge restarts, a running request is picked back up and open questions keep their buttons. Messages waiting in a queue are lost. If the agent container restarts, its running requests end.
- **Mentions:** the bot never pings anyone, whatever the agent writes.

A request is stopped, and the thread is told, when it hits one of these limits. Each one can be changed in `secrets/bridge.env`.

| Setting | Default | Stops a request when |
| --- | --- | --- |
| `PROMPT_TIMEOUT_SECONDS` | 1800 (30 min) | it has worked this long, not counting time spent waiting for an answer |
| `PROMPT_SILENCE_SECONDS` | 900 (15 min) | the agent has shown no activity for this long |
| `QUESTION_TIMEOUT_SECONDS` | 3600 (60 min) | a question has gone unanswered this long |
| `SHELL_TIMEOUT_SECONDS` | 600 (10 min) | a `/shell` command is still running after this long |

### Slash commands

Anyone who can post in the channel can use them. Changes are announced in the thread. `/status`, `/todo`, `/help` and the `/undo` confirmation are shown only to the person who used them.

| Command | What it does |
| --- | --- |
| `/model` | Choose the thread's model |
| `/effort` | Choose how hard the model thinks: `off`, `low`, `high` or `max` |
| `/agent` | Choose the OpenCode agent: `build` (the default) or `plan`, which reads and proposes without editing files |
| `/status` | Show the thread's settings, whether a request is running and how full the context is |
| `/todo` | Show the agent's whole todo list |
| `/diff` | Post the session's file changes as a `.diff` file, one section per request |
| `/export` | Post the conversation as a Markdown file |
| `/stop` | Same as `!stop` |
| `/undo` | Take back the last request and its file changes. Asks first, naming the files. Run it again to go further back. |
| `/redo` | Bring back what `/undo` took back, until the next message is sent |
| `/new` | Start a fresh session in the same thread. Settings stay, earlier context is dropped. |
| `/compact` | Replace the conversation so far with a summary, to free up context |
| `/fork` | Open a new thread that carries on from here, with a copy of the conversation and settings |
| `/rename` | Rename the thread and its session |
| `/shell` | Run a shell command in the checkout and post its output. The agent sees it in later messages. |
| `/command` | Run one of the agent's own commands or skills, such as `review` |
| `/help` | List the commands |

- **In the main channel**, `/model`, `/effort` and `/agent` set what new threads start with, and `/status` shows those defaults. A new thread copies the defaults when it opens, so later changes leave existing threads alone. The other commands work only inside a thread.
- **`default`** as the value puts a setting back on the agent's own config.
- **While a request runs**, a settings change applies from the next message. `/compact`, `/fork`, `/shell` and `/command` wait their turn like a message would. `/undo` and `/redo` refuse.
- **`/undo` and the shared checkout:** undo puts the request's files back as they were before it. If another thread has changed those files since, its changes to them are lost too. The confirmation warns about this.

## Models

The models, the default and the effort levels all come from `opencode-agent/opencode.json`. The slash commands read them from the agent, so after you edit that file and redeploy, `/model` and `/effort` offer the new list. A thread whose model or effort level is no longer offered goes back to the default, and the thread is told.

- `model` is what threads use until someone runs `/model`. It is `deepseek-flash`, and `deepseek-v4-pro` is also configured.
- Each model's `variants` are its effort levels. `low`, `high` and `max` send DeepSeek's `reasoning_effort`, and `off` turns thinking off. With no level chosen, nothing is sent and DeepSeek uses its own default: thinking on, at `high`.

## Tests

The bridge's unit tests run inside a Docker build, so nothing needs installing on Windows. The build fails if a test fails.

```powershell
docker build --target test --progress plain -t fx-sandbox-bridge-test discord-bridge
```

They cover the bridge with stand-ins for Discord and the agent. The agent stand-in replays event streams recorded from a real OpenCode 1.19.5 server, kept in `discord-bridge/tests/fixtures/`. The live Discord side (buttons, file uploads, thread renames) is only checked by hand.
