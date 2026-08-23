"""CLI entry point for NexusMind AI agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.config


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    level = "DEBUG" if debug else "INFO"
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": level,
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "handlers": ["console"],
            "level": level,
        },
    })


async def run_agent(goal: str, debug: bool = False) -> None:
    """Run the agent with a single goal."""
    from agent.orchestrator import orchestrator

    task = await orchestrator.handle_goal(goal)
    print(f"\n{'='*60}")
    print(f"Task Status: {task.status.value}")
    if task.result:
        print(f"Result: {task.result[:500]}")
    if task.error:
        print(f"Error: {task.error}")
    print(f"Steps executed: {len(task.steps)}")
    print(f"{'='*60}")


async def interactive_mode(debug: bool = False) -> None:
    """Run in interactive mode."""
    from agent.orchestrator import orchestrator

    print("NexusMind AI Agent — Interactive Mode")
    print("Type your goal and press Enter. Type 'quit' to exit.\n")

    while True:
        try:
            goal = input("Goal> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not goal or goal.lower() in ("quit", "exit", "q"):
            break

        task = await orchestrator.handle_goal(goal)
        status_color = "OK" if task.status.value == "completed" else "FAIL"
        print(f"[{status_color}] {task.status.value}: {(task.result or task.error or '')[:300]}\n")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="nexusmind",
        description="NexusMind AI — Autonomous Agent",
    )
    parser.add_argument("goal", nargs="?", help="Goal to accomplish (non-interactive)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--status", action="store_true", help="Show orchestrator status")

    args = parser.parse_args()
    setup_logging(debug=args.debug)

    if args.status:
        from agent.orchestrator import orchestrator
        status = orchestrator.get_status()
        for k, v in status.items():
            print(f"{k}: {v}")
        return

    if args.goal:
        asyncio.run(run_agent(args.goal, debug=args.debug))
    elif args.interactive:
        asyncio.run(interactive_mode(debug=args.debug))
    else:
        asyncio.run(interactive_mode(debug=args.debug))


if __name__ == "__main__":
    main()
