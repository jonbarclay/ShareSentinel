# Contributing to ShareSentinel

Thank you for your interest in contributing to ShareSentinel! This guide will help you get started.

## Development Environment Setup

ShareSentinel runs entirely in Docker containers. You do not need to install Python dependencies on your host machine.

1. Clone the repository:
   ```bash
   git clone https://github.com/your-org/ShareSentinel.git
   cd ShareSentinel
   ```

2. Copy the environment template and fill in your values:
   ```bash
   cp .env.example .env
   ```

3. Build and start all services:
   ```bash
   docker compose up --build -d
   ```

## Important: All Code Runs Inside Docker

All code execution, testing, and debugging **must** happen inside the Docker containers. The services depend on Redis, PostgreSQL, and inter-service networking that are only available within the Docker Compose environment. Never run service code directly on the host.

## Running Tests

```bash
docker exec sharesentinel-worker python -m pytest tests/ -v
```

## Code Style

- Python 3.12+
- Follow the existing code patterns and conventions in the codebase
- Use type hints consistently
- Keep functions focused and well-documented

## Pull Request Process

1. **Fork** the repository
2. **Create a branch** for your changes (`git checkout -b my-feature`)
3. **Make your changes** and ensure they follow the existing code style
4. **Run the tests** to verify nothing is broken
5. **Commit** with a clear, descriptive message
6. **Open a Pull Request** against `main`

Please include a description of what your PR does and why. If it addresses an open issue, reference it in the PR description.

## Reporting Issues

If you find a bug or have a feature request, please open a GitHub issue. For security vulnerabilities, see [SECURITY.md](SECURITY.md).
