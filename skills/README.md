# Skills for uad-console-mcp

Two companion Claude skills that teach a model how to run real studio workflows on top of this
MCP server.

- **`apollo-onboarding`**: plan and configure a rig from scratch. Interview questions, which input
  suits which source, the clock master decision for ADAT expanders, sample-rate and S/MUX limits,
  multi-Apollo cascades, then channel naming and a baseline snapshot.
- **`apollo-studio-tasks`**: day-to-day operation. Tracking setup and gain staging, Unison and
  insert chains, cue mixes, podcast and streaming layouts, troubleshooting decision trees, and
  snapshot-before-you-experiment habits.

Install for Claude Code (personal skills directory):

```sh
mkdir -p ~/.claude/skills
cp -r skills/apollo-onboarding skills/apollo-studio-tasks ~/.claude/skills/
```

Works with any MCP client that supports skills, or paste a `SKILL.md` in as a plain prompt
reference. Each skill is a self-contained directory: `SKILL.md` plus a `references/` folder the
skill points at when a step needs the detail.
