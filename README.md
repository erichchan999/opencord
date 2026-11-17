# opencord

opencord is a Discord front end to [OpenCode](https://opencode.ai). @mention the bot in an enabled channel and it opens a thread with its own OpenCode session, which works on a GitHub repo of your choice: reading and changing code, running commands and opening pull requests. Slash commands control each thread's model, effort and session. Everything runs in Docker, with DeepSeek as the model provider.

## How it fits together

| Container | Holds | Does |
| --- | --- | --- |
| `discord-bridge` | Discord bot token | Accepts input from enabled channels, opens threads, and passes messages and slash commands to the agent's API. Follows the agent's event stream for live progress and questions. |
| `opencode-agent` | Scoped GitHub token | Runs `opencode serve` in a clone of the repo. Anyone who can post in an enabled channel effectively controls this container. |
| `llm-proxy` | DeepSeek API key | Forwards only chat completions and the model list to DeepSeek, adds the real key, and limits the agent to 240 requests a minute. |

The containers run in the `opencord` compose project. Each one reads its own file in `secrets/`, so none of them sees another's keys. The one shared value is the OpenCode server password in `shared.env`, which the bridge uses to call the agent.

State lives in three volumes, which survive stopping and redeploying: `work` (the repo clone), `agent-data` (OpenCode sessions) and `bridge-data` (thread map, per-thread settings and open questions).

| Path | What it is |
| --- | --- |
| `compose.yaml` | The containers, networks, volumes and resource limits |
| `start.sh`, `start.ps1` | Deploy, status, logs and stop, on macOS/Linux and on Windows |
| `discord-bridge/` | The bot, with its tests. Python 3.13, discord.py and aiohttp. |
| `llm-proxy/` | The proxy that holds the DeepSeek key. Python standard library only. |
| `opencode-agent/` | Agent image (OpenCode pinned to 1.19.5), egress firewall, startup scripts, `opencode.json`, and `system-prompt.md`, which tells the agent about this system |
| `secrets.example/` | One env file per container with placeholders. Real values go in `secrets/`, which git ignores. |

### Isolation

- **Host files:** nothing from the host is mounted into any container. The agent image carries a read-only copy of this folder at `/opt/opencord`, so the agent can answer questions about the system. `.dockerignore` keeps `secrets/` out of it, and the build fails if `secrets/` ever gets in.
- **Network:** the agent's firewall is set up as root at startup, before the agent drops to the `node` user with no capabilities. It blocks every private range, which covers the host, your LAN and the other containers. The only private destination it allows is port 8080 on the network the agent shares with the proxy. The open internet is allowed.
- **Internal networks:** `bridge-net` (bridge ↔ agent) and `llm-net` (agent ↔ proxy) have no route outside.
- **Limits:** every container has CPU, memory and process limits, drops all capabilities and sets `no-new-privileges`. The bridge and proxy have read-only root filesystems.

The remaining risk is a container escape or a Linux kernel exploit. Docker Desktop (Windows, macOS) runs containers in a VM that can reach the host folders it shares. Docker Engine on Linux shares the host's kernel. Either way, an escape could reach your files.

## Setup

1. **Docker and git:** install Docker with Compose v2: Docker Desktop on Windows or macOS, or Docker Engine with the compose plugin on Linux. Have Docker start at boot so the bot comes back after a reboot. On Docker Desktop that's Settings → General → "Start Docker Desktop when you sign in". On Linux, run `sudo systemctl enable docker`. The containers restart on their own once Docker is up. On Windows, also see [Windows](#windows).

2. **Secrets:** copy the examples, then fill in each file.

   ```bash
   cp -r secrets.example secrets
   ```

   | File | Set |
   | --- | --- |
   | `secrets/proxy.env` | `DEEPSEEK_API_KEY`, from platform.deepseek.com |
   | `secrets/agent.env` | `REPO`, the repo the agent works on, as `owner/name`, and `GH_TOKEN` (step 3) |
   | `secrets/bridge.env` | `DISCORD_BOT_TOKEN` (step 5). Optionally `DISCORD_CHANNEL_IDS`, `MAX_CONCURRENT` (default 16) and the time limits under [Using it](#using-it). |
   | `secrets/shared.env` | `OPENCODE_SERVER_PASSWORD`, any long random string, such as the output of `openssl rand -hex 32` |

3. **GitHub token:** see [GitHub token](#github-token).

4. **Protect `main`:** in the repo, add a ruleset on `main` that requires a pull request with your approval and blocks force pushes and deletion.

5. **Discord bot:** see [Discord bot](#discord-bot).

6. **Start it:** run `./start.sh`. It warns about any secret that's still a placeholder, then builds the images and starts the containers. In `./start.sh --logs`, look for `slash commands ready: ...` and `working in #...` for each enabled channel.

### GitHub token

The agent pushes and opens pull requests with a fine-grained personal access token that can reach only the one repo.

1. On GitHub, click your profile picture → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. **Token name:** for example `opencord`. **Expiration:** your choice. When it runs out, the agent can't push or open pull requests until you make a new token, put it in `secrets/agent.env` and run the start script again.
3. **Resource owner:** the account or organization that owns the repo in `REPO`. An organization may have to allow fine-grained tokens, or approve this one, before it works.
4. **Repository access:** choose **Only select repositories** and pick that repo.
5. **Permissions:** under Repository permissions, set **Contents** and **Pull requests** to **Read and write**. GitHub adds **Metadata** (read-only) itself. Leave everything else at **No access**. Without **Workflows**, nobody can add or change GitHub Actions workflows through the agent.
6. Click **Generate token**, copy it, and set it as `GH_TOKEN` in `secrets/agent.env`.

### Discord bot

OpenCord accepts input from the text channels listed in `secrets/bridge.env` and the threads it opens in them. Anyone who can post in an enabled channel can use the coding agent, including `/shell` in its threads, so enable channels whose members you trust with the checkout.

Discord's **View Channel** permission controls which messages the bot receives. OpenCord ignores input from disabled channels even if Discord delivers it.

1. **Create the app:** in the [Developer Portal](https://discord.com/developers/applications), click **New Application** and give it a name. That name is what people @mention.
2. **Installation** page: under Installation Contexts, untick **User Install**, leaving **Guild Install**. Set **Install Link** to **None** and save. Discord won't let you turn off Public Bot while an install link is set.
3. **Bot** page:
   - Turn **Public Bot** off, so only you can add the bot to a server.
   - Under Privileged Gateway Intents, turn **Message Content Intent** on. The bot needs it to read the messages in its threads that don't mention it.
   - Save, then click **Reset Token**. Copy the token and set it as `DISCORD_BOT_TOKEN` in `secrets/bridge.env`.
4. **Invite it:** copy the **Application ID** from the General Information page, put it in this link and open it. You need the Manage Server permission in the server.

   ```
   https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot&permissions=309237745728
   ```

   Discord gives the bot a role with its name, with Send Messages, Read Message History, Create Public Threads, Send Messages in Threads, Add Reactions and Attach Files. The link leaves out View Channel, which the bot can still inherit from @everyone or a channel override. The `bot` scope also covers slash commands.
5. **Give the bot permission in the channels you want to use:** open each channel's settings → **Permissions** → **Advanced permissions**. Add the bot's role and allow View Channel, Send Messages, Send Messages in Threads, Create Public Threads, Read Message History, Add Reactions and Attach Files as needed.
6. **Choose what Discord delivers to the bot:** to keep messages from other channels away from the bot, deny **View Channel** for its role on their categories and on any uncategorized or unsynced channels. Check new categories and channels when you add them. The startup log lists disabled channels the bot can still see.
7. **Enable OpenCord:** set `DISCORD_CHANNEL_IDS` in `secrets/bridge.env` to a comma-separated list of text channel IDs. In Discord, enable **Developer Mode** under **User Settings** → **Advanced**, then right-click each channel and choose **Copy Channel ID**. For example:

   ```dotenv
   DISCORD_CHANNEL_IDS=123456789012345678,234567890123456789
   ```

   An empty list disables session input everywhere. To change access, edit the list and rerun the start script. The bridge logs which channels are enabled and any Discord permissions they need.
8. **Limit command visibility if you want:** in **Server Settings** → **Integrations** → the bot → **Manage**, you can restrict which channels show the session commands.

**Upgrading an existing install:** keep the bot's Discord permissions and add the IDs of every channel you want to use to `DISCORD_CHANNEL_IDS`. Existing threads and sessions can pick up with a new message once their parent channel is enabled.

## Running it

```bash
./start.sh            # build and (re)start everything; run it again after editing any file here
./start.sh --status   # container state, and which commit each container runs
./start.sh --logs     # follow the logs
./start.sh --stop     # stop the containers (volumes are kept)
```

Each deploy labels every container `opencord.revision` with this folder's commit, plus `-dirty` if there were uncommitted changes. The status command compares it with the folder's current commit. Docker Desktop shows the same label on each container under the `opencord` project, along with its logs and resource use.

## Using it

- **New request:** @mention the bot in an enabled channel. It opens a thread with a new OpenCode session.
- **Follow-ups:** a message in that thread continues the same session while its parent channel is enabled. Disabling the parent blocks new input and question answers, and queued input is checked again before it runs. Work already running can finish, and `!stop` or `/stop` still stops it.
- **One checkout:** every thread works in the same clone, `/work/repo`. Two threads editing the same files at the same time can collide. The bridge makes no branches, so ask the agent for one when a change needs it.
- **Parallel threads:** up to `MAX_CONCURRENT` threads run at once, and further requests wait in a queue. Within a thread, messages run one at a time, in order.
- **Attachments:** text files go to the agent as their content, up to 100 KB each and 200 KB per message. Other files, such as images, and larger ones are passed as links the agent can try to fetch.
- **Progress:** while the agent works, one status message shows the current tool call and the agent's todo list. When the work ends, it shrinks to one line with the time taken, the number of tool calls and the context size. The answer follows as a normal message.
- **Questions:** each question from the agent appears with a button per option (or a menu) and "Type an answer". Anyone in the thread can answer. When only one question is open, a plain message in the thread counts as the answer. The request waits for every answer, and its time limit is paused meanwhile.
- **Stop:** `!stop` or `/stop` aborts the running request, dismisses its questions and drops that thread's queued messages.
- **Restarts:** if the bridge restarts, a running request is picked back up and open questions keep their buttons. Messages waiting in a queue are lost. If the agent container restarts, its running requests end.
- **About the system:** ask the agent how the bot, its commands or its limits work. It reads its copy of this repo, as deployed, to answer.
- **Mentions:** the bot never pings anyone, whatever the agent writes.

A request is stopped, and the thread is told, when it hits one of these limits. Each one can be changed in `secrets/bridge.env`.

| Setting | Default | Stops a request when |
| --- | --- | --- |
| `PROMPT_TIMEOUT_SECONDS` | 1800 (30 min) | it has worked this long, not counting time spent waiting for an answer |
| `PROMPT_SILENCE_SECONDS` | 900 (15 min) | the agent has shown no activity for this long |
| `QUESTION_TIMEOUT_SECONDS` | 3600 (60 min) | a question has gone unanswered this long |
| `SHELL_TIMEOUT_SECONDS` | 600 (10 min) | a `/shell` command is still running after this long |

### Slash commands

Anyone who can post in an enabled channel can use its session commands. Changes are announced in the thread. `/status`, `/todo`, `/help` and the `/undo` confirmation are shown only to the person who used them.

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
| `/clear` | Clear the thread's context and start over. Stops any running request and drops waiting ones. Settings stay, and the old session is deleted from the agent. |
| `/compact` | Replace the conversation so far with a summary, to free up context |
| `/fork` | Open a new thread that carries on from here, with a copy of the conversation and settings |
| `/rename` | Rename the thread and its session |
| `/shell` | Run a shell command in the checkout and post its output. The agent sees it in later messages. |
| `/command` | Run one of the agent's own commands or skills, such as `review` |
| `/help` | List the commands |

- **In an enabled text channel** (outside any thread), `/model`, `/effort` and `/agent` set what new threads start with, and `/status` shows those defaults. A new thread copies the defaults when it opens. Session actions like `/stop` and `/shell` are used inside a thread.
- **`default`** as the value puts a setting back on the agent's own config.
- **While a request runs**, a settings change applies from the next message. `/compact`, `/fork`, `/shell` and `/command` wait their turn like a message would. `/undo` and `/redo` refuse.
- **`/undo` and the shared checkout:** undo puts the request's files back as they were before it. If another thread has changed those files since, its changes to them are lost too. The confirmation warns about this.

## Models

The models, the default and the effort levels all come from `opencode-agent/opencode.json`. The slash commands read them from the agent, so after you edit that file and redeploy, `/model` and `/effort` offer the new list. A thread whose model or effort level is no longer offered goes back to the default, and the thread is told.

- `model` is what threads use until someone runs `/model`. It is `deepseek-flash`, and `deepseek-v4-pro` is also configured.
- Each model's `variants` are its effort levels. `low`, `high` and `max` send DeepSeek's `reasoning_effort`, and `off` turns thinking off. With no level chosen, nothing is sent and DeepSeek uses its own default: thinking on, at `high`.

## Tests

The bridge's unit tests run inside a Docker build, so nothing needs installing on the host. The build fails if a test fails.

```bash
docker build --target test --progress plain -t opencord-bridge-test discord-bridge
```

They cover the bridge with stand-ins for Discord and the agent. The agent stand-in replays event streams recorded from a real OpenCode 1.19.5 server, kept in `discord-bridge/tests/fixtures/`. The live Discord side (buttons, file uploads, thread renames) is only checked by hand.

## Windows

- **Docker Desktop:** install it with the WSL 2 backend. In Settings → General, turn on "Start Docker Desktop when you sign in", and keep "Expose daemon on tcp://localhost:2375 without TLS" turned off.
- **Commands:** use `start.ps1` in PowerShell in place of `start.sh`.

  ```powershell
  Copy-Item -Recurse secrets.example secrets
  .\start.ps1           # build and (re)start everything
  .\start.ps1 -Status   # container state, and which commit each container runs
  .\start.ps1 -Logs     # follow the logs
  .\start.ps1 -Stop     # stop the containers (volumes are kept)
  ```

  If PowerShell says running scripts is disabled, run `powershell -ExecutionPolicy Bypass -File .\start.ps1` instead.
- **Password:** without `openssl`, this makes one for `shared.env`:

  ```powershell
  $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
  ```

- **Isolation:** Docker Desktop's VM can reach your Windows files, which it shares for bind mounts, and all WSL distros share its kernel. A container escape could therefore reach `C:`.
