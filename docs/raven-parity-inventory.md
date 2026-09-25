# Raven feature inventory vs Helix (from Raven source at 06d6199, 2026-09-24)

Legend: HAS / PARTIAL / LEAN (lean equivalent built or planned) / SKIP (with reason)

## Core runtime
| Raven feature | Their impl | Helix status |
| --- | --- | --- |
| Agent loop + turn scheduler | spine/ + agent/ | HAS (executor) |
| DAG orchestration | run_subagent_dag | HAS |
| Typed plan validation + repair | none (free-form host) | HAS (our edge) |
| Token/cost accounting | token_wise/ | HAS (budget governor + cost meter) |
| Model routing | knn router + endpoint rotor | PARTIAL (cheap/strong tiers) |
| Event log / trajectory / tracing | trajectory/ + tracing/ | PARTIAL (event log + export) |
| Sessions | session/ | HAS (jobs) |

## Agents + connections
| Built-in agents (research/code/design/oncall/ppt) | agents/ products | PARTIAL (node kinds; prebuilt plan templates planned) |
| Third-party agent presets | ACP + CLI + HTTP | DONE (lean): preset registry - claude-code, codex, gemini-cli, aider, goose, opencode + http/custom |
| ACP protocol | acp/ + acp_client/ | LEAN (documented worker protocol; full ACP skipped) |
| A2A protocol | a2a/ + a2a_client/ | HAS lean: /.well-known/agent.json + /a2a submit |
| MCP client | mcp/ | DONE (lean): `helix mcp` config + passthrough to workers (--mcp-config / HELIX_MCP_CONFIG) |

## Memory + skills
| EverOS long-term memory | everos-memory plugin | DONE: markdown memory store, planner + node recall, `helix memory` |
| SkillForge + SkillHub | skill_forge + skill_hub | LEAN: local skills dir |
| Knowledge base | knowledge/ | LEAN: folded into memory store |
| Playbooks | playbook/ | DONE: named validated plans, `helix playbook save/run/list/show/delete`, API run endpoint |

## Automation
| Proactivity: sentinel + cron | proactive_engine/ | DONE (cron): 5-field cron + `helix schedule daemon`; webhooks still open (P2) |
| Evolver (self-evolution) | evolver/ (retiring) | SKIP - they retired it; `helix eval` is the honest equivalent |
| Sandbox code exec | sandbox/ | HAS lean: kind="exec" nodes with allowlist |

## Surfaces
| WebUI | ui-web (React) | HAS (dashboard, light+dark) |
| TUI | ui-tui (React/Ink) | DONE (lean): `helix watch` live terminal view |
| 12 messaging channels | channels/adapters/ | DONE (lean): tokenized inbound webhooks + outbound hooks dir |
| Browser tools | browser/ | SKIP (needs a browser stack; low parity value) |

## Platform
| Permissions system | permissions/ | PARTIAL (approval gates + exec allowlist) |
| Plugins | plugins/ | DONE (lean): hooks dir - executable per event type, JSON on stdin |
| Marketplace | market/ | SKIP (hosted ecosystem) |
| Importer | importer/ | SKIP (low value) |
| OAuth provider login | auth/ | SKIP (API keys cover it) |
| i18n | i18n/ | SKIP (English-first demo) |
| Self-update | updates/ | SKIP |
| Voice/transcription | transcribe | SKIP (audio stack) |
| Doctor/status | cli doctor | DONE: `helix doctor` |
