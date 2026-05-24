Kanban t_66a80edc audit: default profile skill restriction plan

Scope:
- Inspected /Users/verkyyi/.hermes/config.yaml as the default profile config.
- /Users/verkyyi/.hermes/profiles/default/config.yaml does not exist.
- Worker profile config exists separately and is not the target.
- No secrets included.

Current state:
- Default profile has 100 installed skills: 99 enabled, 1 globally disabled in CLI output.
- skills.disabled currently contains only kanban-worker globally.
- skills.platform_disabled.telegram currently disables 13 heavy/risky skills: godmode, comfyui, ascii-video, manim-video, touchdesigner-mcp, audiocraft-audio-generation, axolotl, unsloth, fine-tuning-with-trl, serving-llms-vllm, minecraft-modpack-server, pokemon-player, kanban-worker.
- Default platform_toolsets.telegram is already lean: clarify, cronjob, kanban, memory, messaging, session_search, skills, todo.
- With those Telegram toolsets, the generated skills prompt still advertises 86 skill rows across 25 categories: about 11,007 chars, roughly 2.7k tokens by chars/4.

Skill loading behavior found in code:
- agent/prompt_builder.py::build_skills_system_prompt injects a compact available-skills index into the system prompt.
- It does NOT load every full SKILL.md body by default; it injects category names plus each enabled skill name/description.
- Actual full SKILL.md content loads later only when the model calls skill_view or when a session is launched with preloaded skills.
- skills.platform_disabled.<platform> controls what appears for that platform; if present, it replaces the global disabled set for that platform, so Telegram's list must include every globally unwanted skill too, including kanban-worker.
- There is no non-interactive `hermes skills config` flag; it is interactive. Direct config.yaml patch is the practical reversible route.

Recommended default/Telegram posture:
- Treat default Telegram as front-desk/orchestrator, not universal specialist.
- Keep only:
  - agentfeeds
  - hermes-agent
  - kanban-orchestrator
  - founder-command-center
  - traction-watchdog
- Keep specialist skills available in CLI or specialist worker profiles, not in default Telegram prompt.
- If a Telegram request needs a specialist domain, default profile should create/route Kanban work or explicitly load by known name if necessary.

Expected speed impact:
- Prompt skill index drops from ~11,007 chars / ~2.7k rough tokens / 86 rows to ~2,445 chars / ~0.6k rough tokens / 6 rows.
- Net savings: ~8.6k chars, roughly ~2.1k tokens from every default Telegram/API call that includes the skills index.
- Latency improvement should be modest but real: less prompt input, less scanning/decision overhead, better prompt cache stability. Bigger latency wins still come from toolset restriction, no-tools synthesis mode, and avoiding unnecessary AgentFeeds/web refreshes.

Caveats:
- This can reduce opportunistic skill use for Apple/GitHub/media/creative/mlops/productivity tasks in Telegram because those skills won't be advertised.
- It does not remove tools; it only hides skill guidance/commands for that platform.
- Full skill files are not the main baseline bloat; the available-skills index and mandatory skill-scan instruction are.
- Changes require gateway restart for Telegram and /reset or a new CLI session for interactive CLI.

Exact proposed YAML replacement for /Users/verkyyi/.hermes/config.yaml:

