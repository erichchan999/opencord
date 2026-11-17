# opencord

opencord is a Discord front end to [OpenCode](https://opencode.ai). @mention the bot in an enabled channel and it opens a thread with its own OpenCode session, which works on a GitHub repo of your choice: reading and changing code, running commands and opening pull requests. Everything runs in Docker, with DeepSeek as the model provider.

## How it fits together

```
            ┌────────────────┐  bridge-net  ┌────────────────┐  llm-net  ┌─────────────┐
Discord ◀──▶│ discord-bridge │─────────────▶│ opencode-agent │──────────▶│  llm-proxy  │──▶ DeepSeek
            │ bot token      │              │ GitHub token   │           │ DeepSeek key│
            └────────────────┘              │ repo clone     │           └─────────────┘
                                            └───────┬────────┘
                                                    └──▶ GitHub and the open internet
                                                         (host, LAN and other containers blocked)
```

Each container reads only its own file in `secrets/`, so none sees another's keys. The bridge and the agent also share the OpenCode server password. State lives in Docker volumes and survives redeploys.

**Security:** anyone who can post in an enabled channel effectively controls the agent container, including running shell commands in it. Nothing from the host is mounted, the agent's firewall blocks every private address except the proxy, and all containers run without capabilities and with CPU, memory and process limits. The remaining risk is a container escape, which could reach your files.

## Setup

1. **Docker:** install Docker Desktop (Windows, macOS) or Docker Engine with the compose plugin (Linux), and have it start at boot so the bot comes back after a reboot. On Windows, also see [Windows](#windows).

2. **Secrets:** copy the examples, then fill in each file.

   ```bash
   cp -r secrets.example secrets
   ```

   | File | Set |
   | --- | --- |
   | `secrets/proxy.env` | `DEEPSEEK_API_KEY`, from platform.deepseek.com |
   | `secrets/agent.env` | `REPO` as `owner/name`, and `GH_TOKEN` (step 3) |
   | `secrets/bridge.env` | `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_IDS` (step 5). The limits under [Using it](#using-it) are optional. |
   | `secrets/shared.env` | `OPENCODE_SERVER_PASSWORD`, any long random string, such as the output of `openssl rand -hex 32` |

3. **GitHub token:** see [GitHub token](#github-token).

4. **Protect `main`:** in the repo, add a ruleset on `main` that requires a pull request with your approval and blocks force pushes and deletion.

5. **Discord bot:** see [Discord bot](#discord-bot).

6. **Start it:** run `./start.sh`. In `./start.sh --logs`, look for `slash commands ready: ...` and `working in #...` for each enabled channel.

### GitHub token

Create a fine-grained personal access token that can reach only the one repo: on GitHub, **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.

- **Resource owner:** the account or organization that owns the repo. An organization may have to approve the token.
- **Repository access:** **Only select repositories**, and pick the repo.
- **Permissions:** **Contents** and **Pull requests** set to **Read and write**. Leave everything else at **No access**, so nobody can change GitHub Actions workflows through the agent.

Put it in `secrets/agent.env` as `GH_TOKEN`. When it expires, make a new one and run the start script again.

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

## Using it

- **New request:** @mention the bot in an enabled channel. It opens a thread, and every message in that thread continues the same session.
- **One checkout:** every thread works in the same clone of the repo, so two threads editing the same files at once can collide. Ask the agent for a branch when a change needs one.
- **Parallel threads:** up to `MAX_CONCURRENT` (default 16) threads run at once. Within a thread, messages run one at a time, in order.
- **Attachments:** text files go to the agent as their content, up to 100 KB each. Other files are passed as links.
- **Questions:** the agent's questions appear with buttons. Anyone in the thread can answer, and the request's time limit is paused meanwhile.
- **Stop:** `!stop` or `/stop` stops the running request and drops the thread's waiting messages.
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
| `/undo` | Take back the last request and its file changes, after asking. Changes other threads made to those files since are lost too. |
| `/redo` | Bring back what `/undo` took back, until the next message |
| `/clear` | Start the thread over with an empty context, keeping its settings |
| `/compact` | Replace the conversation so far with a summary, to free up context |
| `/fork` | Open a new thread that carries on from here |
| `/rename` | Rename the thread and its session |
| `/shell` | Run a shell command in the checkout and post its output |
| `/command` | Run one of the agent's own commands or skills, such as `review` |
| `/help` | List the commands |

Used in an enabled channel rather than a thread, `/model`, `/effort` and `/agent` set what new threads start with. `default` as the value goes back to the agent's own config.

## Models

The models, the default model and the effort levels come from `opencode-agent/opencode.json`. After editing it, run the start script again and the slash commands offer the new list. The default is `deepseek-flash`, and `deepseek-v4-pro` is also configured.

## Windows

- **Docker Desktop:** use the WSL 2 backend, turn on "Start Docker Desktop when you sign in", and keep "Expose daemon on tcp://localhost:2375 without TLS" off.
- **Commands:** use `start.ps1` in PowerShell in place of `start.sh`.

  ```powershell
  Copy-Item -Recurse secrets.example secrets
  .\start.ps1           # build and (re)start everything
  .\start.ps1 -Status   # container state, and which commit each container runs
  .\start.ps1 -Logs     # follow the logs
  .\start.ps1 -Stop     # stop the containers (volumes are kept)
  ```

  If scripts are disabled, run `powershell -ExecutionPolicy Bypass -File .\start.ps1` instead.
- **Password:** without `openssl`, this makes one for `shared.env`:

  ```powershell
  $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
  ```
