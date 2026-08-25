# Contributing to NexusMind AI

Thank you for your interest in contributing! This document provides guidelines and steps for contributing.

## How to Contribute

### Reporting Bugs

1. Check existing issues to avoid duplicates
2. Open a new issue using the **Bug Report** template
3. Include:
   - Clear description of the problem
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Python version)

### Suggesting Features

1. Check existing issues/discussions first
2. Open a new issue using the **Feature Request** template
3. Describe the use case and expected behavior

### Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Run tests: `python -m pytest tests/ -v`
5. Run linter: `ruff check .`
6. Run formatter: `ruff format .`
7. Commit with clear message
8. Push and create a Pull Request

## Development Setup

Requires Python 3.11+.

```bash
# Clone
git clone https://github.com/tamimlabs/nexusmind-ai.git
cd nexusmind-ai

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -e .

# Copy environment template
cp .env.example .env
# Edit .env with your Gemini API key(s)

# Run tests
python -m pytest tests/ -v

# Run linter
ruff check .

# Run formatter
ruff format .
```

## Code Style

- **Formatter:** ruff format (Black-compatible)
- **Linter:** ruff
- **Type Checking:** mypy (`mypy <file>` on individual files, or `mypy agent/`)
- **Line Length:** 100 characters max
- **Quotes:** Double quotes for strings
- **Trailing Commas:** Yes, in multi-line structures

## Commit Messages

Use conventional commit prefixes:

- `feat:` new features
- `fix:` bug fixes
- `docs:` documentation changes
- `test:` test additions or updates
- `refactor:` code refactoring
- `chore:` maintenance tasks

Examples:

- `feat: add new web scraping skill`
- `fix: handle rate limit in Gemini client`
- `docs: update README with architecture diagram`
- `test: add tests for memory deduplication`
- `refactor: simplify orchestrator loop`

## Testing

The current suite has 180 Passing Tests covering models, memory (hybrid
retrieval, HRR vectors, trust scoring), the self-evolving skill library
(validation gates, lifecycle, matching, audit ledger), deterministic routing
(command gate, tool-name repair ladder), executor, orchestrator, API, the
planner/GitHub pipeline, and watcher gating. Async tests run with
`pytest-asyncio` in auto mode.

- Write tests for new features
- Ensure all existing tests pass
- Aim for meaningful coverage, not 100% line coverage
- Test both success and failure paths

```bash
# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=agent --cov-report=term-missing
```

## Project Structure

```
nexusmind-ai/
├── agent/                    # Core agent logic
│   ├── core/                 # Planner, executor, Gemini client, memory
│   ├── skills/               # Modular skills (web_research, file_management,
│   │                         # data_processing, github)
│   ├── watchers/             # Event monitors (11 watchers + base.py + manager.py)
│   ├── orchestrator.py       # Main agent loop
│   ├── telegram.py           # Telegram integration
│   └── observability.py      # Observability helpers
├── cloud/                    # Google Cloud integrations
│   ├── vertex_ai/
│   ├── firestore/
│   ├── pubsub/
│   └── cloud_run/
├── api/                      # REST API layer
│   ├── main.py               # App entrypoint
│   ├── dashboard.html        # Web dashboard
│   ├── watcher_routes.py     # Watcher endpoints
│   └── credentials_routes.py # Credentials endpoints
├── tests/                    # Test suite
└── scripts/                  # Setup and deployment scripts
```

## Adding a New Skill

1. Create a new directory under `agent/skills/<name>/` containing a `skill.py`
2. Define your tool functions with the `@register_tool` decorator (imported from `agent.core.executor`)
3. Add the package name to the `_SKILL_PACKAGES` list in `agent/skills/loader.py` so it auto-loads
4. For high-risk tools, pass `high_risk=True` so approval gates apply
5. Add tests in `tests/`
6. Update documentation if needed

## Adding a New Watcher

1. Create a new module in `agent/watchers/your_watcher.py`
2. Subclass `BaseWatcher` and implement `check_for_events()` and `process_event()`
3. Register the type in the `_WATCHER_TYPES` dict in `agent/watchers/manager.py`
4. Add tests in `tests/`

### Memory Gate (Important)

If your watcher auto-generates goals, set the class attribute
`INSTRUCTION_KEYWORDS` to domain keywords so the memory gate works. Events then
require a matching standing instruction in memory before acting; use the
inherited helpers `standing_instruction()`, `gated_goal()`, and
`notify_unhandled_event()`. Only owner-configured-goal watchers (such as cron
and webhook) may skip this gate.

## Questions?

Open a discussion or issue on GitHub.
