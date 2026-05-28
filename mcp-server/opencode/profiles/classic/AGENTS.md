# Global OpenCode Rules

Prefer project-local `AGENTS.md`, project config, project skills, and project agents whenever they exist.

Do not assume repo-specific architecture, MCP topology, workflows, or conventions across projects.

When a project does not provide local instructions, derive behavior from the local files and keep changes conservative.

When a global MCP is configured and no project-local instruction overrides it, treat the common local MCP stack as available:

- `playwright-local`
- `searxng-local`
- `firecrawl-local`
- `neo4j-memory-local`

Use `playwright-local` for browser automation, interactive UI verification, and deterministic reproduction of web flows inside OpenCode. Prefer it over ad hoc browser scripting when the task is about interacting with a page, validating UI behavior, or collecting browser-native evidence.

Use lighter tools first when they fit better:

- use local files or repo structure tools for repo-internal questions
- use search/scrape MCPs for web discovery or one-page extraction
- use `playwright-local` when the page interaction itself is the task or simpler fetch/scrape paths are insufficient
