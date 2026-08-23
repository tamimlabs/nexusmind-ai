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
# Edit .env with your Gemini API keys

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
- **Type Checking:** mypy
- **Line Length:** 120 characters max
- **Quotes:** Double quotes for strings
- **Trailing Commas:** Yes, in multi-line structures

## Commit Messages

Use clear, descriptive commit messages:

- `feat: add new web scraping skill`
- `fix: handle rate limit in Gemini client`
- `docs: update README with architecture diagram`
- `test: add tests for memory deduplication`
- `refactor: simplify orchestrator loop`

## Testing

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
├── agent/              # Core agent logic
│   ├── core/           # Planner, executor, memory, Gemini client
│   ├── skills/         # Modular skill plugins
│   ├── watchers/       # Always-awake event monitors (11 platforms)
│   └── orchestrator.py # Main agent loop
├── cloud/              # Google Cloud integrations
├── api/                # REST API + dashboard + credentials
├── tests/              # Test suite
└── scripts/            # Deployment scripts
```

## Adding a New Skill

1. Create a new directory in `agent/skills/your_skill/`
2. Create `skill.py` with your tool functions
3. Register tools using `@register_tool` decorator
4. Add tests in `tests/`
5. Update documentation if needed

## Adding a New Watcher

1. Create a new file in `agent/watchers/your_watcher.py`
2. Inherit from `BaseWatcher`
3. Implement `check_for_events()` and `process_event()`
4. Register in `agent/watchers/manager.py` `_WATCHER_TYPES` dict
5. Add to `agent/watchers/__init__.py` exports
6. Add tests in `tests/`

## Questions?

Open a discussion or issue on GitHub.