skills:
  external_dirs: []
  template_vars: true
  inline_shell: false
  inline_shell_timeout: 10
  guard_agent_created: false
  creation_nudge_interval: 15
  disabled:
    - kanban-worker
  platform_disabled:
    telegram:
      - airtable
      - android-adb-automation
      - apple-calendar
      - apple-notes
      - apple-reminders
      - architecture-diagram
      - arxiv
      - ascii-art
      - ascii-video
      - audiocraft-audio-generation
      - authenticated-feed-sources
      - axolotl
      - baoyu-comic
      - baoyu-infographic
      - blogwatcher
      - claude-code
      - claude-design
      - codebase-inspection
      - codex
      - comfyui
      - debugging-hermes-tui-commands
      - design-md
      - dogfood
      - dspy
      - evaluating-llms-harness
      - excalidraw
      - findmy
      - fine-tuning-with-trl
      - gif-search
      - github-agentic-workflows
      - github-auth
      - github-code-review
      - github-issues
      - github-pr-workflow
      - github-repo-management
      - godmode
      - google-workspace
      - heartmula
      - hermes-agent-skill-authoring
      - himalaya
      - huggingface-hub
      - humanizer
      - ideation
      - imessage
      - jupyter-live-kernel
      - kanban-worker
      - lan-device-discovery
      - linear
      - llama-cpp
      - llm-wiki
      - manim-video
      - maps
      - market-data-streams
      - minecraft-modpack-server
      - nano-pdf
      - native-mcp
      - node-inspect-debugger
      - notion
      - obliteratus
      - obsidian
      - ocr-and-documents
      - opencode
      - openhue
      - outlines
      - p5js
      - pixel-art
      - plan
      - pokemon-player
      - polymarket
      - popular-web-designs
      - powerpoint
      - pretext
      - python-debugpy
      - requesting-code-review
      - research-paper-writing
      - segment-anything-model
      - serving-llms-vllm
      - sketch
      - songsee
      - songwriting-and-ai-music
      - spike
      - spotify
      - subagent-driven-development
      - systematic-debugging
      - tailnet-service-ops
      - test-driven-development
      - touchdesigner-mcp
      - unsloth
      - verky-obsidian-control-plane
      - webhook-subscriptions
      - weights-and-biases
      - writing-plans
      - xurl
      - youtube-content
      - yuanbao

Safe apply command, when approved:

cd /Users/verkyyi/.hermes/hermes-agent
/Users/verkyyi/.hermes/hermes-agent/venv/bin/python - <<'PY'
from pathlib import Path
import shutil, time, yaml
p = Path('/Users/verkyyi/.hermes/config.yaml')
backup = p.with_suffix('.yaml.bak.skill-restrict-' + time.strftime('%Y%m%d%H%M%S'))
shutil.copy2(p, backup)
data = yaml.safe_load(p.read_text()) or {}
skills = data.setdefault('skills', {})
skills['disabled'] = ['kanban-worker']
skills.setdefault('platform_disabled', {})
skills['platform_disabled']['telegram'] = ['airtable', 'android-adb-automation', 'apple-calendar', 'apple-notes', 'apple-reminders', 'architecture-diagram', 'arxiv', 'ascii-art', 'ascii-video', 'audiocraft-audio-generation', 'authenticated-feed-sources', 'axolotl', 'baoyu-comic', 'baoyu-infographic', 'blogwatcher', 'claude-code', 'claude-design', 'codebase-inspection', 'codex', 'comfyui', 'debugging-hermes-tui-commands', 'design-md', 'dogfood', 'dspy', 'evaluating-llms-harness', 'excalidraw', 'findmy', 'fine-tuning-with-trl', 'gif-search', 'github-agentic-workflows', 'github-auth', 'github-code-review', 'github-issues', 'github-pr-workflow', 'github-repo-management', 'godmode', 'google-workspace', 'heartmula', 'hermes-agent-skill-authoring', 'himalaya', 'huggingface-hub', 'humanizer', 'ideation', 'imessage', 'jupyter-live-kernel', 'kanban-worker', 'lan-device-discovery', 'linear', 'llama-cpp', 'llm-wiki', 'manim-video', 'maps', 'market-data-streams', 'minecraft-modpack-server', 'nano-pdf', 'native-mcp', 'node-inspect-debugger', 'notion', 'obliteratus', 'obsidian', 'ocr-and-documents', 'opencode', 'openhue', 'outlines', 'p5js', 'pixel-art', 'plan', 'pokemon-player', 'polymarket', 'popular-web-designs', 'powerpoint', 'pretext', 'python-debugpy', 'requesting-code-review', 'research-paper-writing', 'segment-anything-model', 'serving-llms-vllm', 'sketch', 'songsee', 'songwriting-and-ai-music', 'spike', 'spotify', 'subagent-driven-development', 'systematic-debugging', 'tailnet-service-ops', 'test-driven-development', 'touchdesigner-mcp', 'unsloth', 'verky-obsidian-control-plane', 'webhook-subscriptions', 'weights-and-biases', 'writing-plans', 'xurl', 'youtube-content', 'yuanbao']
p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')
print('backup:', backup)
PY
hermes gateway restart

Rollback:
- Restore the backup printed by the apply command to /Users/verkyyi/.hermes/config.yaml, then restart gateway.
