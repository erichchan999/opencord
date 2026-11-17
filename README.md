# opencord

opencord is a Discord front end to [OpenCode](https://opencode.ai). @mention the bot in an enabled channel and it opens a thread with its own OpenCode session. Every session works in one shared sandbox folder that starts empty, where the agent can write code, run commands, install packages and clone public repos. Everything runs in Docker, with DeepSeek as the model provider.

## How it fits together

```
            ┌────────────────┐  bridge-net  ┌────────────────┐  llm-net  ┌─────────────┐
Discord ◀──▶│ discord-bridge │─────────────▶│ opencode-agent │──────────▶│  llm-proxy  │──▶ DeepSeek
            │ bot token      │              │ /work/sandbox  │           │ DeepSeek key│
            └────────────────┘              │                │           └─────────────┘
                                            └───────┬────────┘
                                                    └──▶ the open internet
                                                         (host, LAN and other containers blocked)
```

The bridge and the proxy each read only their own file in `secrets/`, so neither sees the other's key. The bridge and the agent share the OpenCode server password, the agent's only secret. State lives in Docker volumes and survives redeploys.

**Security:** anyone who can post in an enabled channel effectively controls the agent container, including running shell commands in it. Nothing from the host is mounted, the agent's firewall blocks every private address except the proxy, and all containers run without capabilities and with CPU, memory and process limits. The remaining risk is a container escape, which could reach your files.

## Setup

1. **Docker:** install Docker Desktop (Windows, macOS) or Docker Engine with the compose plugin (Linux). It needs Docker Engine 28 or later, Compose 2.33.1 or later and at least 2 CPUs, and the agent can use up to 4 GB of memory. Set Docker to start on its own (in Docker Desktop, **Start Docker Desktop when you sign in**) so the bot comes back after a reboot.

2. **Get the code:**

   ```bash
   git clone https://github.com/erichchan999/opencord.git
   cd opencord
   ```

3. **Secrets:** copy the examples, then fill in each file.

   ```bash
   cp -r secrets.example secrets
   ```

   | File | Set |
   | --- | --- |
   | `secrets/proxy.env` | `DEEPSEEK_API_KEY`, from platform.deepseek.com |
   | `secrets/bridge.env` | `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_IDS` (step 4). The limits under [Using it](#using-it) are optional. |
   | `secrets/shared.env` | `OPENCODE_SERVER_PASSWORD`, any long random string, such as the output of `openssl rand -hex 32` |

   Without `openssl`, as in PowerShell on Windows, this makes one:

   ```powershell
   $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
   ```

4. **Discord bot:** see [Discord bot](#discord-bot).

5. **Start it:** run `./start.sh`, or `.\start.ps1` in PowerShell on Windows. In the logs (`--logs`, or `-Logs`), look for `slash commands ready: ...` and `working in #...` for each enabled channel.

### Discord bot

1. **Create the app:** in the [Developer Portal](https://discord.com/developers/applications), click **New Application**. Its name is what people @mention.
2. **Installation:** untick **User Install** and set **Install Link** to **None**.
3. **Bot:** turn **Public Bot** off and **Message Content Intent** on. Click **Reset Token** and put the token in `secrets/bridge.env` as `DISCORD_BOT_TOKEN`.
4. **Invite it** with this link, using the **Application ID** from General Information:

   ```
   https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot&permissions=309237745728
   ```

5. **Allow it in your channels:** in each channel's **Permissions**, add the bot's role and allow View Channel, Send Messages, Send Messages in Threads, Create Public Threads, Read Message History, Add Reactions and Attach Files.
6. **Enable the channels:** set `DISCORD_CHANNEL_IDS` in `secrets/bridge.env` to a comma-separated list of channel IDs (with Developer Mode on, right-click a channel → **Copy Channel ID**). The bot ignores every other channel. To keep their messages from reaching it at all, deny its role View Channel there.

## Running it

```bash
./start.sh            # build and (re)start everything; run it again after editing any file here
./start.sh --status   # container state, and which commit each container runs
./start.sh --logs     # follow the logs
./start.sh --stop     # stop the containers (volumes are kept)
```

On Windows, `.\start.ps1` does the same, with `-Status`, `-Logs` and `-Stop`. If PowerShell says running scripts is disabled, run `powershell -ExecutionPolicy Bypass -File .\start.ps1` instead.

## Using it

- **New request:** @mention the bot in an enabled channel. It opens a thread, and every message in that thread continues the same session.
- **Set up first:** @mention the bot with nothing else to open an empty thread. Choose `/model`, `/effort` or `/agent` there, then send your question. The thread is named after it.
- **One sandbox:** every thread works in the same folder, `/work/sandbox`, so two threads editing the same files at once can collide. It isn't a git repo, and the agent has no GitHub credentials.
- **Parallel threads:** up to `MAX_CONCURRENT` (default 16) threads run at once. Within a thread, messages run one at a time, in order.
- **Attachments:** text files go to the agent as their content, up to 100 KB each. Other files are passed as links.
- **Files back:** ask the agent for a file and it attaches it after its reply, up to 8 MB each.
- **Questions:** the agent's questions appear with buttons. Anyone in the thread can answer, and the request's time limit is paused meanwhile.
- **Stop:** `!stop` or `/stop` stops the running request and drops the thread's waiting messages.
- **Side chat:** a message in a thread that starts with `//` is ignored, so people can talk there without the agent picking it up.
- **About the system:** the agent can answer questions about opencord itself from its copy of this repo.

A request is stopped, and the thread told, when it hits one of these limits, set in `secrets/bridge.env`:

| Setting | Default | Stops a request when |
| --- | --- | --- |
| `PROMPT_TIMEOUT_SECONDS` | 1800 (30 min) | it has worked this long, not counting time waiting for an answer |
| `PROMPT_SILENCE_SECONDS` | 900 (15 min) | the agent has shown no activity for this long |
| `QUESTION_TIMEOUT_SECONDS` | 3600 (60 min) | a question has gone unanswered this long |
| `SHELL_TIMEOUT_SECONDS` | 600 (10 min) | a `/shell` command is still running after this long |

### Slash commands

| Command | What it does |
| --- | --- |
| `/model` | Choose the thread's model |
| `/effort` | Choose how hard the model thinks: `off`, `low`, `high` or `max` |
| `/agent` | Choose the OpenCode agent: `build` (the default) or `plan`, which proposes without editing files |
| `/status` | Show the thread's settings, whether it is working and how full the context is |
| `/stop` | Same as `!stop` |
| `/undo` | Take the last request out of the conversation, after asking. Files in the sandbox stay as they are. |
| `/redo` | Bring back what `/undo` took back, until the next message |
| `/clear` | Start the thread over with an empty context, keeping its settings |
| `/compact` | Replace the conversation so far with a summary, to free up context |
| `/fork` | Open a new thread that carries on from here |
| `/rename` | Rename the thread and its session |
| `/shell` | Run a shell command in the sandbox and post its output |
| `/command` | Run one of the agent's own commands or skills, such as `review` |
| `/help` | List the commands |

Used in an enabled channel rather than a thread, `/model`, `/effort` and `/agent` set what new threads start with. `default` as the value goes back to the agent's own config.

## Models

The models, the default model and the effort levels come from `opencode-agent/opencode.json`. After editing it, run the start script again and the slash commands offer the new list. The default is `deepseek-flash`, and `deepseek-v4-pro` is also configured.
