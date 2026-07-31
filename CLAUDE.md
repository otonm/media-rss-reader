# CLAUDE.md — Media RSS Reader

## Project Overview

A self-hosted media RSS reader that fetches feeds from a folder or an OPML file and presents images, galleries, animations, and videos in a vertical scrolling interface or a slideshow. The backend runs continuously — fetching and storing feed items into SQLite in the background, exactly like a headless RSS client. Designed to run inside Docker and serve a browser-based UI over HTTP.

---

## Goals & Non-Goals

**Goals**
- Display only media items (images, GIFs, videos) from RSS feeds
- Smooth, responsive scrolling — including auto-scroll mode
- Slideshow mode: fullscreen, centered, with CSS fade transitions between items
- Pre-fetch and cache upcoming media to eliminate load stalls
- Persistent storage of feed items and seen state across restarts
- Continuous background fetching independent of browser activity
- Configurable entirely via environment variables
- Reddit Feeds companion service health status (modal overlay, backend proxy)

**Non-Goals**
- Article/text rendering (only a simple interface with some options and hints)
- User accounts or authentication
- External database service (SQLite only)

The media is at the center of attention.

---

## Documentation Policy

**Before implementing any feature that uses an external library or API, look up the current documentation.**

1. Always use Context7 when I need library/API documentation, code generation, setup or configuration steps.
2. If Context7 has no coverage, search the web directly.
3. Never rely on training-data knowledge — it may be outdated.

### Library Documentation Sources Examples

- `aiohttp`:
  - Example: _use context7 to show me how to open a http connection_
  - Documentation:`https://docs.aiohttp.org/en/stable/`
  - `https://github.com/aio-libs/aiohttp`

- `pydantic`:
  - Example: _use context7 to show me how to create a model_
  - Documentation:`https://docs.pydantic.dev/latest/`
  - Source: `https://github.com/pydantic/pydantic`

- `aiosqlite`:
  - Example: _use context7 to show me how to create a table_
  - Documentation: `https://aiosqlite.omnilib.dev/en/latest/api.html`
  - Source: `https://github.com/omnilib/aiosqlite`

- `litellm`:
  - Example: _use context7 to show me how to connect to openai_
  - Documentation: `https://docs.litellm.ai/docs/#litellm-python-sdk`
  - Source: `https://github.com/BerriAI/litellm`

---

## Dev Workflow

```bash
# install all deps including dev extras
uv sync --extra dev

# run the app
uv run uvicorn src.main:app --reload --port 8080

# lint (check only)
uv run ruff check .

# lint + fix
uv run ruff check --fix .

# format
uv run ruff format .

# tests + coverage
uv run pytest

# open coverage report
open htmlcov/index.html
```

---

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.

---

## Logging Style

- Use logging extensively: `logging.error` for fatal errors, `logging.warning` for recoverable errors, `logging.info` for important status changes, `logging.debug` for detailed program flow.
- When composing the messages always use f-strings and never the modulo operator (%).

---